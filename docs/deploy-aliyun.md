# 部署到阿里云 ECS（Ubuntu 24.04 + systemd + Nginx）

> 记录一次**从零到公网可访问**的真实部署过程。
> 目标读者：拿到这个仓库、想在自己的 VPS 上跑起来的人。
> 本文所有命令都在 Ubuntu 24.04.5 LTS / x86_64 上实测通过。

---

## 0. 环境

| 项 | 值 |
| --- | --- |
| 云厂商 | 阿里云 ECS |
| 地域 | 华北2（北京） |
| 规格 | 2 vCPU / 2 GiB（经济型 e，`ecs.e-c1m1.large`） |
| 系统盘 | 40 GiB ESSD Entry |
| 系统 | Ubuntu 24.04.5 LTS，x86_64 |
| 公网访问 | HTTP 80（安全组放行 80 / 443 / 22） |

实际资源占用（跑起来之后 `systemctl status` 读数）：

```
Active: active (running)
Memory: 38.6M       ← 启动后瞬时
```

运行期峰值在 200 MB 以内，2 GiB 内存余量充足。

---

## 1. 系统初始化

```bash
# 时区改成北京时间（否则 journalctl 里的时间会比本地慢 8 小时，排查时容易误判）
timedatectl set-timezone Asia/Shanghai

# 更新索引
apt-get update -y
```

**预期**：`Reading package lists... Done`

```bash
# 装运行依赖
apt-get install -y python3 python3-venv python3-pip git nginx curl
```

**各包作用**：

| 包 | 作用 |
| --- | --- |
| `python3` | Ubuntu 24.04 自带 3.12 |
| `python3-venv` | 建虚拟环境。**不能省**，否则 pip 报 `externally-managed-environment` |
| `python3-pip` | 装依赖 |
| `git` | 拉代码 |
| `nginx` | 反向代理 80 → 8000 |
| `curl` | 验证接口 |

---

## 2. 拉代码 + 装依赖

```bash
cd /opt && git clone https://github.com/1377488566-png/shopify-ops.git
cd /opt/shopify-ops

# 建虚拟环境
python3 -m venv venv

# 装依赖
./venv/bin/pip install -r requirements.txt
```

**实测**：依赖共 28 个包（fastapi / uvicorn / jinja2 / requests / starlette / pydantic 等），
`Successfully installed ...` 即为成功。

---

## 3. 先手动验证能跑，再交给 systemd

**这一步不要跳过**：前台先跑通，出问题一眼能看见堆栈；直接上 systemd 的话，错误只会藏在 journal 里。

```bash
./venv/bin/python -m uvicorn webapp.server:app --host 0.0.0.0 --port 8000
```

**预期输出**：

```
AUTO_SEED 已开启且库为空，自动灌入 mock 数据
已写入 20 / 20 条
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000
```

> **AUTO_SEED 的行为**（见 `bootstrap.py`）：`MOCK=true` 且未显式配置 `AUTO_SEED` 时自动开启；
> 一旦检测到真实店铺凭据就**不会**开启，避免往生产库灌假订单。本次是全新空库，因此自动播种生效。

另开一个终端（或在后台跑）验证：

```bash
curl -s -o /dev/null -w "HTTP %{http_code} | %{size_download} 字节\n" http://127.0.0.1:8000/
# HTTP 200 | 14003 字节
```

确认无误后，停掉手动进程，避免和 systemd 抢端口：

```bash
pkill -f "uvicorn webapp.server"
```

---

## 4. systemd 托管（开机自启 + 崩溃自动重启）

```bash
cat > /etc/systemd/system/shopify-ops.service <<'EOF'
[Unit]
Description=Shopify Ops Web App
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/shopify-ops
ExecStart=/opt/shopify-ops/venv/bin/python -m uvicorn webapp.server:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
```

**几个关键点，都是有意的设计：**

| 配置 | 为什么这么写 |
| --- | --- |
| `WorkingDirectory=/opt/shopify-ops` | 代码要读 `data/`、`samples/`、模板文件，工作目录错了会 500 |
| `--host 127.0.0.1` | **只监听本机**，由 Nginx 对外。外网无法直连 8000，减少暴露面 |
| `Restart=always` + `RestartSec=3` | 进程挂了 3 秒后自动拉起，不需要人工介入 |
| `StandardOutput/Error=journal` | 日志统一进 journald，用 `journalctl -u shopify-ops` 查 |

启用并启动：

```bash
systemctl daemon-reload
systemctl enable --now shopify-ops
systemctl status shopify-ops --no-pager
```

**预期**：

```
Loaded: loaded (.../shopify-ops.service; enabled; preset: enabled)
Active: active (running)
```

`enabled` = 开机自启已设；`active (running)` = 正在运行。

---

## 5. Nginx 反向代理（80 → 8000）

```bash
cat > /etc/nginx/sites-available/shopify-ops <<'EOF'
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF

# 启用本站配置，并删掉会抢 80 端口的默认站点
ln -sf /etc/nginx/sites-available/shopify-ops /etc/nginx/sites-enabled/shopify-ops
rm -f /etc/nginx/sites-enabled/default

# 校验语法后再重载
nginx -t
systemctl reload nginx
```

**`nginx -t` 必须显示 `test is successful` 再 reload**，否则会带着坏配置重载。

**验证整条链路**：

```bash
curl -s -o /dev/null -w "HTTP %{http_code} | %{size_download} 字节\n" http://127.0.0.1/
curl -s -I http://127.0.0.1/ | head -3
```

**预期**：

```
HTTP 200 | 14003 字节
HTTP/1.1 200 OK
Server: nginx/1.24.0 (Ubuntu)     ← 出现 nginx 说明请求经过了反代
```

外部验证（从公网）：

```
http://<你的公网 IP>/
```

---

## 6. 安全组

| 端口 | 协议 | 来源 | 用途 |
| --- | --- | --- | --- |
| 80 | TCP | 0.0.0.0/0 | HTTP |
| 443 | TCP | 0.0.0.0/0 | HTTPS（预留） |
| 22 | TCP | 0.0.0.0/0（建议收紧） | SSH |

> **3389（RDP）在 Linux 上没意义，建议删除**，少一个被扫描的入口。

**注意**：安全组放行 80 之后，在装好 Nginx 之前访问仍然不通 —— 这不是安全组的问题，是本地没有进程监听 80。

---

## 7. 常用运维命令

```bash
systemctl status shopify-ops      # 看状态
systemctl restart shopify-ops     # 重启
journalctl -u shopify-ops -f      # 实时跟日志
journalctl -u shopify-ops -n 50   # 最近 50 行
ss -tlnp | grep -E '8000|80'      # 看端口占用
```

**更新代码后**：

```bash
cd /opt/shopify-ops && git pull && systemctl restart shopify-ops
```

---

## 8. 与 Render 部署的差别

| | 阿里云 ECS | Render 免费层 |
| --- | --- | --- |
| 冷启动 | 无 | 15 分钟无访问即休眠，下次约 30–60 秒 |
| 磁盘 | 持久（40 GiB） | 临时，重启丢库 |
| HTTPS | 需自备域名 + 证书 | 自带 |
| 运维 | 自己管（systemd / nginx / 安全组） | 平台托管 |
| 成本 | ~¥0.12/小时 | 免费 |

**结论**：演示用阿里云（快、不丢数据），备用留 Render（免费、HTTPS）。
但阿里云的**公网 IP 不保证长期不变**，长期对外提供访问应绑定域名。

---

## 9. 待办

- [ ] 绑域名 + 申请证书，启用 HTTPS（当前仅 HTTP）
- [ ] 收紧 22 端口的来源 IP
- [ ] 配日志轮转（当前日志全在 journald，长期运行需设上限）
