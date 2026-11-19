# 部署步骤

两条路，选一条。**动手前先读 `注意事项.md`。**

---

## 路线 A：Render / Railway（不用买服务器，最快）

### 1. 把代码推到 GitHub

```bash
git init
git add .
git commit -m "接口测试运行台"
git remote add origin https://github.com/你的账号/仓库名.git
git push -u origin main
```

### 2. 在平台创建服务

**Render**
1. 注册 → New → **Web Service**
2. 选你的仓库
3. Language 选 **Docker**
4. Environment Variables 加一条：`ALLOW_PRIVATE_TARGETS` = `0`
5. Create Web Service

**Railway**
1. 注册 → New Project → Deploy from GitHub repo
2. 自动识别 Dockerfile
3. Variables 加 `ALLOW_PRIVATE_TARGETS` = `0`

平台会给你一个 `xxx.onrender.com` / `xxx.up.railway.app` 地址，先确认能打开。

### 3. 绑自己的域名

平台里找 **Custom Domain**，填 `test.你的域名.com`，它会给你一条 CNAME 记录。

去域名服务商的 DNS 设置里加：

```
类型：CNAME
主机记录：test
记录值：平台给的那串地址
```

等 1~10 分钟生效。HTTPS 平台会自动签。

---

## 路线 B：自己的云服务器 + Docker

```bash
# 1. 登录服务器
ssh root@你的服务器IP

# 2. 装 Docker
curl -fsSL https://get.docker.com | sh

# 3. 本地把代码传上去
scp -r . root@你的IP:/opt/api-test-runner/

# 4. 服务器上构建并运行
cd /opt/api-test-runner
docker build -t api-test-runner .
docker run -d --name runner \
  -p 80:8800 \
  -e ALLOW_PRIVATE_TARGETS=0 \
  --restart unless-stopped \
  api-test-runner
```

打开 `http://你的IP` 应该能看到页面。

### 配 HTTPS

域名 A 记录指向服务器 IP 后，用 Caddy 自动签证书：

```bash
docker run -d --name caddy \
  -p 443:443 -p 80:80 \
  -v caddy_data:/data \
  caddy caddy reverse-proxy --from test.你的域名.com --to 127.0.0.1:80
```

---

## 部署完必须验收

| # | 检查项 | 通过标准 |
|---|---|---|
| 1 | `/healthz` | 返回 `{"ok":true,"allow_private_targets":false}` |
| 2 | 首页 | 三个示例按钮都在 |
| 3 | 点「本地演示接口」→ 运行 | **5 条全部通过** |
| 4 | 上传 `示例集合-15条.json` → 运行 | **15 条全部通过** |
| 5 | 连点两次「开始运行」 | 第二次提示「点得太快了」 |
| 6 | 手机 4G 打开 | 能正常显示 |

第 1 条里 `allow_private_targets` 必须是 `false`——这是安全开关，别漏。

---

## 常见故障

| 现象 | 原因 | 解决 |
|---|---|---|
| 启动崩溃，日志有 `Form data requires "python-multipart"` | 依赖没装全 | 确认 `requirements.txt` 里有 `python-multipart` |
| 页面能开，点运行报 500 | 8765 端口被占（开了多实例） | 改成单实例单 worker |
| 域名打不开，IP 能打开 | DNS 未生效，或国内服务器没备案 | 检查解析 / 去备案 |
| 免费平台第一次打开很慢 | 冷启动（休眠唤醒） | 提前点一次唤醒 |
| `docker build` 拉不到基础镜像 | 网络问题 | 换镜像源，或用平台托管构建 |
