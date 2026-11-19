"""
接口测试运行台（网页版）。

打开后可以：
  1. 选一个预置示例，或上传自己的 Postman collection.json
  2. 点「开始运行」，它会把集合里的请求逐个发出去
  3. 每条给出通过 / 失败和耗时，最后汇总

启动：python runner_app.py    （或双击 打开工具.bat）
"""

import json
import os
import threading
import time
import webbrowser
from contextlib import asynccontextmanager

import requests
import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

import runner_core as core
from mock_server import app as mock_app

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES_DIR = os.path.join(HERE, "samples")
UI_FILE = os.path.join(HERE, "ui.html")

# 部署相关配置全部来自环境变量，方便放到任何平台上跑
HOST = os.environ.get("HOST", "127.0.0.1")
APP_PORT = int(os.environ.get("PORT", "8800"))
MOCK_PORT = int(os.environ.get("MOCK_PORT", "8765"))

# 部署到公网时应设为 0：禁止请求内网地址（防 SSRF）
ALLOW_PRIVATE_TARGETS = os.environ.get("ALLOW_PRIVATE_TARGETS", "1") != "0"

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_REQUESTS = 50

# 简单的防刷：同一个 IP 3 秒内只能跑一次；全局同时只允许一次运行
MIN_INTERVAL_SECONDS = 3
_last_run_at = {}
_run_lock = threading.Lock()

# 本地演示服务始终允许被访问（它是内置靶子，不是用户填的地址）
core.configure_network(
    ALLOW_PRIVATE_TARGETS,
    whitelist={f"127.0.0.1:{MOCK_PORT}", f"localhost:{MOCK_PORT}"},
)


def _start_mock_service():
    """后台启动本地被测服务（给「本地演示接口」用）。"""
    try:
        uvicorn.run(mock_app, host="127.0.0.1", port=MOCK_PORT, log_level="error")
    except Exception:
        pass


@asynccontextmanager
async def lifespan(_app):
    threading.Thread(target=_start_mock_service, daemon=True).start()
    time.sleep(0.6)
    yield


app = FastAPI(title="接口测试运行台", lifespan=lifespan)


def _load_sample(sample_id: str) -> dict:
    path = os.path.join(SAMPLES_DIR, f"{sample_id}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="示例不存在")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@app.get("/", response_class=HTMLResponse)
def index():
    with open(UI_FILE, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/samples")
def list_samples():
    return [
        {
            "id": "local",
            "title": "本地演示接口",
            "desc": "项目自带 Mock 服务，不依赖外网，最稳定",
        },
        {
            "id": "jsonplaceholder",
            "title": "JSONPlaceholder",
            "desc": "公开练习接口，无需登录、无频次限制",
        },
        {
            "id": "httpbin",
            "title": "HTTPBin",
            "desc": "验证 HTTP 行为：回显、JSON、状态码",
        },
    ]


@app.get("/api/sample/{sample_id}")
def get_sample(sample_id: str):
    data = _load_sample(sample_id)
    return {"collection": data, "summary": core.parse_collection(data)}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    raw = await file.read()

    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(status_code=400, detail="文件超过 5MB")

    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception:
        raise HTTPException(status_code=400, detail="这不是合法的 JSON 文件")

    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="文件内容格式不对")

    # 判断是 environment 文件还是 collection 文件
    if "item" not in data and "values" in data:
        return {"kind": "environment", "variables": core.parse_environment(data)}

    if "item" not in data:
        raise HTTPException(
            status_code=400,
            detail="这不像 Postman 导出的 collection（缺少 item 字段）",
        )

    return {
        "kind": "collection",
        "collection": data,
        "summary": core.parse_collection(data),
    }


def _reset_local_target():
    """
    把本地演示服务的数据恢复成初始状态。

    只影响 127.0.0.1:8765 这个内置靶子，不会碰用户自己的接口。
    """
    try:
        requests.post(f"http://127.0.0.1:{MOCK_PORT}/__reset", timeout=1)
    except Exception:
        pass


@app.get("/healthz")
def healthz():
    """平台用来判断服务是否活着的探针。"""
    return {"ok": True, "allow_private_targets": ALLOW_PRIVATE_TARGETS}


@app.post("/api/run")
def run(payload: dict, request: Request):
    collection = payload.get("collection")
    if not isinstance(collection, dict):
        raise HTTPException(status_code=400, detail="没有可运行的集合")

    variables = payload.get("variables") or {}
    token = (payload.get("token") or "").strip()

    preview = core.parse_collection(collection)
    if preview["count"] == 0:
        raise HTTPException(status_code=400, detail="这个集合里没有可执行的请求")
    if preview["count"] > MAX_REQUESTS:
        raise HTTPException(
            status_code=400,
            detail=f"一次最多执行 {MAX_REQUESTS} 个请求，当前 {preview['count']} 个",
        )

    # 防刷：同一 IP 冷却 + 全局只允许一次运行
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    if now - _last_run_at.get(ip, 0) < MIN_INTERVAL_SECONDS:
        raise HTTPException(status_code=429, detail="点得太快了，等几秒再试")
    _last_run_at[ip] = now

    if not _run_lock.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="已有一轮测试正在运行，请稍后")
    try:
        # 每次运行前重置本地靶子，保证同一个集合反复跑结果一致
        _reset_local_target()

        started = time.time()
        results = core.run_collection(collection, variables, token)
        elapsed = round(time.time() - started, 2)
    finally:
        _run_lock.release()

    passed = sum(1 for r in results if r["passed"])
    return {
        "results": results,
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "duration": elapsed,
    }


def _open_browser():
    time.sleep(1.8)
    webbrowser.open(f"http://127.0.0.1:{APP_PORT}")


if __name__ == "__main__":
    if HOST in ("127.0.0.1", "localhost"):
        threading.Thread(target=_open_browser, daemon=True).start()

    print(f"接口测试运行台： http://{HOST}:{APP_PORT}")
    print(f"本地被测服务：   http://127.0.0.1:{MOCK_PORT}")
    print(f"允许内网目标：   {'是（本机模式）' if ALLOW_PRIVATE_TARGETS else '否（公网模式）'}")
    print("关闭这个窗口即可停止。")
    uvicorn.run(app, host=HOST, port=APP_PORT, log_level="warning")
