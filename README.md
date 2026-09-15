# Reuters 爬虫（Google News RSS）

独立 Python 微服务：通过 Google News RSS 抓 Reuters 文章 → `googlenewsdecoder` 解码真实 URL →
`trafilatura` 抽取正文/作者/发布时间 → 直连 MySQL 入库 `t_articles` → 写 `t_analysis_task(pending)`
触发既有 LLM 分析链路。

需求文档见 [REQUIREMENTS.md](./REQUIREMENTS.md)。

## 快速开始

```bash
cd crawler

# 1) 虚拟环境 + 依赖
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 1.1) headless 浏览器内核（FETCH_MODE=auto 或 playwright 时必需）
playwright install chromium
# Debian/Ubuntu 若缺系统依赖，再执行：
# sudo playwright install-deps chromium

# 2) 配置
cp config.example.env .env
vim .env          # 填 CLOUDBASE_ENV_ID / CLOUDBASE_SECRETID / CLOUDBASE_SECRETKEY

# 3) 诊断：不连数据库，只看能不能挖到内容（**推荐第一步**）
python main.py --once --dry-run

# 4) 真实跑一轮入库（需要 .env 已配好数据库）
python main.py --once

# 5) 常驻运行
python main.py
```

## 部署

> **完整部署文档见 [DEPLOYMENT.md](./DEPLOYMENT.md)**：服务器要求、一键/手动部署、
> 配置说明、验证步骤、运维命令、故障排查、反爬要点、退路方案。

### 一键部署（推荐）

```bash
git clone -b feature/reuters-crawler-llm-analyzer \
  https://git.weixin.qq.com/sanshox/news-server.git
cd news-server/crawler
sudo bash deploy/setup.sh
```

脚本自动完成 8 步：系统依赖 → swap → Chrome → venv + Python 依赖 → 生成 `.env` → 注册 systemd。

| 可选参数 | 作用 |
|---|---|
| `--with-playwright` | 额外装 playwright 内核（兜底通道，默认不装） |
| `--no-chrome` | 已装 Chrome 时跳过 |
| `--no-systemd` | 只装环境，不注册服务 |
| `SWAP_SIZE=4G` | 自定义 swap 大小 |

部署后**必须**编辑 `.env`（填 `DB_*` 与 `DASHSCOPE_API_KEY`），
然后按 DEPLOYMENT.md 的验证步骤跑一遍。

### 手动部署（等价步骤）

```bash
# 1) 系统依赖（xvfb + Chrome 运行库）
sudo apt update && sudo apt install -y xvfb python3-venv python3-pip \
  libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 libgbm1 libasound2 \
  libatk-bridge2.0-0 libnss3 libcups2

# 2) Chrome（nodriver 依赖）
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y ./google-chrome-stable_current_amd64.deb

# 3) swap（小内存机器必做）
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# 4) 代码与依赖
sudo mkdir -p /opt/reuters-crawler
sudo rsync -a --exclude venv --exclude logs --exclude .env ./ /opt/reuters-crawler/
cd /opt/reuters-crawler
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
sudo cp config.example.env .env      # 然后编辑填入真实值
sudo chown -R www-data:www-data /opt/reuters-crawler

# 5) systemd
sudo cp deploy/reuters-crawler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now reuters-crawler
```

常用命令：

```bash
sudo systemctl status reuters-crawler
sudo systemctl restart reuters-crawler
journalctl -u reuters-crawler -f          # 实时日志
tail -f /opt/reuters-crawler/logs/crawler.log
```

**自动拉起**：服务文件已配置 `Restart=always` + `RestartSec=10`，进程崩溃后 10 秒自动重启；
`StartLimitIntervalSec/StartLimitBurst` 防止数据库故障时出现重启风暴。

验证自动拉起：

```bash
PID=$(systemctl show -p MainPID --value reuters-crawler)
sudo kill -9 $PID
sleep 12
systemctl show -p MainPID --value reuters-crawler   # 应为新的非 0 PID
```

## 数据库访问：CloudBase OpenAPI（不直连 MySQL）

爬虫通过 CloudBase 官方 HTTP API 访问数据库，与 server 侧（`@cloudbase/node-sdk` 的 `app.rdb()`）同一套机制：

```
secretId/secretKey ──TC3 签名──> POST /auth/v1/token/clientCredential ──> access_token
                                        ↓
                            /v1/rdb/rest/{table}（PostgREST 风格）
```

**优势**：无需数据库公网地址、无需 IP 白名单、无需数据库账号密码，只用 CloudBase 凭证鉴权。

> ⚠️ CloudBase **没有官方 Python 服务端 SDK**（PyPI 上无 `cloudbase` / `tcb` 包，
> 其服务端 SDK 仅覆盖 Node.js / Java / PHP / Go）。
> 本项目按其 Node SDK 协议精确复刻于 `src/cloudbase_client.py`——
> TC3-HMAC-SHA256 签名已与官方 SDK **逐位比对一致**，
> 且实测可正常换取 token 并读写 `t_articles` / `t_analysis_task` / `t_crawler_logs`。
>
> 复刻时的两个关键点（缺失会 401）：
> 1. `Authorization` 末尾必须追加 `, Timestamp={秒级时间戳}`
> 2. 参与签名的头需包含 `host` / `content-type` / `x-client-timestamp` / `x-sdk-version` 等

## 配置说明

见 `config.example.env`，关键项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `RSS_URL` | site:reuters.com + 财经关键词 | 换市场/关键词直接改这里 |
| `INTERVAL_SECONDS` | `300` | 每轮间隔 |
| `MAX_PER_ROUND` | `20` | 单轮最大新文章数（防首轮超时） |
| `DECODE_INTERVAL` | `1` | 解码间隔（秒），防 Google 429 |
| `FETCH_MODE` | `auto` | `requests` 快速 / `playwright` 浏览器 / `auto` 先快后兜底（推荐） |
| `BROWSER_HEADLESS` | `true` | 无头模式 |
| `BROWSER_TIMEOUT_MS` | `30000` | 浏览器导航超时 |
| `BROWSER_WAIT_SELECTOR` | 空 | 纯 JS 页可填选择器（如 `article`）等待渲染 |
| `BROWSER_WAIT_MS` | `1000` | 未配选择器时的固定等待毫秒 |
| `TRIGGER_LLM` | `true` | 是否写 `t_analysis_task` 触发分析 |
| `HTTP_PROXY` / `HTTPS_PROXY` | 空 | 海外部署通常不需要 |

### 抓取策略说明

| 模式 | 行为 | 适用场景 |
|---|---|---|
| `requests` | 普通 HTTP（无 TLS 伪装） | 目标站无反爬，追求最快 |
| `curl_cffi` | 模拟真实浏览器 **TLS/JA3 指纹** | 过 Cloudflare 的 TLS 检测，比浏览器快 10 倍以上 |
| `nodriver` | **反检测浏览器**（纯 CDP，无 webdriver 痕迹） | **突破 DataDome 的关键**（实测 Reuters 全靠它） |
| `playwright` | headless 浏览器（可执行 JS） | 无强反爬站点的轻量兜底 |
| `auto`（默认） | `curl_cffi` → `nodriver` → `playwright` 逐级兜底 | 兼顾速度与成功率 |

### nodriver 通道（突破 DataDome）

Reuters 使用 **CloudFront + DataDome** 双重防护，实测：

| 方式 | 结果 |
|---|---|
| requests / curl_cffi | ❌ 挑战要求执行 JS，纯 HTTP 原理上过不去 |
| playwright + stealth、**有头模式** | ❌ 仍被识别（1584 字节挑战页） |
| **nodriver（有头）** | ✅ **635KB / 4445 字正文** |

**两个自动能力**（均已实测生效）：
1. **UA/身份自动切换**：每次请求从 5 款真实浏览器身份中轮换（UA + 配套头 + 启动参数一致）
2. **被检测后自动换身份重试**：识别到 DataDome 挑战页 → 关闭浏览器 → 换身份重启 → 再试
   （`NODRIVER_MAX_SWITCH` 控制最多切换几次）

**实测补充（务必注意）**：
- **无头模式不可用**：传统 `headless` 与新版 `--headless=new` 均被拦（1584 字节挑战页），
  从未成功过——不要在这上面浪费时间，直接上有头 + xvfb。
- **IP 会被拉黑**：密集请求会让出口 IP 被 DataDome 快速标记。实测连续抓取数十次后，
  即便有头模式也被拦（此前同样的配置是成功的）。因此务必：
  - 设置 `NODRIVER_REQUEST_INTERVAL`（默认 5 秒/篇）冷却
  - 控制 `MAX_PER_ROUND`（单轮篇数，默认 20 偏高，建议 5~10）
  - 服务器使用新 IP 首次运行成功率最高；若被封需换 IP 或等待较长时间冷却

#### 小内存 VPS（1~2 GB）注意事项

Chrome 有头 + xvfb 对内存要求较高，1 GB 内存机器需额外处理，否则容易 OOM：

```bash
# 1) 增大 swap（1GB 内存的机器强烈建议加到 2GB）
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# 2) 降低单轮抓取量，避免长时间占用内存
MAX_PER_ROUND=3 NODRIVER_REQUEST_INTERVAL=8

# 3) 观察内存
free -h
# 若频繁 OOM（dmesg | grep -i oom），说明需要升级内存
```

代码层面已内置省内存启动参数（`--disable-gpu`、`--disable-dev-shm-usage`
、`--disable-extensions`、`--disable-background-networking` 等）。

> 若 1 GB 内存下 Chrome 仍不稳定，退路：
> - 只跑爬虫、暂不启用 nodriver（`FETCH_MODE=requests`），接受 RSS 标题/摘要降级
> - 或改用 BBC / AP News 等源（同样需要浏览器，但内存压力相同）
> - 或升级到 2 GB 内存（Racknerd 等厂商通常可加内存）

#### 无桌面服务器部署（Debian 12/13）

没有可视化桌面**不影响**——xvfb 在内存中模拟虚拟显示，Chrome 仍以「有头」模式运行：
它需要的是「有没有显示器」，而不是「有没有桌面」。

```bash
# 1) 虚拟显示
sudo apt update && sudo apt install -y xvfb

# 2) Chrome（nodriver 依赖；Debian 默认不带）
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y ./google-chrome-stable_current_amd64.deb

# 3) Chrome 运行依赖（缺库会导致启动失败）
sudo apt install -y libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 \
                    libgbm1 libasound2 libatk-bridge2.0-0 libnss3 libcups2

# 4) 验证
which Xvfb && which google-chrome
xvfb-run -a google-chrome --version     # 输出版本号即正常

# 5) 运行
cd /opt/reuters-crawler && source venv/bin/activate
xvfb-run -a python main.py --once
```

systemd 服务已内置 xvfb（`ExecStart=/usr/bin/xvfb-run -a ...`），启用后无需手动加。

> ⚠️ **重要**：DataDome 能识别 headless 模式（`headless=True` 必被拦），所以默认 `headless=False`。
> ⚠️ 另需 **Python 3.10+**（nodriver 语法要求）；低版本环境该通道自动跳过，不影响其余功能。

**为什么必须有 curl_cffi**：Cloudflare / Akamai 主要通过 **TLS 指纹**识别
`requests`/`urllib`（与 UA 无关，换 UA 也会被识别）。curl_cffi 让 TLS 握手特征与真实
Chrome/Firefox 一致，是性价比最高的绕过方式。

**UA 身份轮换**（`UA_ROTATION`，默认 `round_robin`）：内置 5 款真实浏览器身份
（Chrome 124/131、Edge、Firefox 133、Safari 17）。每款身份的
**UA + 配套请求头（sec-ch-ua 等）+ TLS 指纹三者保持一致**——例如 Firefox 不发
`sec-ch-ua`，Safari 用 `safari17_0` 指纹，避免「UA 说 Chrome、指纹却是别的」这种更易被识别的组合。

> 浏览器实例**全程复用**（不是每篇启停），否则单轮耗时会严重超出 5 分钟周期。
>
> ⚠️ 本地经 SOCKS5 代理调试时，curl_cffi 会出现 TLS 握手失败（已知兼容问题），
> 此时会自动回退浏览器——**不影响功能**，服务器上直连无此问题。

## LLM 分析服务（已迁移为独立 Go 服务）

分析服务已迁移到独立 Go 微服务：**`../llm-analysis-server`**（编译/配置/部署见其 [README](../llm-analysis-server/README.md)）。

```
爬虫服务（Python, main.py）              分析服务（Go, llm-analysis-server）
  抓取 → 入库 → 写 pending 任务  ──>  t_analysis_task  ──>  轮询(30s) → 调 LLM → 写结果
```

**为什么独立**：原 server worker 只在启动时扫描一次数据库，运行期间不轮询，
外部新建的任务会永久堆积。独立服务主动轮询解决该问题。

**为什么用 Go**：常驻内存约 **20~50MB**（Python 版约 150~250MB），对 1GB 内存 VPS 非常关键；
且静态编译、无运行时依赖，部署只需拷一个二进制。

### 本服务（爬虫）只需做一件事

```bash
TRIGGER_LLM=true    # 默认：入库后写 t_analysis_task(pending)，由 Go 服务消费
```

LLM 相关配置（`DASHSCOPE_API_KEY`、`ANALYZER_*`、prompt）请在 **`llm-analysis-server/.env`** 配置，不在本服务。

### 停用 server 侧 worker（重要）

两端同时消费 `t_analysis_task` 会重复分析、重复烧 token。已为 server worker 加开关，
在 `server/.env.production` 设置：

```bash
ANALYSIS_WORKER_ENABLED=false
```

## 目录结构

```
crawler/
├── main.py                 爬虫服务入口（--once 单轮 / 常驻）
├── requirements.txt
├── config.example.env
├── src/
│   ├── config.py           配置加载（含 .env 解析）
│   ├── logging_setup.py    日志（文件轮转 10MB×5 + 控制台）
│   ├── rss_fetcher.py      RSS 拉取与标题清洗
│   ├── url_decoder.py      googlenewsdecoder 解码
│   ├── article_parser.py   trafilatura 正文/作者/时间
│   ├── repository.py       MySQL 读写（自动探测 _openid）
│   ├── scheduler.py        爬虫单轮流程 + 循环调度
│   ├── user_agents.py      浏览器身份池（UA + 请求头 + TLS 指纹）
│   └── nodriver_fetcher.py 反检测浏览器通道（突破 DataDome）
├── deploy/
│   ├── setup.sh                  一键部署脚本（系统依赖/swap/Chrome/venv/systemd）
│   └── reuters-crawler.service   爬虫服务 systemd（含 xvfb）
├── DEPLOYMENT.md                 部署文档（服务器要求/验证/运维/故障排查）
└── tests/
```

## 代理（国内环境调试用）

爬虫本身设计运行在海外服务器（直连 Google）。若在本地调试，可让 `requests` 与 Playwright 走本地代理：

```bash
export HTTPS_PROXY=socks5://127.0.0.1:1080
export HTTP_PROXY=socks5://127.0.0.1:1080
python main.py --once --dry-run
```

支持 `socks5://` / `http://` 两种格式（Playwright 会自动取 `HTTPS_PROXY`）。

## 注意事项

- **数据库白名单**：CloudBase MySQL 需放行爬虫服务器 IP，否则连不上。
- **LLM 额度**：`TRIGGER_LLM=true` 时入库即产生分析任务；当前免费额度已耗尽，
  任务会失败重试直到配额恢复——**不影响爬虫本身**。
- **去重**：按 `t_articles.url`（解码后的真实 URL）判重，重复运行不会产生重复文章。
