"""Run a disposable, no-upstream cold-start check for the local server stack."""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def wait_port(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"port {port} did not become available")


def wait_udp_port(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
                connection.settimeout(0.5)
                connection.sendto(b"\x00" * 12, ("127.0.0.1", port))
                connection.recvfrom(2048)
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"UDP port {port} did not become available")


def http_json(url: str, method: str = "GET", payload: dict | None = None) -> dict:
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def frame(message_id: int, body: bytes = b"") -> bytes:
    return struct.pack("<III", 12 + len(body), message_id, 0) + body


def read_frame(connection: socket.socket) -> tuple[int, bytes]:
    header = b""
    while len(header) < 12:
        chunk = connection.recv(12 - len(header))
        if not chunk:
            raise RuntimeError("TCP peer closed before frame header")
        header += chunk
    total, message_id, _ = struct.unpack("<III", header)
    body = b""
    while len(body) < total - 12:
        chunk = connection.recv(total - 12 - len(body))
        if not chunk:
            raise RuntimeError("TCP peer closed before frame body")
        body += chunk
    return message_id, body


def tlv_string(tag: int, value: str) -> bytes:
    data = value.encode("utf-8")
    if len(data) > 255:
        raise ValueError("test TLV value is unexpectedly large")
    return bytes((tag, len(data))) + data


def dns_query(port: int, hostname: str) -> str:
    query_id = 0x4242
    labels = b"".join(bytes((len(label),)) + label.encode("ascii") for label in hostname.split("."))
    packet = struct.pack(">HHHHHH", query_id, 0x0100, 1, 1, 0, 0) + labels + b"\x00" + struct.pack(">HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        connection.settimeout(5)
        connection.sendto(packet, ("127.0.0.1", port))
        response, _ = connection.recvfrom(2048)
    if response[:2] != struct.pack(">H", query_id) or response[3] != 0x80 or response[7] != 1:
        raise RuntimeError("local DNS did not return one successful answer")
    return socket.inet_ntoa(response[-4:])


def main() -> int:
    ports = {"sdk": 18000, "http": 18081, "tcp": 51131, "dns": 1053}
    processes: list[subprocess.Popen] = []
    with tempfile.TemporaryDirectory(prefix="soultide-offline-") as temp_dir:
        env = os.environ.copy()
        env.update(
            {
                "SOULTIDE_SERVER_IP": "127.0.0.1",
                "SOULTIDE_SDK_PORT": str(ports["sdk"]),
                "SOULTIDE_HTTP_PORT": str(ports["http"]),
                "SOULTIDE_TCP_PORT": str(ports["tcp"]),
                "SOULTIDE_DNS_PORT": str(ports["dns"]),
                "SOULTIDE_DB_PATH": str(Path(temp_dir) / "soultide.db"),
                "SOULTIDE_ALLOW_UPSTREAM": "0",
                "SOULTIDE_CAPTURE_PATH": str(Path(temp_dir) / "must-not-be-read.pcap"),
                "SOULTIDE_RESPONSE_FIXTURE_PATH": str(ROOT / "analysis" / "tcp_offline_responses.json"),
            }
        )
        for script in ("sdk_server.py", "login_server.py", "tcp_server.py", "dns_server.py"):
            output = open(Path(temp_dir) / f"{script}.log", "wb")
            process = subprocess.Popen(
                [sys.executable, "-u", script],
                cwd=ROOT,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            output.close()
            processes.append(process)

        try:
            for port in (ports["sdk"], ports["http"], ports["tcp"]):
                wait_port(port)
            time.sleep(0.5)

            sdk = http_json(f"http://127.0.0.1:{ports['sdk']}/health")
            if sdk.get("code") != 0:
                raise RuntimeError("SDK health check failed")
            client_info = http_json(f"http://127.0.0.1:{ports['http']}/api/clientInfo/?version=0.49.10")
            if client_info.get("code") != "0":
                raise RuntimeError("HTTP clientInfo check failed")
            login = http_json(
                f"http://127.0.0.1:{ports['http']}/login/user_login/",
                "POST",
                {"data": {"cUid": "offline-cold-start", "cName": "offline", "channel_id": "46"}},
            )
            server_uuid = str(login["data"]["uuid"])
            dns_ip = dns_query(ports["dns"], "login-onigao-1.iqigame.com")
            if dns_ip != "127.0.0.1":
                raise RuntimeError(f"local DNS returned {dns_ip}")

            with socket.create_connection(("127.0.0.1", ports["tcp"]), timeout=5) as connection:
                message_id, _ = read_frame(connection)
                if message_id != 3812:
                    raise RuntimeError(f"expected server status 3812, got {message_id}")
                validate_body = b"".join(
                    tlv_string(0xA1, value)
                    for value in (server_uuid, "1121", "2001")
                )
                connection.sendall(frame(3802, validate_body))
                message_id, _ = read_frame(connection)
                if message_id != 3807:
                    raise RuntimeError(f"expected validate result 3807, got {message_id}")
                connection.sendall(frame(3803, tlv_string(0xA1, "local-role")))
                message_id, _ = read_frame(connection)
                if message_id != 3808:
                    raise RuntimeError(f"expected choose result 3808, got {message_id}")
                connection.sendall(frame(3902))
                message_id, body = read_frame(connection)
                if message_id != 3910 or len(body) < 1000:
                    raise RuntimeError(f"expected local loadPlayer 3910, got {message_id} ({len(body)} bytes)")

            result = {
                "status": "ok",
                "upstream": False,
                "dns": dns_ip,
                "tcp": [3812, 3807, 3808, 3910],
                "loadPlayerBytes": len(body),
                "database": env["SOULTIDE_DB_PATH"],
            }
            print(json.dumps(result, ensure_ascii=False))
            return 0
        except Exception:
            for script in ("sdk_server.py", "login_server.py", "tcp_server.py", "dns_server.py"):
                log_path = Path(temp_dir) / f"{script}.log"
                if log_path.exists():
                    print(f"\n--- {script} ---", file=sys.stderr)
                    print(log_path.read_text(encoding="utf-8", errors="replace"), file=sys.stderr)
            raise
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


if __name__ == "__main__":
    raise SystemExit(main())
