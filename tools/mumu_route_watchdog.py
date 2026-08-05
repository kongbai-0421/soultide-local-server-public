"""Continuously restore localized MuMu routes after guest reboot or ADB reconnect."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

try:
    from tools.ensure_mumu_routes import install_routes, inspect_routes
    from tools.resolve_mumu_device import discover, resolve_probe
except ModuleNotFoundError:
    from ensure_mumu_routes import install_routes, inspect_routes
    from resolve_mumu_device import discover, resolve_probe


DEFAULT_PACKAGE = "com.glkj.lhcx.aligames"


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def check_once(
    adb: Path,
    package: str,
    explicit_serial: str,
    server_ip: str,
    strict: bool,
) -> dict[str, object]:
    probes = discover(adb, package)
    selected, aliases = resolve_probe(probes, explicit_serial)
    alias_names = tuple(probe.serial for probe in aliases)
    status = inspect_routes(
        adb,
        selected.serial,
        selected.android_id,
        selected.boot_id,
        alias_names,
        server_ip,
        strict,
    )
    repaired = False
    if not (status.healthy and status.strict_ok):
        install_routes(adb, selected.serial, server_ip, strict)
        status = inspect_routes(
            adb,
            selected.serial,
            selected.android_id,
            selected.boot_id,
            alias_names,
            server_ip,
            strict,
        )
        repaired = True
    if not (status.healthy and status.strict_ok):
        raise RuntimeError(f"route repair did not converge for {selected.serial}")
    return {
        "serial": selected.serial,
        "aliases": alias_names,
        "androidId": selected.android_id,
        "bootId": selected.boot_id,
        "serverIp": server_ip,
        "repaired": repaired,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--adb",
        type=Path,
        default=Path(os.environ.get("SOULTIDE_WATCHDOG_ADB", "")),
    )
    parser.add_argument(
        "--package",
        default=os.environ.get("SOULTIDE_WATCHDOG_PACKAGE", DEFAULT_PACKAGE),
    )
    parser.add_argument(
        "--serial",
        default=os.environ.get("SOULTIDE_WATCHDOG_SERIAL", ""),
    )
    parser.add_argument(
        "--server-ip",
        default=os.environ.get("SOULTIDE_SERVER_IP", ""),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("SOULTIDE_ROUTE_INTERVAL", "5")),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=env_flag("SOULTIDE_ROUTE_STRICT", False),
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if not str(args.adb):
        raise SystemExit("ADB path is required through --adb or SOULTIDE_WATCHDOG_ADB")
    if not args.server_ip:
        raise SystemExit("server IP is required through --server-ip or SOULTIDE_SERVER_IP")
    adb = args.adb.resolve()
    if not adb.is_file():
        raise SystemExit(f"ADB executable does not exist: {adb}")
    last_boot_id = ""
    while True:
        try:
            result = check_once(
                adb,
                args.package,
                args.serial,
                args.server_ip,
                args.strict,
            )
            if result["repaired"] or result["bootId"] != last_boot_id:
                print(json.dumps(result, ensure_ascii=False), flush=True)
            last_boot_id = str(result["bootId"])
        except Exception as exc:
            print(json.dumps({"status": "waiting", "error": str(exc)}, ensure_ascii=False), flush=True)
        if args.once:
            return 0
        time.sleep(max(args.interval, 1.0))


if __name__ == "__main__":
    raise SystemExit(main())
