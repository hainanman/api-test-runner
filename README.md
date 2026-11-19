# 接口测试运行台

> **线上演示：http://test.commentai.top** （阿里云函数计算 FC 3.0 · 中国香港 · Python 3.10 Web 函数）

一个网页工具：上传 Postman 导出的 `collection.json`，自动把集合里的请求逐个发出，返回每条的状态码、耗时和通过情况。

## 本地快速体验

```bash
pip install -r requirements.txt
uvicorn runner_app:app --port 8800
```

浏览器打开 `http://127.0.0.1:8800`，点「本地演示接口」→「开始运行」。

## 目录

| 文件 | 说明 |
|---|---|
| `runner_app.py` | 服务入口 |
| `runner_core.py` | 解析与执行核心 |
| `mock_server.py` | 内置演示用被测接口 |
| `ui.html` | 前端页面 |
| `samples/` | 三个预置示例集合 |
| `示例集合-15条.json` | 用于验收测试的 15 条请求集合 |
| `Dockerfile` | 公网部署镜像 |
| `HANDOFF.md` | 技术规格与接口说明 |
| `DEPLOY.md` | 部署步骤 |
| `注意事项.md` | **部署前先看这个** |

## 部署

见 `DEPLOY.md`，务先把 `ALLOW_PRIVATE_TARGETS` 设为 `0`（详见 `注意事项.md`）。

### 当前线上部署（阿里云 FC 3.0）

| 项 | 值 |
|---|---|
| 访问地址 | `http://test.commentai.top` |
| 函数 | `api-test-runner`（中国香港，Web 函数，Python 3.10） |
| 请求处理程序 | `index.handler`，见 `deploy/fc/index.py` |
| 环境变量 | `ALLOW_PRIVATE_TARGETS=0` |
| 实例规格 | 0.35 vCPU / 512 MB，**单实例并发 1**（不可扩容，见注意事项第 ② 条） |
| 依赖 | 全部打进代码包 `vendor/`，FC 环境不执行 pip install |

`deploy/fc/index.py` 是 FC 入口适配层：把 FC HTTP 事件转成 ASGI scope 交给 FastAPI，
文本响应直接返回字符串（避免 base64 触发 `Content-Disposition: attachment`）。
打包时必须补齐 `exceptiongroup`、`sniffio` 这两个 `python_version < 3.11` 专属依赖——
在高于 3.10 的本机用 `pip download` 会因环境标记被跳过，导致线上 `ImportModuleError`。

### 验收结果（线上实测）

- `/healthz` → `{"ok":true,"allow_private_targets":false}`，内网/SSRF 目标已关闭
- 本地演示集合 → 5/5 通过
- 15 条集合 → 15/15 通过
- 连续请求 → 429 `点得太快了，等几秒再试`，防刷生效
- 首页 200 `text/html`，`Content-Disposition: inline`
