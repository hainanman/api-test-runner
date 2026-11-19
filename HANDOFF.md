# 交接说明（技术规格）

## 一、这是什么

**接口测试运行台**：一个网页应用。用户上传 Postman 导出的 `collection.json`，服务端把集合里的每个请求逐个发出，返回状态码、耗时和「通过 / 失败」结论。

**技术栈**：Python + FastAPI + uvicorn + requests，前端是一个静态 HTML 页面。

---

## 二、启动

```bash
# Docker（推荐，平台会自动识别 Dockerfile）
docker build -t api-test-runner .
docker run -p 8800:8800 -e ALLOW_PRIVATE_TARGETS=0 api-test-runner

# 不用 Docker
pip install -r requirements.txt
ALLOW_PRIVATE_TARGETS=0 uvicorn runner_app:app --host 0.0.0.0 --port 8800
```

启动后：
- 网页：`http://<host>:8800/`
- 健康检查：`http://<host>:8800/healthz`

---

## 三、环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOST` | `127.0.0.1` | 监听地址。公网必须 `0.0.0.0` |
| `PORT` | `8800` | 服务端口。Dockerfile 已兼容平台注入的 `PORT` |
| `MOCK_PORT` | `8765` | 内置演示服务端口，一般不改 |
| `ALLOW_PRIVATE_TARGETS` | `1` | **上线必须为 `0`**，否则存在 SSRF 风险 |

`ALLOW_PRIVATE_TARGETS=0` 时，内置演示服务 `127.0.0.1:8765` 在白名单内，仍然可用。

---

## 四、接口清单

| 路径 | 方法 | 说明 |
|---|---|---|
| `/` | GET | 网页（ui.html） |
| `/healthz` | GET | 健康检查，返回 `{"ok": true, "allow_private_targets": false}` |
| `/api/samples` | GET | 三个预置示例的元信息 |
| `/api/sample/{id}` | GET | 取某个示例集合（id: `local` / `jsonplaceholder` / `httpbin`） |
| `/api/upload` | POST | `multipart/form-data`，字段名 `file`，解析上传的集合 |
| `/api/run` | POST | JSON：`{"collection": {...}, "variables": {...}, "token": ""}` |

`/api/run` 的返回：

```json
{
  "results": [{"index":1,"name":"...","method":"GET","url":"...","status":200,
               "expected":"2xx","elapsed_ms":9.6,"passed":true,
               "reason":"状态码 200，属于 2xx","error":"","snippet":"..."}],
  "total": 5, "passed": 5, "failed": 0, "duration": 0.05
}
```

---

## 五、代码结构

| 文件 | 职责 |
|---|---|
| `runner_app.py` | FastAPI 服务：上传、校验、限流、调用核心、返回 JSON |
| `runner_core.py` | 核心逻辑：解析 collection、替换变量、发请求、判定。**不依赖网页** |
| `mock_server.py` | 内置演示用的被测服务（FastAPI），跑在 8765 端口 |
| `ui.html` | 前端页面（原生 JS，无构建步骤） |
| `samples/*.json` | 三个预置示例集合 |

**核心与网页解耦**，`runner_core` 可以在命令行里单独调用。

---

## 六、内置的防护

| 项 | 值 |
|---|---|
| 上传文件大小 | ≤ 5MB |
| 单次请求数 | ≤ 50 |
| 同一 IP 冷却 | 3 秒 |
| 全局并发 | 同时只允许一轮运行 |
| 内网地址 | 公网模式下拒绝 |

---

## 七、判定规则（业务逻辑，别改）

1. 请求名里写了 `→ 404` 或 `-> 404` 的，按 404 判定（负向用例）
2. 没写的，按 2xx 判定
3. 请求发不出去（DNS/超时）的，判失败并给出错误原因
4. **不执行**集合里的 `pm.test` JavaScript 断言脚本

---

## 八、已验证的事实

| 项目 | 结果 |
|---|---|
| 全新虚拟环境安装 `requirements.txt` | ✅ 通过 |
| 从全新环境启动、导入全部模块 | ✅ 通过 |
| 公网模式下运行本地示例 | ✅ 5/5 通过 |
| 公网模式上传 collection | ✅ 正确解析 5 个请求 |
| 公网模式请求内网地址 | ✅ 被拒绝并给出原因 |
| 公网模式请求公网地址 | ✅ 正常 |
| 文件上传接口（multipart） | ✅ 正常 |
| **Docker 镜像构建** | ⚠️ **未验证**（开发机没有 Docker） |

---

## 九、已知约束

1. **只能单实例 / 单 worker**：内置演示服务占用本机 8765 端口，第二个实例绑定会失败。
2. 用户上传的文件不落盘，只在内存处理，跑完丢弃。
3. 演示服务的数据在每次运行前自动重置，保证同一集合反复跑结果一致。
