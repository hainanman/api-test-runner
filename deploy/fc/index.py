# -*- coding: utf-8 -*-
"""阿里云 FC 3.0 入口：把 FC HTTP 事件转成 ASGI 调用 FastAPI 应用。

兼容三种入参契约：
  1. HTTP 事件 dict
  2. HTTP 事件的 JSON 字符串（FC Web 函数实际传入的形式）
  3. WSGI environ（备用）
返回 FC 期望的 statusCode / headers / body 字典。文本响应直接返回 str，
避免 base64 触发 Content-Disposition: attachment。
"""
import asyncio
import base64
import json
import os
import socket
import sys
import threading
import time
import traceback
from urllib.parse import urlencode

_HERE = os.path.dirname(os.path.abspath(__file__))
_VENDOR = os.path.join(_HERE, "vendor")
if os.path.isdir(_VENDOR) and os.environ.get("FC_SKIP_VENDOR") != "1":
    sys.path.insert(0, _VENDOR)
sys.path.insert(0, _HERE)

# 公网部署：禁止请求内网地址（SSRF 防护），必须在导入 runner_app 前设置
os.environ.setdefault("ALLOW_PRIVATE_TARGETS", "0")
os.environ.setdefault("HOST", "127.0.0.1")
os.environ.setdefault("PORT", "8800")

import runner_app  # noqa: E402

# 「本地演示接口」依赖 127.0.0.1:8765 的 mock 服务。
# 单实例单 worker，只允许启动一次，避免端口被重复绑定。
_MOCK_LOCK = threading.Lock()
_MOCK_READY = [False]


def _wait_port(port, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.2)
            s.close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def _ensure_mock():
    if _MOCK_READY[0]:
        return
    with _MOCK_LOCK:
        if _MOCK_READY[0]:
            return
        threading.Thread(target=runner_app._start_mock_service, daemon=True).start()
        ok = _wait_port(runner_app.MOCK_PORT)
        print("[fc] mock service ready=%s port=%s" % (ok, runner_app.MOCK_PORT), flush=True)
        _MOCK_READY[0] = True


_ensure_mock()


def _is_wsgi_environ(event):
    return isinstance(event, dict) and (
        "wsgi.input" in event or "REQUEST_METHOD" in event
    )


def _wsgi_to_event(env):
    headers = {}
    for key, value in env.items():
        if key.startswith("HTTP_"):
            headers[key[5:].replace("_", "-").lower()] = str(value)
    if env.get("CONTENT_TYPE"):
        headers["content-type"] = str(env["CONTENT_TYPE"])
    if env.get("HTTP_HOST"):
        headers["host"] = str(env["HTTP_HOST"])
    length = int(env.get("CONTENT_LENGTH") or 0)
    raw = b""
    if length:
        try:
            raw = env["wsgi.input"].read(length) or b""
        except Exception:
            raw = b""
    return {
        "httpMethod": env.get("REQUEST_METHOD", "GET"),
        "rawPath": env.get("PATH_INFO", "/"),
        "rawQueryString": env.get("QUERY_STRING", ""),
        "headers": headers,
        "body": base64.b64encode(raw).decode("ascii"),
        "isBase64Encoded": True,
    }


def _normalize(event):
    if _is_wsgi_environ(event):
        return _wsgi_to_event(event)
    for _ in range(3):
        if isinstance(event, (bytes, bytearray)):
            event = event.decode("utf-8", "replace")
        if isinstance(event, str):
            try:
                event = json.loads(event)
            except (ValueError, TypeError):
                return {}
        else:
            break
    return event if isinstance(event, dict) else {}


def _make_scope(event):
    ctx = event.get("requestContext") or {}
    http = ctx.get("http") or {}
    method = http.get("method") or event.get("httpMethod") or "GET"
    path = event.get("rawPath") or event.get("path") or http.get("path") or "/"

    query = event.get("rawQueryString") or ""
    if not query:
        params = (
            event.get("queryStringParameters")
            or event.get("queryParameters")
            or {}
        )
        flat = []
        for key, value in params.items():
            if isinstance(value, (list, tuple)):
                flat.extend((key, str(item)) for item in value)
            else:
                flat.append((key, str(value)))
        query = urlencode(flat)

    headers = {}
    for key, value in (event.get("headers") or {}).items():
        headers[str(key).lower()] = str(value)

    raw = event.get("body") or ""
    if isinstance(raw, (dict, list)):
        raw = json.dumps(raw, ensure_ascii=False)
    if event.get("isBase64Encoded"):
        body = base64.b64decode(raw) if raw else b""
    elif isinstance(raw, str):
        body = raw.encode("utf-8")
    else:
        body = raw or b""

    hdrs = [
        (k.encode("latin-1", "replace"), v.encode("latin-1", "replace"))
        for k, v in headers.items()
    ]
    if body and not any(k == b"content-length" for k, _ in hdrs):
        hdrs.append((b"content-length", str(len(body)).encode("ascii")))

    host = headers.get("host", "test.commentai.top")
    hostname = host.split(":")[0]
    try:
        port = int(host.split(":")[1]) if ":" in host else 80
    except ValueError:
        port = 80

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": str(method).upper(),
        "scheme": "https" if headers.get("x-forwarded-proto") == "https" else "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": query.encode("utf-8"),
        "root_path": "",
        "headers": hdrs,
        "server": (hostname, port),
        "client": ("127.0.0.1", 0),
    }
    return scope, body


async def _call_asgi(scope, body):
    messages = []
    sent = [False]

    async def receive():
        if not sent[0]:
            sent[0] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await runner_app.app(scope, receive, send)
    return messages


def _to_response(messages):
    status = 200
    headers = {}
    cookies = []
    chunks = []
    for msg in messages:
        if msg["type"] == "http.response.start":
            status = int(msg.get("status", 200))
            for raw_k, raw_v in (msg.get("headers") or []):
                key = raw_k.decode("latin-1").lower()
                value = raw_v.decode("latin-1")
                if key == "set-cookie":
                    cookies.append(value)
                else:
                    headers[key] = value
        elif msg["type"] == "http.response.body":
            chunks.append(msg.get("body", b"") or b"")
    payload = b"".join(chunks)

    ctype = headers.get("content-type", "").lower()
    binary = any(
        ctype.startswith(x)
        for x in ("image/", "audio/", "video/", "font/", "application/octet-stream")
    )
    if binary:
        out_body = base64.b64encode(payload).decode("ascii")
        encoded = True
    else:
        out_body = payload.decode("utf-8", "replace")
        encoded = False

    if cookies:
        headers["Set-Cookie"] = cookies[0]
    headers.setdefault("Content-Disposition", "inline")
    # 删掉大小写重复项
    clean = {}
    for k, v in headers.items():
        clean[k] = v
    return {
        "statusCode": status,
        "headers": clean,
        "isBase64Encoded": encoded,
        "body": out_body,
    }


def handler(event, context=None):
    try:
        _ensure_mock()
        normalized = _normalize(event)
        if not normalized:
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "text/plain; charset=utf-8"},
                "body": "unsupported event",
                "isBase64Encoded": False,
            }
        scope, body = _make_scope(normalized)
        messages = asyncio.run(_call_asgi(scope, body))
        return _to_response(messages)
    except Exception:
        traceback.print_exc()
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json; charset=utf-8",
                        "Content-Disposition": "inline"},
            "body": json.dumps({"detail": "internal error", "trace": traceback.format_exc()[-2000:]},
                               ensure_ascii=False),
            "isBase64Encoded": False,
        }
