"""
Postman collection 的解析与执行核心。

这个文件不涉及网页，只做三件事：
  1. 从 collection JSON 里把每个请求拆出来
  2. 把 {{变量}} 替换成真实值，然后发出去
  3. 根据响应给出"通过 / 失败"的结论

判断规则（有意做得简单、可解释）：
  - 状态码 2xx（200~299）→ 通过
  - 非 2xx，或请求本身报错 → 失败

注意：Postman 集合里自带的 JavaScript 断言脚本（pm.test）不会被执行。
"""

import ipaddress
import re
import socket
import time
from typing import Any
from urllib.parse import urlparse

import requests

VAR_PATTERN = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")

# 请求名里写了 "→ 404" 或 "-> 404"，就把 404 当作这条请求的期望状态码。
EXPECT_PATTERN = re.compile(r"(?:→|->)\s*(\d{3})(?!\d)")

DEFAULT_TIMEOUT = 15

# 是否允许请求内网 / 本机地址。
# 本机自用可以放开；部署到公网必须关闭，否则别人能拿你的服务器探测内网（SSRF）。
ALLOW_PRIVATE_TARGETS = True
_PRIVATE_WHITELIST = set()


def configure_network(allow_private: bool, whitelist=None):
    """由服务层在启动时调用，决定是否放行内网地址。"""
    global ALLOW_PRIVATE_TARGETS, _PRIVATE_WHITELIST
    ALLOW_PRIVATE_TARGETS = allow_private
    _PRIVATE_WHITELIST = set(whitelist or [])


def is_private_target(url: str) -> bool:
    """判断这个地址是不是指向内网 / 本机。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    host = parsed.hostname
    if not host:
        return False

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if f"{host}:{port}" in _PRIVATE_WHITELIST:
        return False

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False

    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            return True

    return False


def expected_status_of(name: str):
    """从请求名里读出期望状态码；没写就返回 None（表示"任意 2xx 都算通过"）。"""
    match = EXPECT_PATTERN.search(name or "")
    return int(match.group(1)) if match else None


def substitute(value: Any, variables: dict) -> Any:
    """把 {{变量}} 换成真实值，递归处理字符串 / 列表 / 字典。"""
    if isinstance(value, str):
        def _replace(match):
            key = match.group(1).strip()
            return str(variables.get(key, match.group(0)))

        return VAR_PATTERN.sub(_replace, value)

    if isinstance(value, list):
        return [substitute(v, variables) for v in value]

    if isinstance(value, dict):
        return {k: substitute(v, variables) for k, v in value.items()}

    return value


def _flatten(items, folder: str = "", out=None):
    """把嵌套的文件夹结构拍平成一串请求。"""
    if out is None:
        out = []

    for item in items or []:
        if "item" in item:
            name = item.get("name", "")
            sub = f"{folder} / {name}" if folder else name
            _flatten(item["item"], sub, out)
        elif "request" in item:
            out.append(
                {
                    "folder": folder,
                    "name": item.get("name", "未命名请求"),
                    "request": item["request"],
                }
            )

    return out


def _url_of(req) -> str:
    url = req.get("url", "")
    if isinstance(url, dict):
        if url.get("raw"):
            return url["raw"]
        host = url.get("host", [])
        path = url.get("path", [])
        if isinstance(host, list):
            host = ".".join(host)
        if isinstance(path, list):
            path = "/".join(str(p) for p in path)
        return f"{host}/{path}"
    return str(url)


def _headers_of(req) -> dict:
    headers = {}
    for h in req.get("header", []) or []:
        if h.get("disabled"):
            continue
        key = h.get("key")
        if key:
            headers[key] = h.get("value", "")
    return headers


def _body_of(req):
    body = req.get("body") or {}
    mode = body.get("mode")

    if mode == "raw":
        return body.get("raw", "")

    if mode == "urlencoded":
        return {
            i.get("key"): i.get("value", "")
            for i in body.get("urlencoded", [])
            if not i.get("disabled")
        }

    if mode == "formdata":
        return {
            i.get("key"): i.get("value", "")
            for i in body.get("formdata", [])
            if i.get("type") != "file"
        }

    return None


def parse_collection(data: dict) -> dict:
    """解析 collection，返回「请求清单 + 声明的变量」。"""
    items = _flatten(data.get("item", []))

    requests_info = []
    for it in items:
        req = it["request"]
        requests_info.append(
            {
                "name": it["name"],
                "folder": it["folder"],
                "method": (req.get("method") or "GET").upper(),
                "url": _url_of(req),
            }
        )

    variables = [
        {"key": v.get("key", ""), "value": v.get("value", "")}
        for v in (data.get("variable") or [])
    ]

    return {
        "name": (data.get("info") or {}).get("name", "未命名集合"),
        "requests": requests_info,
        "variables": variables,
        "count": len(requests_info),
    }


def parse_environment(data: dict) -> list:
    """解析 Postman 导出的 environment 文件，取出变量。"""
    values = data.get("values") or []
    return [
        {"key": v.get("key", ""), "value": v.get("value", "")}
        for v in values
        if v.get("enabled", True)
    ]


def run_collection(data: dict, variables: dict = None, token: str = "", timeout: int = DEFAULT_TIMEOUT):
    """执行整个 collection，逐条返回结果。"""
    merged = dict(variables or {})
    for v in data.get("variable") or []:
        merged.setdefault(v.get("key", ""), v.get("value", ""))

    items = _flatten(data.get("item", []))
    results = []

    for index, it in enumerate(items, start=1):
        req = it["request"]
        method = (req.get("method") or "GET").upper()
        url = substitute(_url_of(req), merged)
        headers = substitute(_headers_of(req), merged)
        body = substitute(_body_of(req), merged)

        if token:
            headers["Authorization"] = f"Bearer {token}"

        started = time.time()
        status = None
        error = ""
        snippet = ""

        blocked = (not ALLOW_PRIVATE_TARGETS) and is_private_target(url)

        if blocked:
            error = "目标地址属于内网或本机，公网部署时已拒绝（防止被用来探测内网）"
            elapsed_ms = 0.0
        else:
            try:
                kwargs = {"timeout": timeout}
                if isinstance(body, str) and body.strip():
                    kwargs["data"] = body.encode("utf-8")
                elif isinstance(body, dict) and body:
                    if headers.get("Content-Type", "").startswith("application/json"):
                        kwargs["json"] = body
                    else:
                        kwargs["data"] = body

                resp = requests.request(method, url, headers=headers, **kwargs)
                status = resp.status_code
                snippet = resp.text[:400]
            except Exception as exc:  # 网络错误、DNS 失败、超时都走这里
                error = f"{type(exc).__name__}: {exc}"

            elapsed_ms = round((time.time() - started) * 1000, 1)

        expected = expected_status_of(it["name"])

        if error:
            passed = False
            reason = f"请求没发出去：{error}"
            expected_label = f"{expected}" if expected else "2xx"
        elif expected is not None:
            passed = status == expected
            expected_label = f"{expected}"
            if passed:
                reason = f"状态码 {status}，与预期一致"
            else:
                reason = f"状态码 {status}，但预期是 {expected}"
        else:
            passed = 200 <= status < 300
            expected_label = "2xx"
            if passed:
                reason = f"状态码 {status}，属于 2xx"
            else:
                reason = f"状态码 {status}，不属于 2xx（服务端返回了错误）"

        results.append(
            {
                "index": index,
                "name": it["name"],
                "folder": it["folder"],
                "method": method,
                "url": url,
                "status": status,
                "expected": expected_label,
                "elapsed_ms": elapsed_ms,
                "passed": passed,
                "reason": reason,
                "error": error,
                "snippet": snippet,
            }
        )

    return results
