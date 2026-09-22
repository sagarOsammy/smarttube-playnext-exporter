#!/usr/bin/env python3
"""Parse an unencrypted Android TV provider backup into SmartTube JSON and CSV."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import re
import sqlite3
import tarfile
import tempfile
import zlib
from pathlib import Path
from typing import Any


PROVIDER_PACKAGE = "com.android.providers.tv"
PACKAGE_LABELS = {
    "org.smarttube.stable": "SmartTube current package ID",
    "com.teamsmart.videomanager.tv": "SmartTube legacy package ID",
}
VIDEO_ID_RE = re.compile(r"(?:[?&])v=([^&#;]+)")


def iso_now() -> str:
    return dt.datetime.now().astimezone().isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_backup(path: Path) -> tuple[bytes, dict[str, Any]]:
    data = path.read_bytes()
    offset = 0
    header_lines = []
    for _ in range(4):
        end = data.find(b"\n", offset)
        if end < 0:
            raise ValueError("Android backup header is incomplete.")
        header_lines.append(data[offset:end].decode("utf-8"))
        offset = end + 1

    if header_lines[0] != "ANDROID BACKUP":
        raise ValueError("Input is not an Android backup.")
    if header_lines[3] != "none":
        raise ValueError("Encrypted Android backups are not supported.")

    compressed = header_lines[2] == "1"
    payload = data[offset:]
    if compressed:
        payload = zlib.decompress(payload)
    return payload, {
        "magic": header_lines[0],
        "version": int(header_lines[1]),
        "compressed": compressed,
        "encryption": header_lines[3],
        "header_bytes": offset,
    }


def extract_tv_database(
    payload: bytes, destination: Path
) -> tuple[Path, list[str], int]:
    expected = {
        f"apps/{PROVIDER_PACKAGE}/db/tv.db",
        f"apps/{PROVIDER_PACKAGE}/db/tv.db-wal",
        f"apps/{PROVIDER_PACKAGE}/db/tv.db-shm",
    }
    extracted: list[str] = []
    destination.mkdir(parents=True, exist_ok=True)

    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        members = archive.getmembers()
        for member in members:
            if member.name not in expected:
                continue
            if not member.isfile():
                raise ValueError(f"Expected a regular database member: {member.name}")
            relative = Path(member.name).relative_to(f"apps/{PROVIDER_PACKAGE}")
            output = destination / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"Could not read backup member: {member.name}")
            output.write_bytes(source.read())
            extracted.append(member.name)

    db_path = destination / "db" / "tv.db"
    if not db_path.is_file():
        raise ValueError("The TV provider database was not present in the backup.")
    return db_path, sorted(extracted), len(members)


def youtube_video_id(intent_uri: Any) -> str | None:
    if not isinstance(intent_uri, str):
        return None
    match = VIDEO_ID_RE.search(intent_uri)
    return match.group(1) if match else None


def record_status(browsable: Any) -> str:
    if browsable == 1:
        return "browsable"
    if browsable == 0:
        return "retained_not_browsable"
    return "unknown_browsable_value"


def table_columns(connection: sqlite3.Connection) -> list[str]:
    columns = [
        row[1]
        for row in connection.execute("pragma table_info(watch_next_programs)")
    ]
    required = {"_id", "package_name", "browsable", "intent_uri"}
    if not required.issubset(columns):
        raise ValueError("The provider database has an unexpected Watch Next schema.")
    return columns


def records_for_package(
    connection: sqlite3.Connection, package_name: str, columns: list[str]
) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"select {', '.join(columns)} from watch_next_programs "
        "where package_name=? order by _id",
        (package_name,),
    ).fetchall()
    records = []
    for row in rows:
        record = dict(zip(columns, row))
        record["record_status"] = record_status(record.get("browsable"))
        record["youtube_video_id"] = youtube_video_id(record.get("intent_uri"))
        records.append(record)
    return records


def validation_for(
    records: list[dict[str, Any]], integrity: str, quick: str
) -> dict[str, Any]:
    source_ids = [r.get("_id") for r in records if r.get("_id") is not None]
    video_ids = [r["youtube_video_id"] for r in records if r["youtube_video_id"]]
    browsable = [r for r in records if r.get("browsable") == 1]
    browsable_video_ids = {
        r["youtube_video_id"]
        for r in browsable
        if r["youtube_video_id"]
    }
    return {
        "source_row_count": len(records),
        "unique_source_id_count": len(set(source_ids)),
        "duplicate_source_id_count": len(source_ids) - len(set(source_ids)),
        "browsable_row_count": len(browsable),
        "retained_not_browsable_row_count": sum(
            r.get("browsable") == 0 for r in records
        ),
        "unique_browsable_video_id_count": len(browsable_video_ids),
        "duplicate_youtube_video_id_count": len(video_ids) - len(set(video_ids)),
        "sqlite_integrity_check": integrity,
        "sqlite_quick_check": quick,
    }


def write_csv(path: Path, records: list[dict[str, Any]], columns: list[str]) -> None:
    fields = ["record_status", "youtube_video_id", *columns]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def export_package(
    output_dir: Path,
    package_name: str,
    columns: list[str],
    records: list[dict[str, Any]],
    integrity: str,
    quick: str,
    source: dict[str, Any],
    exported_at: str,
) -> dict[str, Any]:
    validation = validation_for(records, integrity, quick)
    payload = {
        "export_type": "android_tv_watch_next_provider",
        "exported_at": exported_at,
        "package": {
            "display_name": "SmartTube",
            "package_name": package_name,
            "package_role": PACKAGE_LABELS[package_name],
        },
        "source": source,
        "scope": {
            "table": "watch_next_programs",
            "included": [
                "all rows matching this exact package_name",
                "raw browsable value and provider columns",
                "YouTube video ID parsed from intent_uri",
            ],
        },
        "columns": columns,
        "records": records,
        "browsable_records": [
            record for record in records if record.get("browsable") == 1
        ],
        "validation": validation,
    }
    stem = f"tv-watch-next-{package_name}"
    json_path = output_dir / f"{stem}.json"
    csv_path = output_dir / f"{stem}.csv"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_csv(csv_path, records, columns)
    return {
        "package_name": package_name,
        "package_role": PACKAGE_LABELS[package_name],
        "source_row_count": len(records),
        "browsable_row_count": validation["browsable_row_count"],
        "unique_browsable_video_id_count": validation[
            "unique_browsable_video_id_count"
        ],
        "retained_not_browsable_row_count": validation[
            "retained_not_browsable_row_count"
        ],
        "outputs": [json_path.name, csv_path.name],
    }


def export_from_backup(backup_path: Path, output_dir: Path) -> dict[str, Any]:
    if not backup_path.is_file():
        raise FileNotFoundError(f"ADB backup not found: {backup_path}")
    output_dir.mkdir(parents=True, exist_ok=True)
    exported_at = iso_now()
    manifest_path = output_dir / "tv-watch-next-export-manifest.json"
    expected_outputs = [manifest_path]
    for package_name in PACKAGE_LABELS:
        stem = f"tv-watch-next-{package_name}"
        expected_outputs.extend(
            (output_dir / f"{stem}.json", output_dir / f"{stem}.csv")
        )
    existing = [path.name for path in expected_outputs if path.exists()]
    if existing:
        raise FileExistsError(
            "Refusing to overwrite existing export files: " + ", ".join(existing)
        )

    with tempfile.TemporaryDirectory(prefix="tv-provider-export-") as temporary:
        payload, header = read_backup(backup_path)
        db_path, extracted, member_count = extract_tv_database(
            payload, Path(temporary)
        )
        database_hash = sha256(db_path)
        connection = sqlite3.connect(str(db_path))
        connection.row_factory = sqlite3.Row
        integrity = connection.execute("pragma integrity_check").fetchone()[0]
        quick = connection.execute("pragma quick_check").fetchone()[0]
        if integrity != "ok" or quick != "ok":
            connection.close()
            raise ValueError("SQLite integrity validation failed.")

        columns = table_columns(connection)
        source = {
            "backup_filename": backup_path.name,
            "backup_size_bytes": backup_path.stat().st_size,
            "backup_sha256": sha256(backup_path),
            "database_sha256": database_hash,
            "backup_header": header,
            "decompressed_tar_bytes": len(payload),
            "tar_member_count": member_count,
            "database_members_extracted": extracted,
        }
        packages = []
        for package_name in PACKAGE_LABELS:
            records = records_for_package(connection, package_name, columns)
            packages.append(
                export_package(
                    output_dir,
                    package_name,
                    columns,
                    records,
                    integrity,
                    quick,
                    source,
                    exported_at,
                )
            )
        connection.close()

    manifest = {
        "export_type": "android_tv_watch_next_provider_manifest",
        "exported_at": exported_at,
        "source": source,
        "database_validation": {
            "sqlite_integrity_check": integrity,
            "sqlite_quick_check": quick,
            "smarttube_package_row_count": sum(
                package["source_row_count"] for package in packages
            ),
        },
        "packages": packages,
        "notes": [
            "SmartTube package IDs are exported separately.",
            "Rows marked browsable=0 are retained with an explicit status.",
        ],
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", type=Path, required=True, help="ADB backup file")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("exports"),
        help="Directory for JSON, CSV, and manifest outputs",
    )
    args = parser.parse_args()
    try:
        manifest = export_from_backup(args.backup, args.output_dir)
    except (OSError, ValueError, sqlite3.Error, tarfile.TarError, zlib.error) as error:
        parser.exit(2, f"Export stopped: {error}\n")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
