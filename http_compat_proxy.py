"""Forward the game's port-80 HTTP traffic to the local login/CDN service."""

from __future__ import annotations

import http.client
import logging
import os
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler


ROOT = os.environ.get("SOULTIDE_ROOT", os.path.dirname(os.path.abspath(__file__)))
BACKEND_HOST = os.environ.get("SOULTIDE_HTTP_BACKEND_HOST", "127.0.0.1")
BACKEND_PORT = int(os.environ.get("SOULTIDE_HTTP_BACKEND_PORT", "8081"))
LISTEN_HOST = os.environ.get("SOULTIDE_HTTP_COMPAT_HOST") or os.environ.get(
    "SOULTIDE_SERVER_IP", "0.0.0.0"
)
LISTEN_PORT = int(os.environ.get("SOULTIDE_HTTP_COMPAT_PORT", "80"))

log = logging.getLogger("http_compat_proxy")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [HTTP80] %(message)s",
    handlers=[
        RotatingFileHandler(
            os.path.join(ROOT, "http_compat_proxy.log"),
            maxBytes=2 * 1024 * 1024,
            backupCount=2,
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self) -> None:
        self._forward()

    def do_HEAD(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def do_PUT(self) -> None:
        self._forward()

    def do_PATCH(self) -> None:
        self._forward()

    def do_DELETE(self) -> None:
        self._forward()

    def _forward(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length) if content_length else None
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.lower() not in HOP_BY_HOP and name.lower() != "host"
            }
            headers["Host"] = self.headers.get("Host", "")

            upstream = http.client.HTTPConnection(
                BACKEND_HOST, BACKEND_PORT, timeout=90
            )
            upstream.request(self.command, self.path, body=body, headers=headers)
            response = upstream.getresponse()

            self.send_response(response.status, response.reason)
            for name, value in response.getheaders():
                if name.lower() in HOP_BY_HOP:
                    continue
                self.send_header(name, value)
            self.end_headers()

            if self.command != "HEAD":
                shutil.copyfileobj(response, self.wfile, length=64 * 1024)
            upstream.close()
            log.info("%s %s -> %s (%s)", self.command, self.path, response.status, self.client_address[0])
        except (BrokenPipeError, ConnectionResetError):
            log.info("client disconnected during %s %s", self.command, self.path)
        except Exception as exc:
            log.warning("proxy failure %s %s: %s", self.command, self.path, exc)
            try:
                self.send_error(502, "Local HTTP backend unavailable")
            except Exception:
                pass

    def log_message(self, _format: str, *_args: object) -> None:
        return


class ProxyServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    server = ProxyServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    log.info(
        "HTTP compatibility proxy listening on %s:%d -> %s:%d",
        LISTEN_HOST,
        LISTEN_PORT,
        BACKEND_HOST,
        BACKEND_PORT,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
