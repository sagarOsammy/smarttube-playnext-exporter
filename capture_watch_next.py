#!/usr/bin/env python3
"""Capture an Android TV provider backup with ADB and export SmartTube rows."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import zlib
from pathlib import Path

from tv_provider_export import PACKAGE_LABELS, export_from_backup


PROVIDER_PACKAGE = "com.android.providers.tv"
BACKUP_FILENAME = "com.android.providers.tv.ab"


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


def output_files(output_dir: Path) -> list[Path]:
    files = [
        output_dir / BACKUP_FILENAME,
        output_dir / "tv-watch-next-export-manifest.json",
    ]
    for package_name in PACKAGE_LABELS:
        stem = f"tv-watch-next-{package_name}"
        files.extend(
            (output_dir / f"{stem}.json", output_dir / f"{stem}.csv")
        )
    return files


def capture_and_export(adb: str, device: str, output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = [path.name for path in output_files(output_dir) if path.exists()]
    if existing:
        raise FileExistsError(
            "Refusing to overwrite existing export files: " + ", ".join(existing)
        )

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

    backup_path = output_dir / BACKUP_FILENAME
    print("Approve the Backup my data prompt on the TV to continue.")
    result = subprocess.run(
        [
            adb,
            "-s",
            device,
            "backup",
            "-noapk",
            "-f",
            str(backup_path),
            PROVIDER_PACKAGE,
        ],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("ADB backup did not complete successfully.")
    if not backup_path.is_file() or backup_path.stat().st_size == 0:
        raise RuntimeError("ADB did not create a usable provider backup.")

    return export_from_backup(backup_path, output_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True, help="Connected ADB device serial")
    parser.add_argument("--adb", help="ADB executable path, defaults to adb on PATH")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("exports"),
        help="New or empty output directory, defaults to ./exports",
    )
    args = parser.parse_args()

    try:
        adb = resolve_adb(args.adb)
        manifest = capture_and_export(adb, args.device, args.output_dir)
    except (OSError, RuntimeError, ValueError, sqlite3.Error, tarfile.TarError, zlib.error) as error:
        print(f"Export stopped: {error}", file=sys.stderr)
        return 2

    print(f"Manifest: {args.output_dir / 'tv-watch-next-export-manifest.json'}")
    for package in manifest["packages"]:
        print(
            f"{package['package_name']}: "
            f"{package['browsable_row_count']} browsable rows, "
            f"{package['unique_browsable_video_id_count']} unique videos"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
