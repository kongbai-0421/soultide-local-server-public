"""Synchronize and verify external bundles for the non-full Android APK."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import tarfile
import threading


_configured_adb_timeout_seconds = 180


def run_adb(
    adb: Path,
    device: str,
    *args: str,
    check: bool = True,
    timeout_seconds: int | None = None,
) -> str:
    timeout = (
        _configured_adb_timeout_seconds
        if timeout_seconds is None
        else timeout_seconds
    )
    command = [str(adb), "-s", device, *args]
    result: subprocess.CompletedProcess[str] | None = None
    transient_markers = (
        "daemon",
        "cannot connect",
        "device offline",
        "no devices/emulators",
        "connection reset",
        "transport error",
    )
    for attempt in range(1, 5):
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            rendered = " ".join(command)
            raise RuntimeError(
                f"adb command timed out after {timeout} seconds: {rendered}"
            ) from error
        if result.returncode == 0:
            return result.stdout
        transient = any(marker in result.stdout.lower() for marker in transient_markers)
        if attempt == 4 or (not check and not transient):
            break
        for retry_command in (
            [str(adb), "start-server"],
            [str(adb), "connect", device],
        ):
            try:
                subprocess.run(
                    retry_command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as error:
                rendered = " ".join(retry_command)
                raise RuntimeError(
                    f"adb command timed out after {timeout} seconds: {rendered}"
                ) from error
    assert result is not None
    if check:
        raise RuntimeError(f"adb failed ({result.returncode}): {' '.join(args)}\n{result.stdout}")
    return result.stdout


def md5_file(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest().lower()


def manifest_rows(manifest: Path) -> list[dict[str, object]]:
    data = json.loads(manifest.read_text(encoding="utf-8"))
    rows = data.get("AssetBundleList")
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"manifest has no AssetBundleList: {manifest}")
    return rows


def relative_path(row: dict[str, object]) -> str:
    value = str(row.get("RelativePath") or row.get("Name") or "")
    if not value or value.startswith("/") or ".." in PurePosixPath(value).parts:
        raise ValueError(f"unsafe manifest path: {value!r}")
    return value.replace("\\", "/")


def expected_length(row: dict[str, object]) -> int:
    return int(row.get("Length") or row.get("Size") or row.get("FileSize") or 0)


def expected_md5(row: dict[str, object]) -> str:
    return str(row.get("Md5") or row.get("MD5") or "").lower()


def remote_sizes(
    adb: Path,
    device: str,
    root: str,
    paths: list[str] | None = None,
) -> dict[str, int]:
    if paths is None:
        command = f"find {shlex.quote(root)} -type f -exec stat -c '%n|%s' '{{}}' \\;"
        # MuMu exposes app-specific external directories through ext_data_rw,
        # but nested directories can retain the app UID as their group. The
        # ordinary adb shell user then sees EACCES and returns only a partial
        # inventory. Read through su so verification covers every manifest row.
        output = run_adb(adb, device, "exec-out", "su", "-c", command)
    else:
        full_paths = [root.rstrip("/") + "/" + item for item in paths]
        command = "stat -c '%n|%s' " + " ".join(
            shlex.quote(item) for item in full_paths
        )
        # Missing files are represented by absent entries, allowing the
        # caller to report the exact manifest path that needs repair.
        # Android scoped-storage can hide Android/data from the ordinary
        # adb shell user even when the rooted emulator can read it.
        output = run_adb(
            adb,
            device,
            "exec-out",
            "su",
            "-c",
            command,
            check=False,
        )
        if not any("|" in line for line in output.splitlines()):
            output = run_adb(adb, device, "shell", command, check=False)
    prefix = root.rstrip("/") + "/"
    sizes: dict[str, int] = {}
    for line in output.splitlines():
        path, separator, size = line.rpartition("|")
        if separator and path.startswith(prefix) and size.isdigit():
            sizes[path[len(prefix):]] = int(size)
    return sizes


def remote_sizes_batched(
    adb: Path,
    device: str,
    root: str,
    paths: list[str],
    batch_size: int = 30,
) -> dict[str, int]:
    """Read manifest paths in bounded ADB batches.

    Avoid a recursive find over Android/data: MuMu can stall that scan after a
    large on-device copy even when individual manifest lookups are healthy.
    """
    sizes: dict[str, int] = {}
    for offset in range(0, len(paths), batch_size):
        sizes.update(remote_sizes(adb, device, root, paths[offset:offset + batch_size]))
    return sizes


def remote_hashes(
    adb: Path,
    device: str,
    root: str,
    paths: list[str],
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for offset in range(0, len(paths), 30):
        batch = paths[offset:offset + 30]
        full_paths = [root.rstrip("/") + "/" + item for item in batch]
        command = "md5sum " + " ".join(shlex.quote(item) for item in full_paths)
        # Use root first for the same Android/data scoped-storage case as
        # remote_sizes; fall back to ordinary shell for non-root devices.
        output = run_adb(
            adb,
            device,
            "exec-out",
            "su",
            "-c",
            command,
            check=False,
        )
        if not any(re.match(r"^[0-9A-Fa-f]{32}\s", line.strip()) for line in output.splitlines()):
            output = run_adb(adb, device, "shell", command, check=False)
        for line in output.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or len(parts[0]) != 32:
                continue
            remote_path = parts[1].lstrip("*")
            prefix = root.rstrip("/") + "/"
            if remote_path.startswith(prefix):
                hashes[remote_path[len(prefix):]] = parts[0].lower()
    return hashes


def remote_streaming_md5(adb: Path, device: str, package: str) -> str | None:
    prefs = f"/data/user/0/{package}/shared_prefs/{package}.v2.playerprefs.xml"
    output = run_adb(
        adb,
        device,
        "exec-out",
        "su",
        "-c",
        f"cat {shlex.quote(prefs)}",
        check=False,
    )
    match = re.search(r'<string name="StreamingMd5">([0-9A-Fa-f]{32})</string>', output)
    return match.group(1).lower() if match else None


def remote_owner(adb: Path, device: str, root: str) -> tuple[str, str] | None:
    """Return the existing app directory owner, if Android exposes it."""
    output = run_adb(
        adb,
        device,
        "shell",
        f"stat -c '%U|%G' {shlex.quote(root)}",
        check=False,
    ).strip()
    owner, separator, group = output.partition("|")
    if separator and owner not in {"", "?"} and group not in {"", "?"}:
        return owner, group
    return None


def push_atomic(
    adb: Path,
    device: str,
    source: Path,
    destination: str,
    owner: tuple[str, str] | None = None,
) -> None:
    """Push through a shell-owned staging file, then copy as root.

    Android's adb daemon tries to fchown files created directly below an
    app-specific external directory. On MuMu that operation is rejected even
    though the bytes were transferred. A root-side copy avoids that daemon
    limitation and keeps the destination replacement atomic.
    """
    parent = str(PurePosixPath(destination).parent)
    # App-specific external roots reject ordinary shell mkdir on MuMu even
    # when the directory already exists. Use the rooted shell first so a
    # missing parent can also be created without changing ownership.
    root_mkdir = f"mkdir -p {shlex.quote(parent)}"
    try:
        run_adb(adb, device, "shell", f"su -c {shlex.quote(root_mkdir)}")
    except RuntimeError:
        run_adb(adb, device, "shell", root_mkdir)
    token = hashlib.sha256(destination.encode("utf-8")).hexdigest()[:16]
    remote_temporary = f"/data/local/tmp/soultide-sync-{token}-{source.name}"
    temporary = destination + ".codex-sync"
    try:
        run_adb(adb, device, "push", str(source), remote_temporary)
    except Exception:
        run_adb(
            adb,
            device,
            "shell",
            f"rm -f {shlex.quote(remote_temporary)}",
            check=False,
        )
        raise

    copy_command = (
        f"rm -f {shlex.quote(temporary)}; "
        f"cp {shlex.quote(remote_temporary)} {shlex.quote(temporary)}; "
    )
    if owner:
        copy_command += (
            f"chown {shlex.quote(owner[0])}:{shlex.quote(owner[1])} "
            f"{shlex.quote(temporary)}; "
        )
    copy_command += (
        f"chmod 660 {shlex.quote(temporary)}; "
        f"mv -f {shlex.quote(temporary)} {shlex.quote(destination)}; "
        f"rm -f {shlex.quote(remote_temporary)}"
    )
    try:
        run_adb(
            adb,
            device,
            "shell",
            f"su -c {shlex.quote(copy_command)}",
        )
    except RuntimeError as root_error:
        # A non-root emulator may still allow shell to copy into the package
        # directory through ext_data_rw. This fallback leaves the file
        # readable even though it cannot restore the app UID ownership.
        shell_copy = copy_command.replace(
            f"chown {shlex.quote(owner[0])}:{shlex.quote(owner[1])} "
            f"{shlex.quote(temporary)}; " if owner else "",
            "",
        )
        try:
            run_adb(
                adb,
                device,
                "shell",
                shell_copy,
            )
        except RuntimeError:
            run_adb(
                adb,
                device,
                "shell",
                f"rm -f {shlex.quote(remote_temporary)}",
                check=False,
            )
            raise root_error


def write_tar_stream(stream: object, root: Path, paths: list[str]) -> None:
    """Write only the selected resources to an uncompressed streaming tar."""
    with tarfile.open(
        fileobj=stream,
        mode="w|",
        format=tarfile.USTAR_FORMAT,
    ) as archive:
        for relative in paths:
            archive.add(root / Path(relative), arcname=relative, recursive=False)


def push_tar_batch(
    adb: Path,
    device: str,
    root: Path,
    package_root: str,
    paths: list[str],
    owner: tuple[str, str] | None = None,
) -> None:
    """Stream many resources through one ADB connection without host temp files.

    Per-file adb push is reliable for small repairs but prohibitively slow when
    ResourceChecker has removed hundreds of bundles. Feed an uncompressed tar
    directly to the device-side extractor instead of creating and deleting a
    multi-gigabyte fixed temporary file. A disconnected stream may leave a
    partial last file, but the caller's full size and MD5 passes repair it.
    """
    if not paths:
        return

    extract = f"set -e; tar -xf - -C {shlex.quote(package_root)}"
    if owner:
        extract += (
            f"; chown -R {shlex.quote(owner[0])}:{shlex.quote(owner[1])} "
            f"{shlex.quote(package_root)}"
        )
    process = subprocess.Popen(
        [str(adb), "-s", device, "exec-in", "su", "-c", extract],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if process.stdin is None or process.stdout is None:
        process.kill()
        process.wait()
        raise RuntimeError("adb tar stream did not expose stdin/stdout pipes")

    output_tail = bytearray()

    def drain_output() -> None:
        while chunk := process.stdout.read(65536):
            output_tail.extend(chunk)
            if len(output_tail) > 1048576:
                del output_tail[:-1048576]

    output_reader = threading.Thread(target=drain_output, daemon=True)
    output_reader.start()
    try:
        write_tar_stream(process.stdin, root, paths)
        process.stdin.close()
        return_code = process.wait()
        output_reader.join()
    except Exception:
        if not process.stdin.closed:
            process.stdin.close()
        process.kill()
        process.wait()
        output_reader.join(timeout=5)
        raise

    output = bytes(output_tail).decode("utf-8", errors="replace")
    if return_code != 0:
        raise RuntimeError(
            f"adb tar stream failed ({return_code}) on {device}:\n{output}"
        )


def tar_batches(root: Path, paths: list[str], max_bytes: int = 500_000_000) -> list[list[str]]:
    """Split repairs into bounded streams tolerated by MuMu's ADB bridge.

    Long exec-in streams around 0.8-0.9 GiB have been observed to stop making
    progress while both host and device processes remain alive. Keep each batch
    below 500 MB so a retry loses little work and avoids that bridge limit.
    """
    batches: list[list[str]] = []
    current: list[str] = []
    current_bytes = 0
    for relative in paths:
        size = (root / Path(relative)).stat().st_size
        if current and current_bytes + size > max_bytes:
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(relative)
        current_bytes += size
    if current:
        batches.append(current)
    return batches


def validate_host(root: Path, rows: list[dict[str, object]]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    total = len(rows)
    for index, row in enumerate(rows, 1):
        relative = relative_path(row)
        source = root / Path(relative)
        if not source.is_file():
            raise FileNotFoundError(f"manifest resource is missing: {source}")
        length = expected_length(row)
        if length and source.stat().st_size != length:
            raise ValueError(
                f"manifest length mismatch: {relative} "
                f"({source.stat().st_size} != {length})"
            )
        digest = md5_file(source)
        expected = expected_md5(row)
        if expected and digest != expected:
            raise ValueError(f"manifest MD5 mismatch: {relative} ({digest} != {expected})")
        hashes[relative] = digest
        if index % 50 == 0 or index == total:
            print(f"Host verification: {index}/{total}", flush=True)
    return hashes


def prepare_external_directories(
    adb: Path,
    device: str,
    package_root: str,
    rows: list[dict[str, object]],
    owner: tuple[str, str] | None,
) -> None:
    """Create and normalize every parent before the first file push."""
    root_path = PurePosixPath(package_root)
    directories: set[str] = set()
    for row in rows:
        parent = root_path / PurePosixPath(relative_path(row)).parent
        relative_parent = parent.relative_to(root_path)
        current = root_path
        for part in relative_parent.parts:
            current /= part
            directories.add(str(current))
    if not directories:
        return

    directory_args = " ".join(shlex.quote(item) for item in sorted(directories))
    run_adb(adb, device, "shell", f"mkdir -p {directory_args}")
    if owner:
        command = (
            f"chown {shlex.quote(owner[0])}:{shlex.quote(owner[1])} "
            f"{directory_args}"
        )
        run_adb(
            adb,
            device,
            "shell",
            f"su -c {shlex.quote(command)}",
            check=False,
        )


def synchronize(args: argparse.Namespace) -> int:
    global _configured_adb_timeout_seconds
    adb_timeout_seconds = getattr(args, "adb_timeout_seconds", 180)
    if adb_timeout_seconds <= 0:
        raise ValueError("--adb-timeout-seconds must be greater than zero")
    _configured_adb_timeout_seconds = adb_timeout_seconds
    adb = args.adb.resolve()
    if args.max_repairs < 0:
        raise ValueError("--max-repairs must be zero or greater")
    if not adb.is_file():
        raise FileNotFoundError(f"adb does not exist: {adb}")
    root = args.resource_root.resolve()
    rows = manifest_rows(args.manifest)
    external = [row for row in rows if int(row.get("storeRootPathId") or 0) == 2]
    if not external:
        raise ValueError("manifest contains no external storeRootPathId=2 resources")

    package_root = f"/storage/emulated/0/Android/data/{args.package}/files/Android"
    media_count = sum(relative_path(row).lower().endswith((".mp4", ".webm", ".mov")) for row in external)
    expected_manifest_hash = md5_file(args.manifest)
    streaming_md5 = remote_streaming_md5(adb, args.device, args.package)
    if not streaming_md5:
        raise RuntimeError(
            "client StreamingMd5 is unavailable; complete the guarded first-launch "
            "handshake before synchronizing external resources"
        )
    if streaming_md5 != expected_manifest_hash:
        raise RuntimeError(
            "client StreamingMd5 does not match the selected manifest "
            f"({streaming_md5} != {expected_manifest_hash}); refusing to publish "
            "resources that ResourceChecker may delete on the next launch"
        )
    if args.quick_verify:
        external_paths = [relative_path(row) for row in external]
        sizes = remote_sizes_batched(adb, args.device, package_root, external_paths)
        invalid = [
            relative_path(row)
            for row in external
            if sizes.get(relative_path(row)) != expected_length(row)
        ]
        manifest_hashes = remote_hashes(
            adb, args.device, package_root, ["version.json"]
        )
        if invalid:
            sample = ", ".join(invalid[:10])
            raise RuntimeError(
                f"post-launch resource guard found {len(invalid)} missing/size-mismatched files: {sample}"
            )
        if manifest_hashes.get("version.json") != expected_manifest_hash:
            raise RuntimeError(
                "post-launch version.json hash mismatch "
                f"({manifest_hashes.get('version.json')} != {expected_manifest_hash})"
            )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "mode": "quick-verify",
                    "external": len(external),
                    "media": media_count,
                    "manifest": expected_manifest_hash,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0
    print(
        f"Validating {len(external)} external resources ({media_count} media files)...",
        flush=True,
    )
    host_hashes = validate_host(root, external)
    run_adb(adb, args.device, "shell", "am", "force-stop", args.package)
    owner = remote_owner(adb, args.device, package_root)
    if owner:
        # A previous failed direct adb push may have created new parents as
        # shell-owned. Normalize directory ownership before copying files.
        owner_command = (
            f"chown -R {shlex.quote(owner[0])}:{shlex.quote(owner[1])} "
            f"{shlex.quote(package_root)}"
        )
        run_adb(
            adb,
            args.device,
            "shell",
            f"su -c {shlex.quote(owner_command)}",
            check=False,
        )
    prepare_external_directories(adb, args.device, package_root, external, owner)

    repaired: set[str] = set()
    external_paths = [relative_path(row) for row in external]
    for attempt in range(1, 4):
        sizes = remote_sizes_batched(adb, args.device, package_root, external_paths)
        candidates = [
            relative_path(row)
            for row in external
            if sizes.get(relative_path(row)) != expected_length(row)
        ]
        correctly_sized = [
            relative_path(row)
            for row in external
            if sizes.get(relative_path(row)) == expected_length(row)
        ]
        print(
            f"Device pass {attempt}: {len(candidates)} missing/size-mismatched, "
            f"hashing {len(correctly_sized)} present resources...",
            flush=True,
        )
        hashes = remote_hashes(adb, args.device, package_root, correctly_sized)
        candidates.extend(
            item for item in correctly_sized if hashes.get(item) != host_hashes[item]
        )
        candidates = list(dict.fromkeys(candidates))
        if not candidates:
            break
        if attempt == 3:
            sample = ", ".join(candidates[:10])
            raise RuntimeError(
                f"device resources still differ after two repair passes: "
                f"{len(candidates)} invalid ({sample})"
            )
        repair_limit = len(candidates)
        if args.max_repairs:
            repair_limit = min(repair_limit, args.max_repairs - len(repaired))
        selected = candidates[:repair_limit]
        # MuMu's adb exec-in stream has repeatedly stopped making progress at
        # non-deterministic offsets (observed near 154 MB and 873 MB) while both
        # endpoints stayed alive. Keep the tar implementation for diagnostics,
        # but production repair uses independent atomic pushes so every finished
        # resource is durable and the next pass can resume exactly.
        for index, relative in enumerate(selected, 1):
            destination = package_root + "/" + relative
            print(f"Repairing {index}/{len(selected)}: {relative}", flush=True)
            push_atomic(adb, args.device, root / Path(relative), destination, owner)
            repaired.add(relative)
        if args.max_repairs and len(repaired) >= args.max_repairs:
            remaining = len(candidates) - repair_limit
            print(
                json.dumps(
                    {
                        "status": "partial",
                        "external": len(external),
                        "media": media_count,
                        "repaired": len(repaired),
                        "remainingAtLeast": remaining,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return 2

    # External LuaJIT copies shadow the APK-embedded patched bundles even though
    # the mixed manifest declares them as root 1. Remove only these two known
    # architecture files after every root-2 resource has passed size and MD5.
    for relative in (
        "16_luaab/luajit/luajit_base.ab.x64",
        "16_luaab/luajit/luajit_base.ab.x86",
    ):
        row = next(
            (item for item in rows if relative_path(item) == relative),
            None,
        )
        if row is not None and int(row.get("storeRootPathId") or 0) != 1:
            continue
        destination = package_root.rstrip("/") + "/" + relative
        command = f"rm -f {shlex.quote(destination)}"
        run_adb(
            adb,
            args.device,
            "exec-out",
            "su",
            "-c",
            command,
            check=False,
        )

    # Publish the mixed-root manifest only after every external resource is valid.
    for name in ("version.json", "version-remote.json", "version.json.bak"):
        push_atomic(
            adb,
            args.device,
            args.manifest.resolve(),
            package_root + "/" + name,
            owner,
        )
    if owner:
        run_adb(
            adb,
            args.device,
            "shell",
            f"chown -R {shlex.quote(owner[0])}:{shlex.quote(owner[1])} {shlex.quote(package_root)} 2>/dev/null || true",
            check=False,
        )
    published_hashes = remote_hashes(adb, args.device, package_root, ["version.json"])
    if published_hashes.get("version.json") != expected_manifest_hash:
        raise RuntimeError(
            "published version.json hash mismatch "
            f"({published_hashes.get('version.json')} != {expected_manifest_hash})"
        )
    print(
        json.dumps(
            {
                "status": "ok",
                "external": len(external),
                "media": media_count,
                "repaired": len(repaired),
                "manifest": expected_manifest_hash,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="127.0.0.1:16416")
    parser.add_argument("--package", default="com.glkj.lhcx.aligames")
    parser.add_argument(
        "--adb",
        type=Path,
        default=Path(r"C:\Program Files\Netease\MuMu\nx_device\12.0\shell\adb.exe"),
    )
    parser.add_argument(
        "--resource-root",
        type=Path,
        default=Path(r"E:\A灵魂潮汐\服务器\offline_cdn\Android"),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(r"E:\A灵魂潮汐\服务器\offline_cdn\Android\version-local-nonfull.json"),
    )
    parser.add_argument("--quick-verify", action="store_true")
    parser.add_argument(
        "--adb-timeout-seconds",
        type=int,
        default=180,
        help="timeout for each adb command and retry helper command",
    )
    parser.add_argument(
        "--max-repairs",
        type=int,
        default=0,
        help="repair at most this many files and exit 2 without publishing the manifest; 0 means unlimited",
    )
    return synchronize(parser.parse_args())


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
