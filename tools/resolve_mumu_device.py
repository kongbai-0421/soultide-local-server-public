"""Resolve one physical MuMu instance from potentially duplicated ADB serials.

MuMu may expose the same guest as both ``emulator-N`` and ``127.0.0.1:PORT``.
The resolver probes the installed package, Android ID, and boot ID, groups aliases
that describe the same running guest, and prefers a loopback TCP serial because
it remains stable across host-side emulator port changes.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence


DEFAULT_PACKAGE = "com.glkj.lhcx.aligames"


@dataclass(frozen=True)
class DeviceProbe:
    serial: str
    state: str
    android_id: str
    boot_id: str
    package_path: str

    @property
    def identity(self) -> tuple[str, str]:
        return self.android_id, self.boot_id

    @property
    def has_package(self) -> bool:
        return self.package_path.startswith("package:")


def _run(adb: Path, serial: str | None, *arguments: str) -> tuple[int, str]:
    command = [str(adb)]
    if serial:
        command.extend(("-s", serial))
    command.extend(arguments)
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return completed.returncode, completed.stdout.strip()


def online_serials(adb: Path) -> list[str]:
    code, output = _run(adb, None, "devices")
    if code:
        raise RuntimeError(f"unable to list ADB devices: {output}")
    serials: list[str] = []
    for line in output.splitlines()[1:]:
        match = re.match(r"^([^\s]+)\s+device(?:\s|$)", line.strip())
        if match:
            serials.append(match.group(1))
    return serials


def probe_device(adb: Path, serial: str, package: str) -> DeviceProbe:
    _, state = _run(adb, serial, "get-state")
    if state != "device":
        return DeviceProbe(serial, state, "", "", "")
    _, android_id = _run(adb, serial, "shell", "settings", "get", "secure", "android_id")
    _, boot_id = _run(adb, serial, "shell", "cat", "/proc/sys/kernel/random/boot_id")
    package_code, package_path = _run(adb, serial, "shell", "pm", "path", package)
    if package_code:
        package_path = ""
    return DeviceProbe(
        serial=serial,
        state=state,
        android_id=android_id.strip(),
        boot_id=boot_id.strip(),
        package_path=package_path.strip(),
    )


def serial_preference(serial: str) -> tuple[int, int, str]:
    loopback = re.fullmatch(r"127\.0\.0\.1:(\d+)", serial)
    if loopback:
        return 0, int(loopback.group(1)), serial
    emulator = re.fullmatch(r"emulator-(\d+)", serial)
    if emulator:
        return 1, int(emulator.group(1)), serial
    return 2, 0, serial


def group_instances(probes: Iterable[DeviceProbe]) -> list[list[DeviceProbe]]:
    groups: dict[tuple[str, str], list[DeviceProbe]] = {}
    for probe in probes:
        if probe.state != "device" or not probe.has_package:
            continue
        if not probe.android_id or not probe.boot_id:
            key = ("serial", probe.serial)
        else:
            key = probe.identity
        groups.setdefault(key, []).append(probe)
    return list(groups.values())


def resolve_probe(
    probes: Sequence[DeviceProbe],
    explicit_serial: str = "",
) -> tuple[DeviceProbe, list[DeviceProbe]]:
    package_probes = [probe for probe in probes if probe.state == "device" and probe.has_package]
    if explicit_serial:
        matches = [probe for probe in package_probes if probe.serial == explicit_serial]
        if not matches:
            raise RuntimeError(
                f"explicit ADB serial is not online with the target package: {explicit_serial}"
            )
        selected = matches[0]
        aliases = next(
            (group for group in group_instances(package_probes) if selected in group),
            [selected],
        )
        return selected, sorted(aliases, key=lambda item: serial_preference(item.serial))

    instances = group_instances(package_probes)
    if not instances:
        raise RuntimeError("no online ADB device has the target package installed")
    if len(instances) > 1:
        descriptions = [
            "/".join(sorted(probe.serial for probe in group))
            for group in instances
        ]
        raise RuntimeError(
            "multiple distinct ADB instances have the target package installed: "
            + ", ".join(descriptions)
            + "; pass an explicit serial"
        )
    aliases = sorted(instances[0], key=lambda item: serial_preference(item.serial))
    return aliases[0], aliases


def discover(
    adb: Path,
    package: str,
    serials: Sequence[str] | None = None,
    probe: Callable[[Path, str, str], DeviceProbe] = probe_device,
) -> list[DeviceProbe]:
    candidates = list(serials) if serials is not None else online_serials(adb)
    return [probe(adb, serial, package) for serial in candidates]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--package", default=DEFAULT_PACKAGE)
    parser.add_argument("--serial", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    adb = args.adb.resolve()
    if not adb.is_file():
        raise SystemExit(f"ADB executable does not exist: {adb}")
    probes = discover(adb, args.package)
    try:
        selected, aliases = resolve_probe(probes, args.serial)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    if args.json:
        print(
            json.dumps(
                {
                    "selected": selected.serial,
                    "androidId": selected.android_id,
                    "bootId": selected.boot_id,
                    "aliases": [probe.serial for probe in aliases],
                    "probes": [asdict(probe) for probe in probes],
                },
                ensure_ascii=False,
            )
        )
    else:
        print(selected.serial)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
