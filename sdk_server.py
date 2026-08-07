"""Soul Tide local IQI + Bilibili SDK server. Fully offline, no upstream."""

import base64
import hashlib
import http.server
import json
import logging
import os
import time
import urllib.parse
from logging.handlers import RotatingFileHandler


ROOT = os.environ.get("SOULTIDE_ROOT", os.path.dirname(os.path.abspath(__file__)))
LOCAL_UID = os.environ.get("SOULTIDE_LOCAL_UID", "local_uid_12345")
LOCAL_USERNAME = os.environ.get("SOULTIDE_LOCAL_USERNAME", "local_player")
LOCAL_TOKEN = "local_token_" + hashlib.md5(LOCAL_UID.encode("ascii")).hexdigest()
SERVER_IP = os.environ.get(
    "SOULTIDE_SERVER_IP",
    "127.0.0.1" if os.environ.get("SOULTIDE_MOBILE_MODE") == "1" else "192.168.1.136",
)
_BIND_HOST = os.environ.get(
    "SOULTIDE_BIND_HOST",
    "127.0.0.1" if os.environ.get("SOULTIDE_MOBILE_MODE") == "1" else "0.0.0.0",
).strip()
PAYMENT_MODE = os.environ.get("SOULTIDE_PAYMENT_MODE", "local").strip().lower()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SDK] %(message)s",
    handlers=[
        RotatingFileHandler(
            ROOT + r"\sdk_server.log",
            maxBytes=5 * 1024 * 1024,
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("sdk_server")

# ── Rich init response with all config fields a cold-start game needs ──
INIT_RESPONSE = {
    "code": 0,
    "msg": "success",
    "data": {
        "agreements": {
            "list": [
                {
                    "name": "儿童个人信息保护政策",
                    "url": f"http://cdn-sdk.iqigame.com/iqisdk/agreement/child.html",
                },
                {
                    "name": "鬼脸游戏个人信息保护政策",
                    "url": f"http://cdn-sdk.iqigame.com/iqisdk/agreement/privacy.html",
                },
                {
                    "name": "鬼脸游戏用户协议",
                    "url": f"http://cdn-sdk.iqigame.com/iqisdk/agreement/user.html",
                },
            ],
            "version": int(os.environ.get("SOULTIDE_AGREEMENT_VERSION", "7")),
            "switch": True,
        },
        "sdk_switch": {},
        "open_url": {},
    },
}

# ── Agreement HTML templates for the WebView ──
AGREEMENT_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<style>body{{font-family:sans-serif;padding:16px;line-height:1.6;color:#333;max-width:800px;margin:0 auto}}
h1{{color:#1a1a1a;border-bottom:2px solid #eee;padding-bottom:8px}}
p{{text-indent:2em}}</style></head>
<body><h1>{title}</h1>
<p>此为本地离线运行环境，原服务协议不适用于此。</p>
<p>本服务器仅供个人研究使用，不收集任何用户信息。</p>
<p>{content}</p></body></html>"""

AGREEMENTS = {
    "child": {
        "title": "儿童个人信息保护政策",
        "content": "本地离线模式不收集儿童个人信息。",
    },
    "privacy": {
        "title": "鬼脸游戏个人信息保护政策",
        "content": "本地离线模式：所有数据仅存储在本地设备，不会上传至任何服务器。",
    },
    "user": {
        "title": "鬼脸游戏用户协议",
        "content": "本地离线模式：游戏内容仅供个人研究学习使用。",
    },
}


def _now_millis():
    return int(time.time() * 1000)


def _payment_response(path):
    """Return local SDK state without manufacturing a provider receipt."""
    if PAYMENT_MODE == "local":
        return 409, {
            "code": 409,
            "msg": "local payment is handled by the game server",
            "data": {
                "mode": "local",
                "status": "use_local_order_grant",
                "providerReceipt": None,
                "path": path,
            },
        }
    return 503, {
        "code": 503,
        "msg": "external payment is disabled by this local SDK",
        "data": {"mode": PAYMENT_MODE, "status": "disabled", "path": path},
    }


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ── GET handlers ──
    def do_GET(self):
        log.info("GET %s", self.path)
        path = urllib.parse.urlsplit(self.path).path

        # SDK agreement pages (viewed in WebView)
        if "/iqisdk/agreement/" in path:
            name = path.rsplit("/", 1)[-1].replace(".html", "")
            agreement = AGREEMENTS.get(name, AGREEMENTS["privacy"])
            html = AGREEMENT_HTML.format(**agreement)
            self._send_raw(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return

        # Health check endpoint
        if path in ("/health", "/ping"):
            self._send_json({"code": 0, "msg": "pong", "data": {"time": _now_millis()}})
            return

        # Version info
        if path == "/version":
            self._send_json({"code": 0, "msg": "success", "data": {"version": "1.2.7", "build": 200711}})
            return

        self._send_json({"code": 0, "msg": "success", "data": {}})

    # ── POST handlers ──
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""
        path = urllib.parse.urlsplit(self.path).path

        # ── IQI SDK init ──
        if path == "/client/init":
            log.info("POST %s (%db) -> init", path, len(body))
            self._send_json(INIT_RESPONSE)
            return

        # ── IQI SDK check user ──
        if path == "/client/checkUser":
            self._log_sdk_json(body)
            self._send_json({
                "code": 0,
                "msg": "success",
                "data": {
                    "user_info": {
                        "usdk_uid": LOCAL_UID,
                        "usdk_token": LOCAL_TOKEN,
                        "usdk_username": LOCAL_USERNAME,
                    }
                },
            })
            log.info("POST %s -> user %s", path, LOCAL_UID)
            return

        # ── Bilibili cloud-storage config (prevent cold-start timeout) ──
        if "cloud-storage" in path or "cloudStorage" in path:
            log.info("POST %s (%db) -> cloud-storage stub", path, len(body))
            self._send_json({
                "code": 0,
                "msg": "success",
                "data": {
                    "config": {"enable": True, "serverUrl": f"http://{SERVER_IP}:8000/cloud-storage"},
                    "switch": True,
                },
            })
            return

        # ── Payment / order endpoints ──
        if any(x in path for x in ("pay", "order", "recharge", "buy")):
            status, response = _payment_response(path)
            log.warning("POST %s (%db) -> payment refused: %s", path, len(body), response["msg"])
            self._send_json(response, status=status)
            return

        # ── SDK report / analytics ──
        if any(x in path for x in ("report", "analytics", "log", "crash", "track", "upload")):
            log.info("POST %s (%db) -> report ignored (len=%d)", path, len(body), len(body))
            self._send_json({"code": 0, "msg": "success", "data": {}})
            return

        # ── Generic success for anything else ──
        log.info("POST %s (%db) -> generic success", path, len(body))
        self._send_json({"code": 0, "msg": "success", "data": {}})

    # ── PUT handler ──
    def do_PUT(self):
        self.do_POST()

    # ── Helpers ──

    def _log_sdk_json(self, body):
        """Decode and log the base64-encoded JSON from IQI SDK requests."""
        try:
            params = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
            encoded = params.get("data", [""])[0]
            decoded = base64.b64decode(urllib.parse.unquote(encoded)).decode(
                "utf-8", errors="replace"
            )
            log.info("  data: %s", decoded[:500])
        except Exception as exc:
            log.debug("SDK data decode failed: %s", exc)

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self._send_raw(status, body, "application/json; charset=utf-8")

    def _send_raw(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Server", "elb")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    port = int(os.environ.get("SOULTIDE_SDK_PORT", "8000"))
    log.info("SDK server starting on 0.0.0.0:%d (fully local)", port)
    http.server.ThreadingHTTPServer((_BIND_HOST, port), Handler).serve_forever()
