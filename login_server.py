"""
Soul Tide local HTTP login and resource server.

Offline mode is the default. SOULTIDE_ALLOW_UPSTREAM=1 is only for diagnostics.
"""

import hashlib
import json
import logging
import mimetypes
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

import storage


ROOT = Path(os.environ.get("SOULTIDE_ROOT", Path(__file__).resolve().parent)).resolve()
ASSET_ROOT = Path(
    os.environ.get(
        "SOULTIDE_ASSET_ROOT",
        str(ROOT / "offline_cdn" / "Android"),
    )
)
# In the single-APK build the full media mirror stays with the game external
# resources. Keep the private mirror as a fallback, but do not duplicate
# several GiB of video into app-private storage.
GAME_ASSET_ROOT = Path(
    os.environ.get(
        "SOULTIDE_GAME_ASSET_ROOT",
        str(ASSET_ROOT),
    )
)
_configured_manifest = os.environ.get("SOULTIDE_LOCAL_MANIFEST", "").strip()
BUNDLED_MANIFEST = ROOT / "version-local-default.json"
if _configured_manifest:
    # An explicit manifest is useful for diagnostics and official-list
    # compatibility.  Keep that override, but make the non-full package
    # manifest the normal choice whenever it is present.
    LOCAL_MANIFEST = Path(_configured_manifest)
else:
    _active_manifest = ASSET_ROOT / "version-local-active.json"
    _built_manifest = ROOT / "apk_build" / "version-local-nonfull-built.json"
    _nonfull_manifest = ASSET_ROOT / "version-local-nonfull.json"
    if _active_manifest.is_file():
        # A Lua hot-update release publishes this pointer last.  Keeping the
        # active manifest separate from the APK build manifest lets the
        # external client update without making the installer reject the
        # still-installed APK as a version mismatch.
        LOCAL_MANIFEST = _active_manifest
    elif _built_manifest.is_file():
        LOCAL_MANIFEST = _built_manifest
    elif _nonfull_manifest.is_file():
        LOCAL_MANIFEST = _nonfull_manifest
    elif BUNDLED_MANIFEST.is_file():
        # The service APK carries this small bootstrap manifest so the game can
        # complete version negotiation before a full external resource pack is
        # imported into SOULTIDE_ASSET_ROOT.
        LOCAL_MANIFEST = BUNDLED_MANIFEST
    else:
        LOCAL_MANIFEST = ASSET_ROOT / "version-local.json"
ALLOW_UPSTREAM = (
    os.environ.get("SOULTIDE_ALLOW_UPSTREAM", "0") == "1"
    and os.environ.get("SOULTIDE_MOBILE_MODE", "0") != "1"
)
CDN_UPSTREAM_FALLBACK = os.environ.get(
    "SOULTIDE_CDN_UPSTREAM_FALLBACK",
    "1" if ALLOW_UPSTREAM else "0",
) == "1" and ALLOW_UPSTREAM
CDN_UPSTREAM_ROOT = os.environ.get(
    "SOULTIDE_CDN_UPSTREAM_ROOT",
    "http://cdn-onigao-1.iqigame.com/Onigao/Update/resources",
).rstrip("/")
MOBILE_LOCAL_MODE = os.environ.get("SOULTIDE_MOBILE_MODE") == "1"
LOCAL_CHANNEL_UID = os.environ.get("SOULTIDE_LOCAL_UID", "local-test-dollmaster")
LOCAL_USERNAME = os.environ.get("SOULTIDE_LOCAL_USERNAME", "人偶师")
UPDATE_MODE_PATH = ROOT / "update_mode.json"
VERSION_UPSTREAM_URL = os.environ.get(
    "SOULTIDE_VERSION_UPSTREAM_URL",
    "http://cdn-onigao-1.iqigame.com/Onigao/Update/version-Android.txt",
).strip()
UPSTREAM_PROXY = os.environ.get(
    "SOULTIDE_UPSTREAM_PROXY",
    "",
).strip() or None
HTTP_PORT = int(os.environ.get("SOULTIDE_HTTP_PORT", "8081"))
_BIND_HOST = os.environ.get(
    "SOULTIDE_BIND_HOST",
    "127.0.0.1" if os.environ.get("SOULTIDE_MOBILE_MODE") == "1" else "0.0.0.0",
).strip()

CONFIG = {
    "server_ip": os.environ.get(
        "SOULTIDE_SERVER_IP",
        "127.0.0.1" if os.environ.get("SOULTIDE_MOBILE_MODE") == "1" else "192.168.1.136",
    ),
    "tcp_port": int(os.environ.get("SOULTIDE_TCP_PORT", "51121")),
    "server_id": "1121",
    "area_id": "101",
    "area_name": "新月",
    "server_name": "新月",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [HTTP] %(message)s",
    handlers=[
        RotatingFileHandler(
            ROOT / "server.log",
            maxBytes=10 * 1024 * 1024,
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("soultide")
app = FastAPI()
_RECENT_HTTP_ACCOUNTS = {}


def _json_or_text(value):
    if not isinstance(value, str):
        return value
    text = unquote(value).strip()
    if not text:
        return text
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _decode_request_payload(body):
    text = body.decode("utf-8", errors="replace").strip()
    parsed = _json_or_text(text)
    if isinstance(parsed, (dict, list)):
        return parsed
    values = parse_qs(text, keep_blank_values=True)
    if not values:
        return {}
    return {
        key: _json_or_text(rows[-1] if rows else "")
        for key, rows in values.items()
    }


def _payload_values(value, names):
    wanted = {str(name).lower() for name in names}
    found = []

    def walk(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if str(key).lower() in wanted and child not in (None, ""):
                    found.append(child)
                walk(_json_or_text(child))
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return found


def _payload_int(value, names):
    for candidate in _payload_values(value, names):
        try:
            parsed = int(candidate)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return 0


def _recharge_plan(payload):
    import module_handlers
    import module_rules

    mall_id = _payload_int(payload, (
        "mallId", "mall_id", "mallCid", "mall_cid",
    ))
    ambiguous_product_id = _payload_int(payload, (
        "goodsId", "goods_id", "productId", "product_id",
    ))
    pay_id = _payload_int(payload, (
        "payMoney", "pay_money", "payId", "pay_id", "rechargeId", "recharge_id",
    ))
    row = module_handlers._mall_row(mall_id) if mall_id else None
    if row is None and ambiguous_product_id:
        mall_id = ambiguous_product_id
        row = module_handlers._mall_row(mall_id)
        if row is None:
            pay_id = ambiguous_product_id
    if row is None and pay_id:
        pay_row = module_rules._row("mall", "PayTable", pay_id)
        mall_id = int((pay_row or {}).get("MallID", 0) or 0)
        row = module_handlers._mall_row(mall_id) if mall_id else None
        if row is not None:
            # Reaching this endpoint proves the client already displayed and
            # selected the PayTable product. Historical event windows and
            # server-only conditions must not reject that local purchase.
            row = dict(row)
            row.update({"TimeLimitType": 0, "ConditionId": 0, "ShowConditionId": 0})
    if row is None or int(row.get("SellType", 0) or 0) != 3:
        return None
    period = module_handlers._mall_period(row)
    plan = module_handlers._mall_purchase_plan("", row, 1, period)
    if plan is None or plan.get("kind") != "offline_payment":
        return None
    return int(row.get("Id", mall_id)), period, plan


def _payload_shape(value, depth=0):
    """Return key/type metadata without logging request values."""
    if depth >= 5:
        return type(value).__name__
    if isinstance(value, dict):
        return {
            str(key)[:80]: _payload_shape(_json_or_text(child), depth + 1)
            for key, child in list(value.items())[:80]
        }
    if isinstance(value, list):
        return {
            "type": "list",
            "length": len(value),
            "items": [_payload_shape(child, depth + 1) for child in value[:3]],
        }
    if isinstance(value, str):
        return {"type": "str", "length": len(value)}
    return type(value).__name__


def _recharge_account(payload, client_host):
    identity_names = (
        "uid", "userId", "user_id", "uuid", "accountId", "account_id",
        "roleId", "role_id", "roleUid", "role_uid", "playerId", "player_id",
        "cUid", "c_uid", "channelUid", "channel_uid",
    )
    for identity in _payload_values(payload, identity_names):
        account = storage.get_account_by_identity(identity)
        if account is not None:
            return account
    recent = _RECENT_HTTP_ACCOUNTS.get(str(client_host))
    if recent and int(time.time()) - int(recent[1]) <= 600:
        account = storage.get_account_by_identity(recent[0])
        if account is not None:
            return account
    return storage.get_configured_alias_account()


def offline_success(data=None):
    return {"code": 0, "msg": "success", "data": data or {}}


def _range_bounds(value: str, size: int):
    """Return an inclusive byte range for a single HTTP Range header."""
    if not value.lower().startswith("bytes=") or "," in value:
        return None
    spec = value[6:].strip()
    if "-" not in spec:
        return None
    start_text, end_text = spec.split("-", 1)
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0:
                return None
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
            if start < 0 or start >= size or end < start:
                return None
            end = min(end, size - 1)
        return start, end
    except ValueError:
        return None


@app.get("/api/clientInfo/")
async def client_info(version: str = "", channelId: str = "", packageName: str = ""):
    log.info("clientInfo v=%s ch=%s pkg=%s", version, channelId, packageName)
    return {"msg": "ok", "code": "0", "submitMode": "0"}


@app.get("/Onigao/Update/version-Android.txt")
async def version_check(request: Request):
    manifest = LOCAL_MANIFEST if LOCAL_MANIFEST.is_file() else ASSET_ROOT / "version-remote.json"
    mode = os.environ.get("SOULTIDE_UPDATE_MODE", "").strip().lower()
    if not mode and UPDATE_MODE_PATH.exists():
        try:
            mode = str(json.loads(UPDATE_MODE_PATH.read_text(encoding="utf-8")).get("mode", ""))
        except (OSError, ValueError, TypeError):
            mode = ""
    mode = mode if mode in {"local", "official"} else "local"
    if mode == "official":
        try:
            import httpx

            client_kwargs = {"timeout": 20, "follow_redirects": True, "trust_env": False}
            if UPSTREAM_PROXY:
                client_kwargs["proxy"] = UPSTREAM_PROXY
            async with httpx.AsyncClient(**client_kwargs) as client:
                upstream = await client.get(VERSION_UPSTREAM_URL)
            if upstream.status_code < 400 and upstream.content:
                log.info("official version list returned (%db)", len(upstream.content))
                return Response(
                    content=upstream.content,
                    status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type", "text/plain"),
                    headers={"Access-Control-Allow-Origin": "*"},
                )
            log.warning("official version list failed: HTTP %s; using local list", upstream.status_code)
        except Exception as exc:
            log.warning("official version list unavailable; using local list: %s", exc)
    game_version = "0.49.10"
    resource_version = 0
    if manifest.exists():
        try:
            manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
            game_version = str(manifest_data.get("ApplicableGameVersion") or game_version)
            resource_version = int(manifest_data.get("InternalResourceVersion") or 0)
        except (OSError, ValueError, TypeError):
            log.warning("local resource manifest is invalid: %s", manifest)
    response = {
        "LatestGameVersion": game_version,
        "InternalResourceVersion": resource_version,
        "VersionListLength": manifest.stat().st_size if manifest.exists() else 0,
        "UpdateMode": mode,
    }
    return JSONResponse(
        content=response,
        media_type="text/plain",
        headers={"Access-Control-Allow-Origin": "*"},
    )


@app.api_route(
    "/Onigao/Update/resources/{resource_version}/Android/{resource_path:path}",
    methods=["GET", "HEAD"],
)
async def local_cdn(resource_version: str, resource_path: str, request: Request):
    relative = Path(unquote(resource_path.replace("\\", "/")))
    manifest_request = relative.as_posix().lower() == "version.json"
    if manifest_request and LOCAL_MANIFEST.is_file():
        candidate = LOCAL_MANIFEST.resolve()
    else:
        if manifest_request:
            relative = Path("version-remote.json")
        candidate = (ASSET_ROOT / relative).resolve()
    root = ASSET_ROOT.resolve()
    if not manifest_request or candidate != LOCAL_MANIFEST.resolve():
        try:
            candidate.relative_to(root)
        except ValueError:
            return JSONResponse({"code": 403, "msg": "invalid path"}, status_code=403)

    if not candidate.is_file():
        log.warning("CDN missing: %s", relative.as_posix())
        # The local mirror is authoritative.  A missing file may optionally be
        # fetched from the official CDN; the marker prevents a DNS override of
        # the official hostname from recursively entering this handler.
        if CDN_UPSTREAM_FALLBACK and request.headers.get("x-soultide-upstream") != "1":
            try:
                import httpx

                headers = {"X-Soultide-Upstream": "1"}
                requested_range = request.headers.get("range")
                if requested_range:
                    headers["Range"] = requested_range
                client_kwargs = {
                    "timeout": 20,
                    "follow_redirects": True,
                    "trust_env": False,
                }
                if UPSTREAM_PROXY:
                    client_kwargs["proxy"] = UPSTREAM_PROXY
                async with httpx.AsyncClient(**client_kwargs) as client:
                    upstream_base = f"{CDN_UPSTREAM_ROOT}/{resource_version}/Android"
                    upstream = await client.get(f"{upstream_base}/{relative.as_posix()}", headers=headers)
                if upstream.status_code < 400:
                    # Cache only complete responses. A partial response must
                    # never replace a valid local bundle with a truncated file.
                    if upstream.status_code == 200 and not requested_range and request.method == "GET":
                        candidate.parent.mkdir(parents=True, exist_ok=True)
                        temp = candidate.with_name(candidate.name + ".part")
                        temp.write_bytes(upstream.content)
                        temp.replace(candidate)
                        log.info(
                            "official CDN cached via %s: %s (%db)",
                            UPSTREAM_PROXY or "direct",
                            relative.as_posix(),
                            len(upstream.content),
                        )
                    return Response(
                        content=upstream.content,
                        status_code=upstream.status_code,
                        media_type=upstream.headers.get("content-type"),
                        headers={
                            "Cache-Control": "public, max-age=31536000",
                            "Accept-Ranges": upstream.headers.get("accept-ranges", "bytes"),
                            "Content-Range": upstream.headers.get("content-range", ""),
                        },
                    )
            except Exception as exc:
                log.info("official CDN unavailable; skipping %s: %s", relative.as_posix(), exc)
        return JSONResponse({"code": 404, "msg": "resource not found"}, status_code=404)

    content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    log.info("CDN local: %s (%db)", relative.as_posix(), candidate.stat().st_size)
    range_header = request.headers.get("range")
    if range_header and request.method == "GET":
        size = candidate.stat().st_size
        bounds = _range_bounds(range_header, size)
        if bounds is None:
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
            )
        start, end = bounds
        with candidate.open("rb") as stream:
            stream.seek(start)
            content = stream.read(end - start + 1)
        return Response(
            content=content,
            status_code=206,
            media_type=content_type,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Accept-Ranges": "bytes",
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(len(content)),
                "Cache-Control": "public, max-age=31536000",
            },
        )
    return FileResponse(
        candidate,
        media_type=content_type,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=31536000",
        },
    )


@app.post("/login/get_notice/")
async def get_notice(request: Request):
    body = await request.body()
    log.info("get_notice (%db)", len(body))
    return {"code": 0, "data": {"notices": []}}


@app.post("/login/user_login/")
async def user_login(request: Request):
    body_text = (await request.body()).decode("utf-8", errors="replace")
    channel_uid = LOCAL_CHANNEL_UID
    username = LOCAL_USERNAME
    channel_id = "46"
    try:
        data = json.loads(unquote(body_text))
        inner = data.get("data", {})
        if not MOBILE_LOCAL_MODE:
            channel_uid = str(inner.get("cUid") or inner.get("channel_uid") or channel_uid)
            username = str(inner.get("cName") or inner.get("channel_username") or username)
            channel_id = str(inner.get("channel_id") or channel_id)
        log.info(
            "user_login channel=%s(%s) user=%s",
            inner.get("channel_name"),
            channel_id,
            channel_uid,
        )
    except Exception as exc:
        log.warning("user_login parse error: %s", exc)

    account = storage.get_or_create_account(channel_uid, username, channel_id)
    uid = account["uid"]
    client_host = request.client.host if request.client else ""
    if client_host:
        _RECENT_HTTP_ACCOUNTS[client_host] = (uid, int(time.time()))
    response = {
        "code": 0,
        "data": {
            "uid": uid,
            "lastLoginServerId": CONFIG["server_id"],
            "accountServerId": "2001",
            "phone": "",
            "checkOtherAccountResult": False,
            "districts": [
                {
                    "serverId": CONFIG["server_id"],
                    "areaId": CONFIG["area_id"],
                    "areaName": CONFIG["area_name"],
                    "serverName": CONFIG["server_name"],
                    "isRmd": 1,
                    "state": 1,
                    "downTimeInfo": "非维护中",
                    "serverIp": CONFIG["server_ip"],
                    "port": CONFIG["tcp_port"],
                    "roleCount": 1,
                }
            ],
            "activation": True,
            "uuid": account["uuid"],
            "serverTime": int(time.time()),
        },
    }
    log.info(
        "user_login -> uid=%s server=%s:%s",
        uid,
        CONFIG["server_ip"],
        CONFIG["tcp_port"],
    )
    return JSONResponse(
        response,
        headers={"Access-Control-Allow-Origin": "*", "Server": "elb"},
    )


@app.post("/login/get_simple_info/")
@app.post("/login/user_reg/")
@app.post("/login/get_guest_account/")
@app.post("/login/user_code_reg/")
@app.post("/login/user_activation/")
@app.post("/login/send/")
@app.post("/login/verify/")
@app.post("/login/get_notice_content/")
@app.api_route("/cloud-storage/config/getConfig", methods=["GET", "POST"])
async def cloud_storage_config(request: Request):
    log.info("cloud-storage config -> local")
    return JSONResponse(
        content={"code": 0, "msg": "success", "data": {"config": {"enable": True}, "switch": True}},
        headers={"Access-Control-Allow-Origin": "*"},
    )


@app.post("/login/create_recharge_order/")
async def create_recharge_order(request: Request):
    body = await request.body()
    payload = _decode_request_payload(body)
    client_host = request.client.host if request.client else ""
    account = _recharge_account(payload, client_host)
    plan_data = _recharge_plan(payload)
    if account is None or plan_data is None:
        log.warning(
            "recharge rejected account=%s product=%s bytes=%d shape=%s",
            bool(account), bool(plan_data), len(body),
            json.dumps(_payload_shape(payload), ensure_ascii=False, sort_keys=True),
        )
        return JSONResponse(
            {"code": 1, "msg": "unrecognized local recharge order", "data": {}},
            status_code=400,
        )

    mall_id, period, plan = plan_data
    order_values = _payload_values(payload, (
        "orderId", "order_id", "cpOrderId", "cp_order_id", "tradeNo", "trade_no",
    ))
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    request_key = str(order_values[0]) if order_values else hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    order_key = "recharge:%s:%d:%s" % (account["uid"], mall_id, request_key)
    result = storage.trade_offline_payment(
        account["uid"], int(plan["amount"]), list(plan["rewards"]), order_key,
        mall_id=mall_id, period=period, count=1,
        pending_notification={
            "kind": "offline_recharge",
            "orderKey": order_key,
            "mallId": int(mall_id),
            "payMoney": int(plan.get("payMoney", 0)),
            "amount": int(plan["amount"]),
            "rewards": list(plan["rewards"]),
            "createdAt": int(time.time()),
        },
    )
    if result is None:
        log.error("recharge transaction failed uid=%s mall=%s", account["uid"], mall_id)
        return JSONResponse(
            {"code": 1, "msg": "local recharge transaction failed", "data": {}},
            status_code=409,
        )
    order_id = "offline-" + hashlib.sha256(order_key.encode("utf-8")).hexdigest()[:24]
    log.info(
        "recharge delivered uid=%s mall=%s order=%s duplicate=%s",
        account["uid"], mall_id, order_id, bool(result.get("duplicate")),
    )
    return offline_success({
        "orderId": order_id,
        "order_id": order_id,
        "goodsId": int(plan.get("payMoney", 0)),
        "productId": int(plan.get("payMoney", 0)),
        "mallId": int(mall_id),
        "createTime": int(time.time()),
        "extraParams": "local-offline-delivery",
        "status": "success",
        "payStatus": 1,
        "paid": True,
        "isSuccess": True,
        "localDelivered": True,
        "duplicate": bool(result.get("duplicate")),
    })


def _local_media_candidate(relative: Path) -> Path | None:
    """Find a requested video in the game pack before the private mirror."""
    if relative.is_absolute() or ".." in relative.parts:
        return None
    for root in (GAME_ASSET_ROOT, ASSET_ROOT):
        resolved_root = root.resolve()
        for prefix in (Path(), Path("21_Media") / "CG", Path("21_Media")):
            candidate = (root / prefix / relative).resolve()
            try:
                candidate.relative_to(resolved_root)
            except ValueError:
                continue
            if candidate.is_file():
                return candidate
    return None


@app.api_route("/Onigao/Media/{media_path:path}", methods=["GET", "HEAD"])
async def local_media(media_path: str, request: Request):
    """Serve legacy MediaUrl paths from game external resources or the mirror."""
    relative = Path(unquote(media_path.replace("\\", "/")))
    if relative.is_absolute() or ".." in relative.parts:
        return JSONResponse({"code": 403, "msg": "invalid path"}, status_code=403)
    candidate = _local_media_candidate(relative)
    if candidate is None:
        log.warning("media missing: %s", relative.as_posix())
        return JSONResponse({"code": 404, "msg": "media not found"}, status_code=404)
    content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    range_header = request.headers.get("range")
    if range_header and request.method == "GET":
        size = candidate.stat().st_size
        bounds = _range_bounds(range_header, size)
        if bounds is None:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        start, end = bounds
        with candidate.open("rb") as stream:
            stream.seek(start)
            content = stream.read(end - start + 1)
        return Response(
            content=content,
            status_code=206,
            media_type=content_type,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(len(content)),
            },
        )
    log.info("media local: %s (%db)", relative.as_posix(), candidate.stat().st_size)
    return FileResponse(candidate, media_type=content_type, headers={"Accept-Ranges": "bytes"})


@app.api_route("/ng/client/system.getSecurityKey", methods=["GET", "POST"])
async def local_security_key(request: Request):
    """Return the SDK's binary public-key envelope without contacting 9game."""
    body = await request.body()
    log.info("9game security key -> local (%db)", len(body))
    # Keep the bootstrap payload available for one-time offline fixture capture.
    (ROOT / "sdk_security_key_request.bin").write_bytes(body)
    key_file = ROOT / "sdk_security_key_response.bin"
    if key_file.is_file():
        return Response(
            content=key_file.read_bytes(),
            media_type="text/plain; charset=utf-8",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Server": "Tengine/Aserver",
                "Acs-Resp-Code": "10",
                "Acs-Sub-Code": "4001010",
                "Cache-Control": "no-cache",
            },
        )
    return Response(status_code=503, content=b"", headers={"Server": "elb"})


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def catch_all(path: str, request: Request):
    host = request.headers.get("host", "").split(":", 1)[0].lower()
    body = await request.body()
    log.info(
        "offline stub %s host=%s path=/%s (%db)",
        request.method,
        host,
        path,
        len(body),
    )

    if ALLOW_UPSTREAM:
        try:
            import httpx

            headers = {
                key: value
                for key, value in request.headers.items()
                if key.lower() not in {"host", "content-length"}
            }
            async with httpx.AsyncClient(timeout=20.0) as client:
                upstream = await client.request(
                    request.method,
                    f"http://{host}/{path}",
                    params=dict(request.query_params),
                    headers=headers,
                    content=body,
                )
            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                media_type=upstream.headers.get("content-type"),
            )
        except Exception as exc:
            log.warning("diagnostic upstream failed: %s", exc)

    if "httpdns" in host or path.endswith("/resolve") or "/resolve" in path:
        return offline_success({"ttl": 60, "ips": []})
    if "collector" in path or "log." in host or host.startswith("line1-log"):
        return Response(status_code=200)
    return JSONResponse(offline_success())


def serve():
    # Recursive resource counting is diagnostic-only and can take minutes on
    # an Android shared storage mirror.  Do not block the mobile server before
    # it starts accepting the game's HTTP requests.
    manifest_count = None if os.environ.get("SOULTIDE_MOBILE_MODE") == "1" else sum(
        1 for item in ASSET_ROOT.rglob("*") if item.is_file()
    )
    log.info(
        "HTTP server starting on 0.0.0.0:8081 (offline=%s, CDN fallback=%s)",
        not ALLOW_UPSTREAM,
        CDN_UPSTREAM_FALLBACK,
    )
    log.info("TCP target: %s:%s", CONFIG["server_ip"], CONFIG["tcp_port"])
    if manifest_count is None:
        log.info("Local CDN: %s (file count skipped on mobile)", ASSET_ROOT)
    else:
        log.info("Local CDN: %s (%d files)", ASSET_ROOT, manifest_count)
    log.info("Local manifest: %s", LOCAL_MANIFEST)
    uvicorn.run(app, host=_BIND_HOST, port=HTTP_PORT, log_level="info")


if __name__ == "__main__":
    serve()
