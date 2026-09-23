#!/usr/bin/env python3
"""Prepare and restore a TV-provider backup with Watch Next rows removed."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import zlib
from pathlib import Path
from typing import Any

from tv_provider_export import PROVIDER_PACKAGE, extract_tv_database, read_backup


WATCH_NEXT_TABLE = "watch_next_programs"
DATABASE_MEMBER = f"apps/{PROVIDER_PACKAGE}/db/tv.db"
WAL_MEMBER = f"{DATABASE_MEMBER}-wal"
SHM_MEMBER = f"{DATABASE_MEMBER}-shm"
BACKUP_NAME = f"{PROVIDER_PACKAGE}.original.ab"
CLEARED_BACKUP_NAME = f"{PROVIDER_PACKAGE}.watch-next-cleared.ab"
MANIFEST_NAME = "watch-next-clear-manifest.json"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_adb(requested: str | None) -> str:
    candidate = requested or "adb"
    if Path(candidate).is_file():
        return str(Path(candidate).resolve())
    located = shutil.which(candidate)
    if located:
        return located
    raise FileNotFoundError(
        "ADB was not found. Install Android SDK Platform-Tools or pass --adb."
    )


def require_device(adb: str, device: str) -> None:
    state = subprocess.run(
        [adb, "-s", device, "get-state"],
        check=False,
        capture_output=True,
        text=True,
    )
    if state.returncode != 0 or state.stdout.strip() != "device":
        raise RuntimeError(
            "ADB cannot use the selected device. Connect it and approve the "
            "debugging prompt, then check adb devices -l."
        )


def require_new_files(paths: list[Path]) -> None:
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise FileExistsError(
            "Refusing to overwrite existing files: " + ", ".join(existing)
        )


def capture_provider_backup(adb: str, device: str, path: Path) -> None:
    require_device(adb, device)
    path.parent.mkdir(parents=True, exist_ok=True)
    require_new_files([path])
    print("Approve the full backup request on the TV to continue.")
    result = subprocess.run(
        [
            adb,
            "-s",
            device,
            "backup",
            "-noapk",
            "-f",
            str(path),
            PROVIDER_PACKAGE,
        ],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("ADB backup did not complete successfully.")
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError("ADB did not create a usable provider backup.")


def quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def table_row_counts(connection: sqlite3.Connection) -> dict[str, int]:
    names = [
        row[0]
        for row in connection.execute(
            "select name from sqlite_master "
            "where type='table' and name not like 'sqlite_%' order by name"
        )
    ]
    return {
        name: int(
            connection.execute(
                f"select count(*) from {quoted_identifier(name)}"
            ).fetchone()[0]
        )
        for name in names
    }


def package_row_counts(connection: sqlite3.Connection) -> dict[str, int]:
    columns = {
        row[1]
        for row in connection.execute(
            f"pragma table_info({quoted_identifier(WATCH_NEXT_TABLE)})"
        )
    }
    if "package_name" not in columns:
        return {}
    return {
        str(package_name): int(row_count)
        for package_name, row_count in connection.execute(
            f"select package_name, count(*) from {quoted_identifier(WATCH_NEXT_TABLE)} "
            "group by package_name order by package_name"
        )
    }


def sqlite_validation(connection: sqlite3.Connection) -> tuple[str, str]:
    integrity = str(connection.execute("pragma integrity_check").fetchone()[0])
    quick = str(connection.execute("pragma quick_check").fetchone()[0])
    if integrity != "ok" or quick != "ok":
        raise ValueError("SQLite integrity validation failed.")
    return integrity, quick


def inspect_and_clear_database(database: Path) -> dict[str, Any]:
    connection = sqlite3.connect(str(database))
    try:
        integrity_before, quick_before = sqlite_validation(connection)
        before_counts = table_row_counts(connection)
        if WATCH_NEXT_TABLE not in before_counts:
            raise ValueError("The provider database has no Watch Next table.")

        rows_before = before_counts[WATCH_NEXT_TABLE]
        packages_before = package_row_counts(connection)
        connection.execute("begin immediate")
        deleted = connection.execute(
            f"delete from {quoted_identifier(WATCH_NEXT_TABLE)}"
        ).rowcount
        connection.commit()

        checkpoint = connection.execute("pragma wal_checkpoint(truncate)").fetchone()
        if checkpoint is not None and int(checkpoint[0]) != 0:
            raise ValueError("Could not checkpoint the provider database WAL.")
        journal_mode = str(
            connection.execute("pragma journal_mode=delete").fetchone()[0]
        )
        if journal_mode.lower() != "delete":
            raise ValueError("Could not consolidate the provider database WAL.")

        integrity_after, quick_after = sqlite_validation(connection)
        after_counts = table_row_counts(connection)
        if after_counts.get(WATCH_NEXT_TABLE) != 0:
            raise ValueError("Watch Next rows remain after the delete operation.")

        preserved_tables = {
            name: count
            for name, count in before_counts.items()
            if name != WATCH_NEXT_TABLE
        }
        observed_preserved_tables = {
            name: after_counts.get(name)
            for name in preserved_tables
        }
        if observed_preserved_tables != preserved_tables:
            raise ValueError(
                "A non-Watch-Next table row count changed. No restore backup was written."
            )
        if set(after_counts) != set(before_counts):
            raise ValueError("The database table list changed during preparation.")
        if deleted != rows_before:
            raise ValueError("SQLite reported an unexpected number of deleted rows.")
        if integrity_before != integrity_after or quick_before != quick_after:
            raise ValueError("SQLite integrity validation changed unexpectedly.")
        if package_row_counts(connection):
            raise ValueError("Package rows remain in Watch Next after deletion.")

        return {
            "rows_before": rows_before,
            "rows_deleted": deleted,
            "rows_after": 0,
            "package_rows_before": packages_before,
            "non_watch_next_table_row_counts": preserved_tables,
            "non_watch_next_table_row_counts_preserved": True,
            "sqlite_integrity_check": integrity_after,
            "sqlite_quick_check": quick_after,
        }
    finally:
        connection.close()


def rebuild_tar_payload(payload: bytes, database_bytes: bytes) -> tuple[bytes, int]:
    source_buffer = io.BytesIO(payload)
    output_buffer = io.BytesIO()
    db_members = 0
    skipped_sidecars = 0

    with tarfile.open(fileobj=source_buffer, mode="r:") as source:
        members = source.getmembers()
        db_members = sum(member.name == DATABASE_MEMBER for member in members)
        if db_members != 1:
            raise ValueError("Expected exactly one TV provider database in the backup.")
        if any(
            member.name in {WAL_MEMBER, SHM_MEMBER}
            and not member.isfile()
            for member in members
        ):
            raise ValueError("The provider backup contains an unexpected WAL sidecar.")

        with tarfile.open(fileobj=output_buffer, mode="w:", format=source.format) as output:
            for member in members:
                if member.name == DATABASE_MEMBER:
                    replacement = copy.copy(member)
                    replacement.size = len(database_bytes)
                    output.addfile(replacement, io.BytesIO(database_bytes))
                    continue
                if member.name in {WAL_MEMBER, SHM_MEMBER}:
                    skipped_sidecars += 1
                    continue
                stream = source.extractfile(member) if member.isfile() else None
                if member.isfile() and stream is None and member.size:
                    raise ValueError(f"Could not preserve backup member: {member.name}")
                output.addfile(member, stream)

    return output_buffer.getvalue(), skipped_sidecars


def write_android_backup(payload: bytes, header: dict[str, Any], path: Path) -> None:
    compressed = bool(header["compressed"])
    body = zlib.compress(payload) if compressed else payload
    lines = [
        "ANDROID BACKUP",
        str(header["version"]),
        "1" if compressed else "0",
        "none",
    ]
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8") + body)


def database_details(backup_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    payload, header = read_backup(backup_path)
    with tempfile.TemporaryDirectory(prefix="watch-next-clear-") as temporary:
        database, extracted, member_count = extract_tv_database(
            payload, Path(temporary)
        )
        connection = sqlite3.connect(str(database))
        try:
            integrity, quick = sqlite_validation(connection)
            counts = table_row_counts(connection)
            if WATCH_NEXT_TABLE not in counts:
                raise ValueError("The provider database has no Watch Next table.")
            details = {
                "watch_next_rows": counts[WATCH_NEXT_TABLE],
                "package_rows": package_row_counts(connection),
                "sqlite_integrity_check": integrity,
                "sqlite_quick_check": quick,
                "table_row_counts": counts,
            }
        finally:
            connection.close()
    return header, {
        "database_members_extracted": extracted,
        "tar_member_count": member_count,
        **details,
    }


def prepare(adb: str, device: str, output_dir: Path) -> dict[str, Any]:
    original_path = output_dir / BACKUP_NAME
    cleared_path = output_dir / CLEARED_BACKUP_NAME
    manifest_path = output_dir / MANIFEST_NAME
    require_new_files([original_path, cleared_path, manifest_path])
    output_dir.mkdir(parents=True, exist_ok=True)

    capture_provider_backup(adb, device, original_path)
    payload, header = read_backup(original_path)
    with tempfile.TemporaryDirectory(prefix="watch-next-clear-") as temporary:
        database, extracted, member_count = extract_tv_database(
            payload, Path(temporary)
        )
        validation = inspect_and_clear_database(database)
        database_bytes = database.read_bytes()
        rebuilt_payload, sidecars_removed = rebuild_tar_payload(
            payload, database_bytes
        )

    write_android_backup(rebuilt_payload, header, cleared_path)
    if not cleared_path.is_file() or cleared_path.stat().st_size == 0:
        raise RuntimeError("Could not create the cleared provider backup.")

    generated_header, generated = database_details(cleared_path)
    if generated["watch_next_rows"] != 0:
        raise ValueError("The prepared restore backup still has Watch Next rows.")
    generated_preserved_counts = {
        name: count
        for name, count in generated["table_row_counts"].items()
        if name != WATCH_NEXT_TABLE
    }
    if generated_preserved_counts != validation["non_watch_next_table_row_counts"]:
        raise ValueError("The prepared backup changed a non-Watch-Next table count.")
    if generated_header["version"] != header["version"]:
        raise ValueError("The prepared backup header version changed unexpectedly.")

    manifest = {
        "format": "android_tv_watch_next_clear_manifest",
        "created_at": dt.datetime.now().astimezone().isoformat(),
        "provider_package": PROVIDER_PACKAGE,
        "table": WATCH_NEXT_TABLE,
        "original_backup": original_path.name,
        "original_backup_sha256": file_sha256(original_path),
        "cleared_backup": cleared_path.name,
        "cleared_backup_sha256": file_sha256(cleared_path),
        "backup_header": {
            "version": header["version"],
            "compressed": bool(header["compressed"]),
            "encryption": header["encryption"],
        },
        "backup_contents": {
            "tar_member_count": member_count,
            "database_members_extracted": extracted,
            "wal_sidecars_removed_after_checkpoint": sidecars_removed,
        },
        "clear_summary": validation,
        "prepared_backup_validation": {
            "watch_next_rows": generated["watch_next_rows"],
            "sqlite_integrity_check": generated["sqlite_integrity_check"],
            "sqlite_quick_check": generated["sqlite_quick_check"],
            "non_watch_next_table_row_counts_preserved": validation[
                "non_watch_next_table_row_counts_preserved"
            ],
        },
        "warning": (
            "Restoring replaces the TV provider data with this snapshot. "
            "Changes made after the original backup may be overwritten."
        ),
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def validate_restore_input(backup_path: Path, manifest_path: Path) -> dict[str, Any]:
    if not backup_path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError("The cleared backup or its manifest is missing.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "android_tv_watch_next_clear_manifest":
        raise ValueError("The manifest is not from this clear Watch Next workflow.")
    if manifest.get("provider_package") != PROVIDER_PACKAGE:
        raise ValueError("The manifest refers to a different provider package.")
    if manifest.get("cleared_backup_sha256") != file_sha256(backup_path):
        raise ValueError("The cleared backup hash does not match its manifest.")
    _, details = database_details(backup_path)
    if details["watch_next_rows"] != 0:
        raise ValueError("Refusing to restore a backup that still has Watch Next rows.")
    if details["sqlite_integrity_check"] != "ok":
        raise ValueError("Refusing to restore a backup with SQLite integrity errors.")
    return manifest


def restore(
    adb: str, device: str, backup_path: Path, manifest_path: Path
) -> None:
    validate_restore_input(backup_path, manifest_path)
    require_device(adb, device)
    print(
        "WARNING: This restores the full TV-provider snapshot and replaces its "
        "current data. Any changes made after the backup may be overwritten."
    )
    expected = f"RESTORE {PROVIDER_PACKAGE}"
    answer = input(f"Type {expected} to continue: ").strip()
    if answer != expected:
        raise RuntimeError("Restore canceled. No restore command was sent.")

    print("Approve the restore prompt on the TV if it appears.")
    result = subprocess.run(
        [adb, "-s", device, "restore", str(backup_path)], check=False
    )
    if result.returncode != 0:
        raise RuntimeError("ADB restore did not complete successfully.")
    print("ADB restore command finished. Run verify to read the TV provider again.")


def verify(adb: str, device: str, backup_path: Path) -> dict[str, Any]:
    capture_provider_backup(adb, device, backup_path)
    _, details = database_details(backup_path)
    report = {
        "checked_at": dt.datetime.now().astimezone().isoformat(),
        "provider_package": PROVIDER_PACKAGE,
        "backup": backup_path.name,
        "backup_sha256": file_sha256(backup_path),
        "table": WATCH_NEXT_TABLE,
        "watch_next_rows": details["watch_next_rows"],
        "sqlite_integrity_check": details["sqlite_integrity_check"],
        "sqlite_quick_check": details["sqlite_quick_check"],
    }
    print(
        f"Fresh TV backup: {details['watch_next_rows']} Watch Next rows, "
        f"SQLite integrity {details['sqlite_integrity_check']}"
    )
    if details["watch_next_rows"] != 0:
        raise RuntimeError(
            "Watch Next rows are present in the fresh TV backup. The TV may "
            "have repopulated the table after restore."
        )
    return report


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser(
        "prepare", help="Capture a fresh provider backup and prepare a cleared copy"
    )
    prepare_parser.add_argument("--device", required=True, help="ADB device serial")
    prepare_parser.add_argument("--adb", help="ADB executable path, defaults to PATH")
    prepare_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("exports/clear-watch-next"),
        help="Output directory, defaults inside the ignored exports directory",
    )

    restore_parser = subparsers.add_parser(
        "restore", help="Restore a prepared cleared provider backup to the TV"
    )
    restore_parser.add_argument("--device", required=True, help="ADB device serial")
    restore_parser.add_argument("--adb", help="ADB executable path, defaults to PATH")
    restore_parser.add_argument("--backup", type=Path, required=True)
    restore_parser.add_argument("--manifest", type=Path, required=True)

    verify_parser = subparsers.add_parser(
        "verify", help="Capture a fresh provider backup and confirm the table is empty"
    )
    verify_parser.add_argument("--device", required=True, help="ADB device serial")
    verify_parser.add_argument("--adb", help="ADB executable path, defaults to PATH")
    verify_parser.add_argument(
        "--backup",
        type=Path,
        default=Path("exports/clear-watch-next/after-restore.ab"),
        help="Path for a fresh local verification backup",
    )
    verify_parser.add_argument(
        "--report",
        type=Path,
        default=Path("exports/clear-watch-next/after-restore.json"),
        help="Path for a verification report",
    )
    return parser


def main() -> int:
    args = make_parser().parse_args()
    try:
        adb = resolve_adb(args.adb)
        if args.command == "prepare":
            manifest = prepare(adb, args.device, args.output_dir)
            print(f"Original backup: {args.output_dir / BACKUP_NAME}")
            print(f"Cleared backup: {args.output_dir / CLEARED_BACKUP_NAME}")
            print(f"Manifest: {args.output_dir / MANIFEST_NAME}")
            print(
                f"Prepared {manifest['clear_summary']['rows_deleted']} deleted rows, "
                "0 remain in the restore backup."
            )
        elif args.command == "restore":
            restore(adb, args.device, args.backup, args.manifest)
        elif args.command == "verify":
            if args.backup.resolve() == args.report.resolve():
                raise ValueError("The verification backup and report must be different files.")
            require_new_files([args.backup, args.report])
            report = verify(adb, args.device, args.backup)
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"Verification report: {args.report}")
    except (
        FileExistsError,
        FileNotFoundError,
        OSError,
        RuntimeError,
        ValueError,
        sqlite3.Error,
        tarfile.TarError,
        zlib.error,
    ) as error:
        print(f"Watch Next clear stopped: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
