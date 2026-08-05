"""Replace selected already-declared external client files on the emulator.

This is a fast path for re-copying files that are already part of the active
APK manifest. It never changes manifests and rejects embedded/root-1 files;
those changes require an APK install.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath

from sync_nonfull_resources import (
    expected_length,
    expected_md5,
    md5_file,
    push_atomic,
    remote_hashes,
    remote_owner,
    remote_streaming_md5,
    run_adb,
)


DEFAULT_PACKAGE = "com.glkj.lhcx.aligames"
DEFAULT_DEVICE = ""
DEFAULT_ADB = Path("adb.exe")
DEFAULT_RESOURCE_ROOT = Path("offline_cdn") / "Android"


def safe_relative(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe resource path: {value!r}")
    return normalized


def read_paths(values: list[str], paths_file: Path | None) -> list[str]:
    paths = list(values)
    if paths_file:
        paths.extend(
            line.strip()
            for line in paths_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    result = list(dict.fromkeys(safe_relative(path) for path in paths))
    if not result:
        raise ValueError("provide at least one --path or --paths-file")
    return result


def manifest_rows(path: Path) -> dict[str, dict[str, object]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    rows = data.get("AssetBundleList")
    if not isinstance(rows, list):
        raise ValueError(f"manifest has no AssetBundleList: {path}")
    result: dict[str, dict[str, object]] = {}
    for row in rows:
        if isinstance(row, dict):
            relative = safe_relative(str(row.get("RelativePath") or row.get("Name") or ""))
            result[relative] = row
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default=DEFAULT_DEVICE)
    parser.add_argument("--package", default=DEFAULT_PACKAGE)
    parser.add_argument("--adb", type=Path, default=DEFAULT_ADB)
    parser.add_argument("--resource-root", type=Path, default=DEFAULT_RESOURCE_ROOT)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--paths-file", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than zero")
    adb = args.adb.resolve()
    manifest = args.manifest.resolve()
    resource_root = args.resource_root.resolve()
    if not adb.is_file():
        raise FileNotFoundError(adb)
    if not manifest.is_file():
        raise FileNotFoundError(manifest)
    if not resource_root.is_dir():
        raise FileNotFoundError(resource_root)

    import sync_nonfull_resources as sync

    sync._configured_adb_timeout_seconds = args.timeout_seconds
    paths = read_paths(args.path, args.paths_file)
    rows = manifest_rows(manifest)
    selected: dict[str, dict[str, object]] = {}
    embedded: list[str] = []
    missing: list[str] = []
    mismatched: list[str] = []
    for relative in paths:
        row = rows.get(relative)
        if row is None:
            missing.append(relative)
            continue
        if int(row.get("storeRootPathId") or 0) != 2:
            embedded.append(relative)
            continue
        source = resource_root / Path(relative)
        if not source.is_file():
            missing.append(relative)
            continue
        if source.stat().st_size != expected_length(row) or md5_file(source) != expected_md5(row):
            mismatched.append(relative)
            continue
        selected[relative] = row

    if missing:
        raise RuntimeError(f"paths are not present in the selected manifest or host root: {missing}")
    if embedded:
        raise RuntimeError(
            "embedded/root-1 client files cannot be replaced in external storage; "
            f"rebuild and install an APK: {embedded}"
        )
    if mismatched:
        raise RuntimeError(f"host files do not match selected manifest MD5/length: {mismatched}")

    expected_manifest = md5_file(manifest)
    actual_streaming = remote_streaming_md5(adb, args.device, args.package)
    if actual_streaming != expected_manifest.lower():
        raise RuntimeError(
            "device StreamingMd5 does not match the selected manifest; refusing direct replacement "
            f"({actual_streaming} != {expected_manifest.lower()})"
        )
    report = {
        "status": "dry-run" if args.dry_run else "ok",
        "device": args.device,
        "package": args.package,
        "manifest": str(manifest),
        "manifestMd5": expected_manifest,
        "paths": list(selected),
        "replaced": [],
    }
    if not args.dry_run:
        run_adb(adb, args.device, "shell", "am", "force-stop", args.package, check=False)
        package_root = f"/storage/emulated/0/Android/data/{args.package}/files/Android"
        owner = remote_owner(adb, args.device, package_root)
        for relative in selected:
            push_atomic(adb, args.device, resource_root / Path(relative), package_root + "/" + relative, owner)
        verified = remote_hashes(adb, args.device, package_root, list(selected))
        failed = [
            relative
            for relative, row in selected.items()
            if verified.get(relative) != expected_md5(row)
        ]
        if failed:
            raise RuntimeError(f"direct replacement MD5 verification failed: {failed}")
        report["replaced"] = list(selected)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ERROR: {error}")
        raise SystemExit(1)
