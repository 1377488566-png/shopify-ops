# shopify-ops —— Shopify 订单同步 + AI 英文售后邮件

一个**能直接跑通**的小工程：把 Shopify 店铺的订单增量拉下来存进 SQLite，遇到退款能感知，
然后用 LLM（DeepSeek）给客户写**英文售后邮件**，按订单状态自动选语气，最后导出/打印。

> 默认 `MOCK` 模式：**不会向任何真实店铺发请求**，全部用内置假数据（20 条订单，其中 2 条带退款）。
> 想接真机，看 [第 4 节](#4-从-mock-切到真实店铺)。

---

## 1. 链路总览

```
                 ┌──────────────────────────────┐
   API 轮询 ───► │ shopify_client.py            │  增量：updated_at_min
  （增量同步）    │  · 分页 Link 头               │  + 429 退避重试
                 │  · 超时 / 重试                │  + 请求超时
                 └──────────────┬───────────────┘
                                │ orders JSON
  Webhook ──────────────────────┤   （可选，退款实时）
  orders/* refunds/*            │   HMAC-SHA256 验签
  webhook.py                    ▼
                 ┌──────────────────────────────┐
                 │ db.py  (SQLite)              │
                 │  orders / line_items         │
                 │  refunds / emails / sync_state│  幂等 UPSERT
                 └──────────────┬───────────────┘
                                │ OrderRow
                                ▼
                 ┌──────────────────────────────┐
                 │ email_gen.py                 │
                 │  场景判定 → 4 类语气          │
                 │  DeepSeek LLM（英文）         │
                 │  无 key / 出错 → 英文模板兜底  │
                 └──────────────┬───────────────┘
                                ▼
                      emails 表 → 打印 / 导出 samples/
```

邮件按订单状态自动分 4 类：

| 场景 | 触发条件 | 语气 |
| --- | --- | --- |
| `welcome` 欢迎信 | 新订单待处理 / 待付款 | 感谢下单，承诺发货后给 tracking |
| `shipping_notice` 发货通知 | 已发货 | 告知已发出 + 3-7 天预期 |
| `refund_apology` 退款安抚 | 有退款记录 / 已退款 | 道歉 + 确认退款金额 + 银行 5-10 工作日 |
| `review_recovery` 差评挽回 | 订单取消 / 作废 | 道歉 + 引导重新下单 + 挽回 |

---

## 2. 目录结构

```
shopify-ops/
├─ README.md              # 本文档
├─ requirements.txt       # 依赖只有 requests
├─ .env.example           # 环境变量模板（复制成 .env 再填）
├─ .gitignore
├─ config.py              # 读环境变量 / .env；缺凭据自动进 mock；日志配置
├─ shopify_client.py      # Admin REST 客户端：增量 + 分页 + 限流退避 + mock 假数据
├─ db.py                  # SQLite 建表 / 幂等 UPSERT / 查询 / 统计
├─ sync.py                # 轮询同步（水位存 sync_state），可单独运行
├─ email_gen.py           # 场景判定 + DeepSeek 调用 + 英文模板兜底
├─ webhook.py             # 可选：stdlib http.server 收 webhook，HMAC 验签
├─ cli.py                 # 统一命令行入口
├─ tests/
│  └─ test_smoke.py       # 9 个冒烟测试（标准库 unittest）
├─ samples/               # 实际运行产物（证据）
│  ├─ run_log.txt         #   完整命令 + 输出
│  ├─ orders_sample.json  #   库里的 5 条订单
│   ├─ webhook_smoke.txt      webhook 验签实测输出（401/401/200）
│  ├─ emails_sample.txt   #   5 封生成的英文邮件全文
│  └─ stats.json          #   统计快照
├─ data/shopify.db        # 运行后自动生成的 SQLite 库（已 gitignore）
└─ logs/                  # 运行后自动生成的日志（已 gitignore）
```

---

## 3. 三步跑起来（Windows PowerShell）

```powershell
cd C:\Users\13774\projects\shopify-ops

# 第 0 步（只做一次）：建虚拟环境 + 装依赖
C:\Users\13774\AppData\Local\Programs\Python\Python312\python.exe -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt

# 第 1 步：同步订单（mock，不联网）
.\venv\Scripts\python.exe cli.py sync --mock

# 第 2 步：生成 5 封英文售后邮件
.\venv\Scripts\python.exe cli.py emails --limit 5

# 第 3 步：看统计 / 看邮件 / 导出样本
.\venv\Scripts\python.exe cli.py stats
.\venv\Scripts\python.exe cli.py show-emails --limit 5
.\venv\Scripts\python.exe cli.py export --out samples
```

> 没配 `DEEPSEEK_API_KEY` 也能跑完整流程，邮件会用内置英文模板生成（`provider=template`）。

### 3.1 Web 界面（FastAPI）

命令行之外还有一层浏览器界面，**同一个数据库、同一套业务逻辑**：

```powershell
# 装依赖（requirements.txt 已包含 fastapi / uvicorn / jinja2）
.\venv\Scripts\python.exe -m pip install -r requirements.txt

# 起服务
.\venv\Scripts\python.exe -m uvicorn webapp.server:app --host 127.0.0.1 --port 8000
```

然后打开 

| 地址 | 内容 |
| --- | --- |
| `http://127.0.0.1:8000/` | 运营看板：6 个指标卡 + 订单表 + 最近邮件 |
| `http://127.0.0.1:8000/order/5001` | 订单详情 + 一键生成英文售后邮件 |
| `http://127.0.0.1:8000/docs` | 自动生成的 API 文档（可直接在页面上点 "Try it out"） |

Web 层只做三件事，**没有改动原有任何模块**：

1. 每个请求单独开一个 SQLite 连接（`sqlite3` 连接不能跨线程共享），用完即关；
2. 把 CLI 里那几段「取数 → 生成 → 落库」搬成路由，复用 `db.py` / `email_gen.py` / `sync.py`；
3. 页面服务端渲染（Jinja2 + 内联 CSS），**零前端构建**，任何机器 clone 下来就能开。

### 3.2 公网部署（Render 免费层）

仓库里带了 `render.yaml`，在 [Render](https://render.com) 上选 **New → Blueprint** 指向本仓库即可，
会自动按下面的配置建服务：

- 构建：`pip install -r requirements.txt`
- 启动：`uvicorn webapp.server:app --host 0.0.0.0 --port $PORT`
- 健康检查：`/api/health`

`bootstrap.py` 的作用：Render 的磁盘是临时的，容器重启后 SQLite 是空的，首页会变成空表。
手动跑一次 `python bootstrap.py` 会在**库为空时**播种 20 条 mock 订单，已有数据则跳过（实测：空库播种 20 条，第二次运行日志为「数据库已有数据，跳过播种」）。

> 免费层的限制：15 分钟无访问会休眠，下次访问冷启动约 30-60 秒；磁盘不持久，重启后需要重新播种。

### 全部命令

| 命令 | 作用 |
| --- | --- |
| `cli.py sync [--mock] [--full]` | 增量同步订单（`--full` 忽略水位全量重拉） |
| `cli.py emails --limit 5 [--force]` | 给还没写过邮件的订单生成邮件（`--force` 强制重生成） |
| `cli.py show-emails --limit 5` | 打印已生成邮件的全文 |
| `cli.py orders --limit 10` | 订单列表 |
| `cli.py stats` | 数据概览 |
| `cli.py export --out samples` | 导出样本到 `samples/` |
| `cli.py webhook --port 8000` | 启动 webhook 接收端（可选组件） |
| `python -m unittest discover -s tests -v` | 跑测试 |

---

## 4. 从 mock 切到真实店铺

**第 1 步：拿 Admin API token**

1. Shopify 后台 → `Settings` → `Apps and sales channels` → `Develop apps` → `Create an app`
2. `Configure Admin API scopes`，至少勾上 **`read_orders`**（要看客户信息再加 `read_customers`）
3. `Install app` → 复制 **Admin API access token**（只显示一次，复制好别丢）

**第 2 步：填 `.env`**

```powershell
copy .env.example .env
notepad .env
```

```dotenv
SHOPIFY_SHOP=your-store.myshopify.com      # 只写域名，不要带 https://
SHOPIFY_ACCESS_TOKEN=shpat_xxxxxxxxxxxx    # 上一步复制的 token
MOCK=false                                 # 关掉 mock
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxx           # 想用 AI 写邮件就填；不填走模板
```

**第 3 步：先小范围验证，再放开**

```powershell
# 3.1 确认凭据正确、能连上（这一步只读 /shop.json）
.\venv\Scripts\python.exe cli.py stats

# 3.2 全量拉一次（第一次会拉全店订单，慢慢等）
.\venv\Scripts\python.exe cli.py sync --full

# 3.3 之后就是增量了，只拉水位之后有变动的订单
.\venv\Scripts\python.exe cli.py sync
```

> 安全提醒：
> - `.env` 已经在 `.gitignore` 里，**千万别提交、别贴给别人**；
> - 代码里**没有任何硬编码密钥**，key 只从环境变量读；
> - 只要 `SHOPIFY_SHOP` / `SHOPIFY_ACCESS_TOKEN` 为空，程序**强制走 mock**，不会误连线上。

---

## 5. Webhook 本地联调（可选）

轮询已经能覆盖 99% 场景，Webhook 的意义是**退款事件近实时**（不用等下一次轮询）。

思路（不绑定具体工具）：

1. 本地起服务：

   ```powershell
   .\venv\Scripts\python.exe cli.py webhook --port 8000
   ```

2. 用**内网穿透**把本地 `8000` 暴露成公网 HTTPS 地址。
   常见选择：`ngrok http 8000` / `cloudflared tunnel` / `frp`，任选一个你顺手的。

3. Shopify 后台 → `Settings` → `Notifications` → `Webhooks` → `Create webhook`：
   - URL 填 `https://<你的公网地址>/webhook`
   - 格式选 **JSON**
   - 事件勾上 `Order creation`、`Order update`、`Refund creation`

4. 创建后 Shopify 会给一个 **signing secret**，填进 `.env` 的 `SHOPIFY_WEBHOOK_SECRET`，重启服务。

5. 后台点 **Send test notification**，本地日志里应该出现：

   ```
   INFO | webhook | 验签通过 topic=orders/create
   INFO | webhook | 订单已入库：#1001
   ```

6. 本地自测（mock 模式下跳过验签，方便用 curl 打）：

   ```powershell
   curl.exe -X POST http://127.0.0.1:8000/webhook `
     -H "Content-Type: application/json" -H "X-Shopify-Topic: orders/create" `
     -d '{\"id\":999001,\"name\":\"#999001\",\"total_price\":\"12.00\",\"currency\":\"USD\",\"email\":\"a@b.com\",\"customer\":{\"first_name\":\"Test\"},\"line_items\":[]}'
   ```

> 记住：**验签用的是原始请求体字节**，所以代码里先 `hmac` 校验再 `json.loads`，顺序反了签名必然失败。
> 生产环境建议换成 gunicorn/uvicorn + 反向代理，并加一个失败重试队列。

---

## 6. 实际运行输出（证据）

完整记录见 [`samples/run_log.txt`](samples/run_log.txt)。下面是关键片段。

### 6.1 同步（第 1 次，全量）

```
$ python cli.py sync --mock
2026-09-23 21:26:26 | INFO | sync | 开始同步｜mode=mock｜mode=MOCK(内置假数据) | email_engine=内置英文模板(兜底)
2026-09-23 21:26:26 | INFO | sync | 上次同步水位：（无，全量拉取）
2026-09-23 21:26:26 | INFO | shopify_client | [mock] 返回内置假订单（不联网）
2026-09-23 21:26:27 | INFO | sync | 已写入 20 / 20 条
2026-09-23 21:26:27 | INFO | sync | 同步完成｜拉取 20 条｜写入 20 条｜页数 1｜新水位 2026-09-23T13:21:26+00:00

=== 同步结果 ===
模式        : mock
拉取 / 写入 : 20 / 20 条（1 页）
水位        : (无)  ->  2026-09-23T13:21:26+00:00
提示        : 本次全部是内置假数据，没有向任何真实店铺发请求。
```

### 6.2 再同步一次 —— 验证幂等

```
$ python cli.py sync --mock
2026-09-23 21:26:28 | INFO | sync | 上次同步水位：2026-09-23T13:16:26+00:00   ← 增量生效
...
拉取 / 写入 : 20 / 20 条（1 页）
```

订单总数仍然是 **20**（不是 40）——UPsert 幂等成立。

### 6.3 生成邮件

```
$ python cli.py emails --limit 5
邮件引擎：内置英文模板｜待生成 5 封

[1] #1001 | 发货通知 | provider=template
     Subject: Your order #1001 is on the way
[2] #1002 | 欢迎信（新订单待处理） | provider=template
     Subject: Thank you for your order #1002!
[3] #1003 | 欢迎信（新订单待处理） | provider=template
     Subject: Thank you for your order #1003!
[4] #1004 | 退款安抚 | provider=template
     Subject: About the refund for order #1004
[5] #1005 | 欢迎信（新订单待处理） | provider=template
     Subject: Thank you for your order #1005!
```

### 6.4 订单列表

```
$ python cli.py orders --limit 8
订单号      日期           客户                 金额           支付         发货         退款
#1001    2026-09-23   Emily Carter       USD 84.00    paid       fulfilled  -
#1002    2026-09-22   Michael Nguyen     USD 47.50    paid       -          -
#1003    2026-09-20   Sofia Rossi        USD 35.90    pending    -          -
#1004    2026-09-20   Daniel Okafor      USD 36.00    refunded   fulfilled  USD 14.40
#1005    2026-09-19   Hannah Schmidt     USD 142.90   paid       -          -
#1006    2026-09-18   Liam O'Connell     USD 75.00    cancelled  -          -
#1007    2026-09-17   Priya Sharma       USD 125.30   paid       fulfilled  -
#1008    2026-09-15   Noah Bennett       USD 107.40   paid       -          -
```

### 6.5 统计

```
$ python cli.py stats
=== 数据概览 ===
操作模式      : MOCK（内置假数据）
邮件引擎      : 内置英文模板（未配置 API key）
订单总数      : 20
订单明细行数  : 42
含退款订单    : 2（退款总额 98.4）
已生成邮件    : 5 封，覆盖 5 个订单
邮件按场景    : {'welcome': 3, 'shipping_notice': 1, 'refund_apology': 1}
邮件按生成器  : {'template': 5}
同步水位      : 2026-09-23T13:21:28+00:00
```

### 6.6 一封真实产出的邮件（模板模式，`samples/emails_sample.txt` 节选）

```
邮件 #4 | 订单 #1004 | 场景 退款安抚
收件人 : Daniel Okafor <daniel.okafor@example.com>
Subject: About the refund for order #1004
------------------------------------------------------------------------------
Hi Daniel Okafor,

Thank you for reaching out, and I am sorry that order #1004 did not work out as
expected. I completely understand how disappointing that is.

We have processed a refund of USD 14.40 back to your original payment method.
Depending on your bank or card issuer, the amount usually appears within 5-10
business days. If it has not shown up after that window, just reply to this
email and I will chase it for you personally.
...
```

### 6.7 测试

```
$ python -m unittest discover -s tests -v
Ran 9 tests in 0.147s

OK
```

---

## 7. 常见问题（FAQ）

**Q1：为什么我什么都没配，跑起来也没报错？**
这是设计如此。没有 `SHOPIFY_SHOP`/`SHOPIFY_ACCESS_TOKEN` 就自动进 mock，用内置假数据把流程走完，
这样你可以在不碰真实店铺的前提下先把链路跑通、把邮件文案调好。

**Q2：日志里出现 `provider=template` 而不是 `deepseek`？**
说明没读到 `DEEPSEEK_API_KEY`，或者 LLM 调用失败了（日志里会有 `LLM 生成失败，降级到内置模板：...`）。
检查点：`.env` 里 key 是否填了、`DEEPSEEK_BASE_URL` 是否为 `https://api.deepseek.com`、
网络能不能通。**mock 模式下 key 依然生效**，两者互不影响。

**Q3：同步会不会重复插入订单？**
不会。`orders` 主键就是 Shopify 订单 ID，用 `INSERT ... ON CONFLICT(id) DO UPDATE`；
`line_items`/`refunds` 每次按 `order_id` 先删后插。整体在一个事务里。

**Q4：`sync` 会不会重复跑到很久以前的数据？**
不会，水位存在 `sync_state` 表里，每次只拉 `updated_at_min` 之后有变动的订单。
水位还会**回退 5 分钟**做安全余量，防止时钟误差漏单。想重来一次就加 `--full`。

**Q5：`429 Too Many Requests` 怎么办？**
客户端已经内置处理：优先读响应头 `Retry-After`，没有就指数退避（1/2/4/8/16 秒），最多重试 5 次。
不需要你做什么。

**Q6：邮件为什么是英文的？**
需求就是发给英文客户的售后信。模板和 prompt 都固定输出英文；测试里还有一条断言专门检查正文不含中文。

**Q7：数据存在哪？**
默认 `data/shopify.db`（SQLite 单文件），日志在 `logs/*.log`。两者都在 `.gitignore` 里。
这个项目**不会把任何数据回传到外部**，唯一的外部调用是你自己配的 LLM API。

**Q8：`cli.py emails` 跑第二遍说“没有需要生成邮件的订单”？**
这是有意为之的幂等设计：默认只给**还没生成过邮件**的订单生成。
想强制重生成就加 `--force`。

**Q9：`python -m unittest` 报错退出码 1，但明明显示 OK？**
那是 PowerShell 把 unittest 写到 stderr 的正常输出当成了错误，测试实际全部通过。

---

## 8. 下一步可以做什么

1. **接真实店铺** —— 按第 4 节配好 `.env`，`MOCK=false` 后 `sync --full`。
2. **接邮件发送（SMTP）** —— `db.py` 里已经留好 `mark_email_sent()` 和 `emails.status/sent_at` 字段；
   接一个 `smtp.py`（`smtplib` + `email.message.EmailMessage`），把生成的 draft 发出去并回写状态即可。
3. **接定时任务** —— 用系统计划任务定时 `cli.py sync && cli.py emails`（本机不建议直接改系统计划任务，先手动跑稳）。
4. **Webhook 去重与队列** —— 真实环境里 webhook 可能重复投递，可用 `X-Shopify-Webhook-Id` 做幂等键。
5. **多语言/多店铺** —— `email_gen.py` 的 prompt 和模板都是集中的常量，扩展语种成本很低。

---

## 9. 安全与合规

- 默认 mock，**不会向真实店铺发请求**；
- 仓库里没有任何真实 token / API key，也没有真实的客户数据（假数据里的邮箱都是 `example.com`）；
- 密钥只从环境变量读，不落库、不打日志、不进汇报；
- 只读权限（`read_orders`）就够本项目的全部功能，不需要写权限。

---

## 10. 如何改造这份代码（三个由浅入深的练习）

这份代码是**参考实现**，不是终点。建议按下面顺序练，每一步都能独立验收。

### 练习前的安全网（先做这个，2 分钟）

```powershell
# 1) 记下基线数字，改坏了有参照
.\venv\Scripts\python.exe cli.py sync --mock
.\venv\Scripts\python.exe cli.py emails --limit 5
.\venv\Scripts\python.exe cli.py stats      # 记下：订单 20 / 明细 42 / 含退款 2 / 邮件 5

# 2) 先提交一次，改坏了能一键回滚
git init; git add .; git commit -m "baseline: 跑通的参考实现"

# 3) 随时用这两个命令自检
.\venv\Scripts\python.exe -m unittest discover -s tests -v   # 期望 Ran 9 tests OK
.\venv\Scripts\python.exe cli.py stats                        # 期望数字回到基线
```

> 基线（本机实测）：`orders=20  line_items=42  orders_with_refund=2  refund_total=98.4  emails=5`

---

### 练习 ①（理解层）不看代码，讲清数据流：API → DB → LLM → 邮件

**目标**：合上 `README` 和源码，用白板/嘴里把整条链路讲一遍，讲清每一跳**传的是什么数据结构**。

**提示（自测问题，按顺序答）**：

1. 程序入口是谁？`cli.py` 的 `cmd_sync()` 里第一件事做了什么？（建库 + 读水位）
2. 水位从哪张表、哪个 key 读出来？为什么存 DB 而不是存内存？
3. `shopify_client.iter_orders()` 为什么返回的是 `Iterator[list[dict]]` 而不是 `list[dict]`？（分页/流式）
4. `dict` 在哪一步变成 `OrderRow`？为什么要有 `OrderRow` 这个中间结构，而不是把 `dict` 一路传下去？
5. `db.upsert_order()` 凭什么说自己是幂等的？子表为什么要"先删后插"？
6. `email_gen.detect_scenario()` 的判断**顺序**为什么不能调换？
7. 邮件最终写到哪张表、带哪些字段？`provider` 字段存在的意义是什么？

**验收标准**（全部满足才算过）：

- [ ] 能不看代码画出 4 个箭头：`iter_orders → upsert_orders → detect_scenario → insert_email`，并说出每个箭头上数据的具体类型
- [ ] 能准确回答上面 7 个问题，尤其是第 4、5、6 题
- [ ] 能说出"水位"的两个细节：存在 `sync_state` 表 / 每次**回退 5 分钟**（`sync.WATERMARK_SAFETY_MARGIN`）
- [ ] 能解释为什么没有 API key 也能跑完（`config.has_llm` → `generate_template_email` 兜底）
- [ ] 全程没看代码讲完，且在 10 分钟内

> 面试可直接用的一段话：*"轮询用 `updated_at_min` 做增量，水位落在 SQLite 里所以进程重启不丢；写入用订单 ID 做主键做 Upsert，重复投递天然幂等；邮件生成把订单状态收敛成 4 类场景，LLM 和模板共用同一个 `EmailDraft` 出口，所以外部依赖挂了也不影响主流程。"*

---

### 练习 ②（调试层）故意改坏一处，然后自己定位并修好

**目标**：练"从现象反推到代码行"的能力。下面三处任选一处（建议都试），**先不修**，跑一遍看它怎么暴露出来。

| # | 改哪里 | 怎么改（故意写坏） | 预期会看到的现象 |
| --- | --- | --- | --- |
| ②-1 | `email_gen.detect_scenario()` | 把 `if order.refund_count > 0` 那段判断**挪到最后** | `cli.py emails --force` 后 #1004 的场景从"退款安抚"变成"欢迎信"，文案完全跑偏 |
| ②-2 | `db.upsert_order()` | 把 `ON CONFLICT(id) DO UPDATE` 改回普通 `INSERT` | 再跑一次 `cli.py sync --mock` 报 `UNIQUE constraint failed`；`stats` 的订单数不再稳定为 20 |
| ②-3 | `sync.run_sync()` | 把新水位改成用"**同步结束时间**"，并删掉 `- WATERMARK_SAFETY_MARGIN` | 测试 `test_sync_mock_is_idempotent` 仍可能通过，但水位被推到了订单更新时间之后 → 真实环境里"同步期间被更新的订单会被永久漏掉" |

**流程要求**：改坏 → 跑 `cli.py sync --mock` / `cli.py emails --force` / 测试 → **把日志里暴露问题的那一行贴出来** → 定位到具体文件+函数 → 修复 → 回归。

**验收标准**：

- [ ] 三处各改过一次，且都成功还原（`git diff` 显示干净或只剩自己的新功能）
- [ ] 能说出每处 bug 是**被哪条命令或哪个测试**捕获的（②-2 是 `UNIQUE constraint failed`；②-1 是场景标签变了；②-3 要靠推理，因为现有测试测不出来）
- [ ] 能指出 ②-3 为什么**难被抓到**：它不报错、测试也过，只在"同步期间订单被改动"这种时序下丢数据 —— 这类静默 bug 才是面试加分点
- [ ] 顺手补一条测试，让 ② 类 bug 以后能被自动抓住（例如断言水位 `< 订单的 updated_at` 的最大值）
- [ ] 修复后 `unittest` 全绿，且 `cli.py stats` 数字**完全回到基线**

> 进阶思考：②-3 的正确做法是把水位固定为"**本次请求开始时间**"（`sync.run_sync` 里的 `started`），因为 `updated_at_min` 比较的是店铺侧时间，用结束时间会跳过"响应已生成、但本地还没处理完"的那批订单。

---

### 练习 ③（设计层）加一个原设计没有的小功能：按客户国家/语言调整语气

**目标**：新增一个**贯穿 schema → 同步 → 生成 → 展示 → 测试**的完整特性（这是最接近真实需求的一类改动）。

**拆分（建议按这个顺序做，每小步都跑一遍）：**

1. **数据来源**：订单 JSON 里取国家 —— `shipping_address.country_code`（如 `US`/`DE`/`JP`），缺失时用 `billing_address`，再缺失记 `UNKNOWN`。
2. **改 schema**：`orders` 表加 `country TEXT` 列。注意 `CREATE TABLE IF NOT EXISTS` **不会**给已存在的库加列，你得二选一：
   - 删掉 `data/shopify.db` 重跑（最省事，反正都是 mock 数据）；或
   - 写一个 `migrate()`：`PRAGMA table_info(orders)` 查列，缺了就 `ALTER TABLE orders ADD COLUMN country TEXT`。
3. **改 mock 数据**：`shopify_client.build_mock_orders()` 里给 20 条订单**轮着分配若干国家**（至少覆盖 3 个：`US`、`DE`、`JP`），这样测试才有分支可测。
4. **改同步**：`db.upsert_order()` 写入 `country`；`OrderRow` 加字段；`_SELECT_ORDER` 的 SELECT 里带上它。
5. **改生成逻辑**（核心）：在 `email_gen.py` 里加一层"语气维度"。要求 **输出仍然是英文**（需求是英文售后邮件），但按国家调整**称呼正式度、物流时效预期、落款**。例如：
   - `US`：`Hi Emily,` + "3-7 business days"，轻松口吻（现状）
   - `DE`：`Dear Ms. Schmidt,` + "5-10 business days"，更正式、少用感叹号、把退货政策写得更明确
   - `JP`：`Dear Ms. Tanaka,` + 更克制的道歉措辞（`We sincerely apologise for any inconvenience caused.`）

   实现建议：把"语气"做成 `_TONE_BY_COUNTRY: dict[str, Tone]`，模板和 LLM prompt **都读同一个 Tone**，别只改模板（否则接上 DeepSeek 后语气又变回去了 —— 这是最容易踩的坑）。
6. **改展示与统计**：`cli.py orders` 加一列国家；`db.stats()` 加 `emails_by_country`。
7. **补测试**：断言 `DE`/`JP` 的邮件里出现对应称呼、且仍然 **不含中文**（复用现有那条断言）。

**验收标准**（全部满足才算过）：

- [ ] `cli.py sync --mock` 后 `orders` 表里有 `country` 列且有值，`stats` 显示国家分布
- [ ] `cli.py orders --limit 8` 能看到国家列
- [ ] `cli.py emails --force` 生成后，`samples/emails_sample.txt` 里能同时看到至少 **2 种不同语气**（对比 `Hi Emily,` 与 `Dear Ms. Schmidt,`）
- [ ] **有 key 时**（配了 `DEEPSEEK_API_KEY`）语气差异同样生效 —— 说明 Tone 真的接进了 LLM prompt，而不是只在模板里
- [ ] 新增 ≥2 条测试，且 `unittest` 全绿（含原有的 9 条）
- [ ] `provider=template` 与 `provider=deepseek` 两种模式下邮件都不含中文
- [ ] 你能用一句话说清这次改动的**兼容性影响**：老库需要迁移或重建，因为 `CREATE TABLE IF NOT EXISTS` 不补列

> 面试价值：这一节让你可以答"**你改动过一个有多层依赖的特性吗**"——从数据源、schema 迁移、mock 数据、业务分支、到测试全链路，而且你还知道"模板和 prompt 必须共用同一份语气配置"这个坑。

---

### 做完三个练习之后

建议按第 8 节的顺序往下走（SMTP 发信 → 定时任务 → webhook 去重），每加一块都补测试、都跑 `cli.py stats` 对基线。到那时这份代码才真正算"你的"。
