# Reuters 爬虫（Google News RSS + googlenewsdecoder）

> 创建日期：2026-09-10
> 状态：开发中
> 定位：**独立 Python 微服务**（不属于 NestJS server，也不属于小程序）

## 一、目标

通过 Google News RSS 抓取 Reuters 文章，解码出真实 URL，抓取正文/作者/发布时间后入库 `t_articles`，并触发既有 LLM 分析链路。

- 源：Google News RSS（site:reuters.com，关键词 markets/business/stocks/economy/fed/earnings）
- 频率：**每 5 分钟**一轮
- 运行方式：**服务器本地 Python 进程 + systemd 常驻**，挂了自动拉起，写日志文件

## 二、已确认决策

| # | 决策项 | 结论 |
|---|--------|------|
| Q1 | 架构 | **独立 Python 微服务**，通过 **CloudBase OpenAPI** 访问数据库（不直连 MySQL），不依赖 server 运行 |
| Q2 | 部署环境 | **海外服务器**，可直接访问 Google News（无需代理，但保留代理配置项） |
| Q3 | LLM 触发 | **需要**：入库后写 `t_analysis_task(pending)`，复用现有 Worker |
| Q4 | 正文抽取 | **trafilatura**（正文 + 作者 + 发布时间一次完成） |
| Q5 | 部署方式 | **systemd 常驻**（`Restart=always`），日志文件轮转 |

## 三、RSS 源

从浏览器扩展 URL 解出的真实 RSS 地址：

```
https://news.google.com/rss/search?q=site%3Areuters.com+markets+OR+business+OR+stocks+OR+economy+OR+fed+OR+earnings&hl=en-US&gl=US&ceid=US%3Aen
```

配置项 `RSS_URL` 可覆盖（换关键词/市场只需改配置）。

## 四、单轮流程

```
① fetch_rss()         拉 RSS → 条目列表（title / google link / pubDate / source）
② decode_urls()       googlenewsdecoder 解码 → 真实 reuters.com URL
                      gnewsdecoder(url, interval=DECODE_INTERVAL) → {"status", "decoded_url"}
③ filter_new()        按 url 查 t_articles 去重，得到新增条目
④ parse_article()     抓真实页 → trafilatura 抽正文/作者/发布时间
                      抓取策略由 FETCH_MODE 决定：
                      - requests  ：只走 HTTP 直连
                      - playwright：只用 headless 浏览器（绕过 JS 渲染/反爬）
                      - auto（默认）：先 HTTP，失败或抽不到正文再用浏览器兜底
                      浏览器实例全程复用（每篇启停会拖垮 5 分钟周期）
⑤ save_article()      写 t_articles + t_authors + t_article_authors
⑥ trigger_analysis()  upsert t_analysis_task(article_id, 'pending') → Worker 自动接手
⑦ write_log()         写 t_crawler_logs + 本地日志文件
⑧ sleep(300)          进入下一轮
```

**单轮上限** `MAX_PER_ROUND`（默认 20）：防止首次运行 RSS 返回上百条导致单轮超出 5 分钟周期。

## 五、数据模型与字段映射

### `t_articles`（snake_case）

| 字段 | 来源 | 说明 |
|---|---|---|
| `uuid` | `uuid4()` | 唯一 |
| `category` | 固定 `'reuters'` | |
| `title` | RSS 标题（去掉 `" - Reuters"` 后缀） | |
| `summary` | RSS description / 正文前 200 字 | |
| `url` | **解码后的真实 URL** | **去重键**（unique, varchar 500） |
| `content` | trafilatura 正文 | |
| `long_excerpt` | 正文前 500 字 | |
| `publish_time` | RSS `pubDate` 优先，回退页面 meta | varchar(50) |
| `content_length` | `len(content)` | |
| `extraction_strategy` | 固定 `'trafilatura'` | |
| `from_platform` | 固定 `'reuters'` | |
| `data_source` | 固定 `'google_news_rss'` | |
| `extract_time` | 当前时间字符串 | |

### 作者（`t_authors` + 关联表 `t_article_authors`）

- `t_authors`：`uuid` / `name`（按 name 查重，无则新建）
- 关联：多对多，写入 `t_article_authors(article_id, author_id)`

### 触发 LLM（`t_analysis_task`）

`article_id` 有 **unique 索引**，必须 upsert：

```sql
INSERT INTO t_analysis_task (article_id, status, retry_count)
VALUES (%s, 'pending', 0)
ON DUPLICATE KEY UPDATE update_time = VALUES(update_time);
```

> Worker（`ArticleLlmAnalysisWorkerService`）启动与轮询时会扫描 `pending/processing/failed` 任务并消费，无需额外改造。
> ⚠️ 注意：LLM 免费额度已耗尽，分析任务会失败重试直到配额恢复（不影响爬虫本身）。

### 运行日志（`t_crawler_logs`）

`platform='reuters'` / `level` / `action='crawl'` / `message` / `is_success` / `articles_added|skipped|failed` / `duration_ms`

## 六、配置项（`config.example.env`）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CLOUDBASE_ENV_ID` / `CLOUDBASE_SECRETID` / `CLOUDBASE_SECRETKEY` | — | CloudBase 凭证（必填，与 `server/.env.production` 一致） |
| `CLOUDBASE_RDB_INSTANCE` / `CLOUDBASE_RDB_DATABASE` | 默认/环境ID | MySQL 实例与库名（留空则用 default / envId） |
| `CLOUDBASE_OPENID` | `system` | CloudBase 表若含 NOT NULL 的 `_openid` 列时补的值 |
| `RSS_URL` | 上文 RSS | 可换关键词 |
| `INTERVAL_SECONDS` | `300` | 轮询间隔 |
| `MAX_PER_ROUND` | `20` | 单轮最大新文章数 |
| `DECODE_INTERVAL` | `1` | 解码间隔（秒），防 Google 429 |
| `HTTP_TIMEOUT` | `30` | 抓取超时 |
| `USER_AGENT` | 常规浏览器 UA | 抓取用 |
| `FETCH_MODE` | `auto` | `requests` / `playwright` / `auto`（先 HTTP，失败或抽不到正文则用浏览器兜底） |
| `BROWSER_HEADLESS` | `true` | 无头模式 |
| `BROWSER_TIMEOUT_MS` | `30000` | 浏览器导航超时 |
| `BROWSER_WAIT_SELECTOR` | 空 | 纯 JS 渲染页可填选择器（如 `article`） |
| `BROWSER_WAIT_MS` | `1000` | 未配选择器时的固定等待毫秒 |
| `HTTP_PROXY` / `HTTPS_PROXY` | 空 | 可选代理（海外部署通常不需要） |
| `NODRIVER_HEADLESS` | `false` | DataDome 能识别 headless，必须非 headless（服务器用 `xvfb-run`） |
| `NODRIVER_WAIT_SECONDS` | `15` | 挑战页 JS 执行等待秒数（原 8s，实测不足以完成挑战跳转） |
| `NODRIVER_MAX_SWITCH` | `3` | 单篇被拦后最多切换多少次身份重试 |
| `NODRIVER_REQUEST_INTERVAL` | `5` | 每篇抓取后的冷却秒数（防 IP 被拉黑） |
| `NODRIVER_DUMP_BLOCKED` | `false` | 被判定拦截时把 HTML 落盘到 `{LOG_DIR}/blocked/`，用于定位挑战页类型 |
| `NODRIVER_FAIL_FAST_THRESHOLD` | `3` | 连续 N 篇文章全被拦后本轮跳过该通道（0=不熔断） |
| `LOG_DIR` | `./logs` | 日志目录 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `TRIGGER_LLM` | `true` | 是否触发 LLM 分析 |

## 七、部署（systemd）

服务文件 `deploy/reuters-crawler.service`：

- `Restart=always` + `RestartSec=10` → 进程挂掉自动拉起
- `WorkingDirectory` + venv 绝对路径
- 日志同时输出文件（RotatingFileHandler，10MB × 5）与 journal

```bash
sudo cp deploy/reuters-crawler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now reuters-crawler
sudo systemctl status reuters-crawler
journalctl -u reuters-crawler -f
```

## 八、容错设计

| 场景 | 处理 |
|---|---|
| RSS 拉取失败 | 记 error 日志 + 写 `t_crawler_logs`，本轮跳过，下轮重试 |
| 单条 URL 解码失败 | 跳过该条，不影响本轮其它条目 |
| 正文抓取失败（403/超时） | 跳过该条，计数 `failed` |
| 数据库连接断开 | 每轮重连；连续失败记录日志（由 systemd 决定是否重启） |
| 单轮超时 | `MAX_PER_ROUND` 限流 |
| 进程崩溃 | systemd `Restart=always` 自动拉起 |

## 九、验收标准

1. 单轮可跑通：拉 RSS → 解码 → 抓正文 → 入库，日志可见每轮统计（新增/跳过/失败）。
2. 重复运行不产生重复文章（按 `url` 去重）。
3. 入库文章自动出现 `t_analysis_task` 的 `pending` 记录。
4. systemd 下 `kill -9` 进程后能在 10 秒内自动拉起。
5. 日志文件按大小轮转，不无限增长。

## 十、开发记录

- **2026-09-15 服务器实测（无可用代理）**：确认拦截发生在 **IP 层**（三指纹均 1546 字节 + curl_cffi 401），
  换身份重试为无效功；据此实施 P0（dump 诊断 + 安装 playwright 内核 + 启动失败禁通道）、
  P1（连续失败熔断）、P2（等待 8s→15s），详见 [11.4](#114-服务器实测ip-层拦截与诊断增强2026-09-15-实施)。

- **2026-09-10 需求确认**：Q1 独立 Python 微服务直连 MySQL；Q2 部署海外（无需代理，保留配置）；Q3 入库后触发 LLM；Q4 正文抽取用 trafilatura；Q5 systemd 常驻 + 日志 + 崩溃自动拉起。
- **2026-09-10 勘察结论**：既有 `server/src/reuters/` 的 `runCrawlJob` 为空壳（只写日志）且走 TypeORM（生产是 cloud_mysql），从未真正抓取；本爬虫是第一个可落地的 Reuters 爬虫，入库字段映射复用其定义。
- **2026-09-10 开发完成**：
  - `src/config.py`（含最小 .env 解析）、`logging_setup.py`（RotatingFileHandler 10MB×5）、`rss_fetcher.py`、`url_decoder.py`、`article_parser.py`、`repository.py`、`scheduler.py`、`main.py`
  - `repository` 自动探测 CloudBase 表的 `_openid` 列（NOT NULL 时补 `system`），避免环境差异导致 INSERT 失败
  - `t_analysis_task.article_id` 有唯一索引 → 触发 LLM 用 `ON DUPLICATE KEY UPDATE` upsert
  - `deploy/reuters-crawler.service`：`Restart=always` + `RestartSec=10` + `StartLimitBurst=5`（防重启风暴）
- **2026-09-10 测试**：单元测试 22 项全部通过（`python -m unittest discover -s tests -t . -v`）；`main.py --help` 与配置校验均正常。
- **2026-09-10 已知限制**：本地（国内）网络 `news.google.com` 不可达（`ConnectTimeout`），端到端集成测试需在海外服务器执行（与部署决策一致）。
- **2026-09-10 正文抓取升级为 headless 浏览器**：
  - 新增 `src/browser_fetcher.py`（Playwright Chromium，单例复用 browser + context，每篇只 new_page）
  - `src/article_parser.py` 重构为 `fetch_html_with_requests` / `extract_article` / `fetch_article_detail` 三层，支持 `requests` / `playwright` / `auto` 三种模式
  - 默认 `auto`：先 HTTP 快通道，**失败或抽不到正文才启动浏览器**，兼顾速度与成功率
  - `scheduler.create_browser()` 统一管理生命周期（常驻与 `--once` 单轮均覆盖），进程退出前 `close()`
  - 浏览器启动参数含 `--no-sandbox --disable-dev-shm-usage --disable-blink-features=AutomationControlled`（容器与反自动化检测）
  - 部署需额外执行 `playwright install chromium`（Debian/Ubuntu 缺依赖时 `playwright install-deps chromium`）
- **2026-09-10 新增 `--dry-run` 诊断模式**：不连数据库，只跑「RSS → 解码 → 抓正文」并打印挖掘到的标题/作者/时间/正文字数/摘要，用于在海外服务器快速验证「能否挖到内容」（`python main.py --once --dry-run`，失败返回退出码 1）。
- **2026-09-10 修复**：`--once` 单轮模式下异常（如 RSS 网络超时）原本直接抛 traceback 崩溃，现改为记录清晰错误日志并返回退出码 1。
- **2026-09-10 本地实测（经 SOCKS5 代理访问海外，全链路已跑通）**：
  - **RSS**：Google 对「只带 User-Agent 不带 Accept」的请求返回 **503**（实测：无头 2/3 失败，带完整头 3/3 成功）→ 固定带 `Accept` + `Accept-Language`，并加 3 次指数退避重试（实测重试 1 次即成功，拿到 **100 条**）
  - **解码**：`googlenewsdecoder` 正常，3 条 Google 链接全部解出真实 `reuters.com` 地址
  - **正文**：**被 Reuters 的 Akamai Bot Manager 拦截**（返回 1566 字节挑战页，正文 0 字）
    - 已验证**非浏览器问题**：同浏览器访问 BBC（252 字）、AP News（325 字）均正常抽取
    - 已尝试且无效：延长等待 10s、`playwright-stealth` 反检测、AMP 页面、带 utm 查询参数
    - 推断与该代理出口 IP 被标记有关，**需在海外服务器上用其原生 IP 复测**
  - 新增**降级策略**：抓不到正文时用 RSS 摘要入库并标记 `extraction_strategy='rss_fallback'`（`ALLOW_RSS_FALLBACK`，默认开），保证有数据可积累、后续可补抓
  - 实测产出：`added=3, degraded=3, failed=0`（标题/真实 URL/发布时间完整，正文为摘要）
  - 注：本机 Python 3.9 + LibreSSL 2.8.3 导致 requests 访问 Reuters 出现 `SSLV3_ALERT_HANDSHAKE_FAILURE`，**服务器上（Python 3.13 + OpenSSL 3）无此问题**

## 十一、反爬与降级（实测结论）

| 环节 | 状态 | 应对 |
|------|------|------|
| Google News RSS | ✅ 可用 | 必须带完整请求头（`Accept` + `Accept-Language`）+ 重试（偶发 503 限流） |
| Google News 链接解码 | ✅ 可用 | `interval` 控制节奏，防 429 |
| Reuters 正文 | ⚠️ **Akamai 强反爬** | 浏览器 + stealth 仍被拦（代理 IP）；**待海外原生 IP 复测**；失败时降级 RSS 摘要 |
| BBC / AP News 正文 | ✅ 可用 | 同浏览器实测正常，说明抓取链路本身无问题 |

**降级策略**：`ALLOW_RSS_FALLBACK=true`（默认）时，正文抓取失败会用 RSS 摘要入库，
`extraction_strategy` 标记为 `rss_fallback`，便于后续识别并补抓完整正文。

### 11.1 反爬增强（2026-09-11 实施）

| 手段 | 作用 |
|---|---|
| **UA 身份池轮换** | 内置 5 款真实浏览器身份，每次请求轮换；UA + 配套头 + TLS 指纹三者一致 |
| **curl_cffi 通道** | 模拟真实浏览器 **TLS/JA3 指纹**——过 Cloudflare/Akamai 的关键（换 UA 无效） |
| **playwright + stealth** | 保留为兜底（需 JS 渲染或前两档失败时） |
| `auto` 编排 | curl_cffi → 失败/抽不到正文 → 浏览器 → 仍失败则降级 RSS 摘要 |

**实测结论（本地经代理）**：
- Google News RSS：完整请求头 + 重试后可稳定拉取（100 条）
- Reuters 防护真身：**CloudFront + DataDome**（响应体含 `var dd={'rt':...}`）
- BBC / AP News：浏览器可正常抽取正文（证明链路本身无问题）

### 11.2 nodriver 通道：DataDome 突破（2026-09-11 实施并验证）

| 方式 | 结果 |
|---|---|
| requests / curl_cffi | ❌ 挑战要求执行 JS，纯 HTTP 原理上过不去 |
| playwright + stealth（headless） | ❌ 1584 字节挑战页 |
| playwright **有头模式** | ❌ 仍被识别 |
| **nodriver（有头）** | ✅ **635KB / 4445 字真实正文** |

**原理**：nodriver 走纯 CDP，不加载 webdriver、不注入 `navigator.webdriver` 等自动化
属性、每次全新 browser profile，DataDome 无法识别。

**两个自动能力（均已实测生效）**：
1. **身份自动切换**：每次请求从 5 款真实浏览器身份轮换（UA + 配套头 + `--user-agent` 启动参数一致）
2. **被检测后自动换身份重试**：`is_blocked()` 识别 DataDome 挑战页（签名匹配 + 响应过小）
   → 关闭浏览器 → 取下一个身份重启 → 重试，最多 `NODRIVER_MAX_SWITCH` 次
   - 实测：第 1 次 `chrome124-win` 被拦 → 自动换 `chrome131-mac` → 成功

**部署要求**：
- **Python 3.10+**（nodriver 语法要求；低版本延迟导入 → 通道自动跳过，不崩溃）
- **必须非 headless**（DataDome 能识别 headless；`headless=True` 实测必被拦）
  → 服务器无显示器需 `xvfb-run -a` 启动（见 README）
- 日志修复：用 `uc.loop().run_until_complete()` 替代 `asyncio.run()`，避免反复
  创建/关闭事件循环产生 "Loop ... is closed" 告警

### 11.3 数据库访问改为 CloudBase OpenAPI（2026-09-14 改造）

**背景**：原方案用 PyMySQL 直连 MySQL，需要公网地址 + IP 白名单 + 数据库账号密码，
且 CloudBase 托管库的连接信息获取与维护成本高。

**改造**：改为与 server 侧一致的 CloudBase OpenAPI 方式：

```
secretId/secretKey ──TC3 签名──> /auth/v1/token/clientCredential ──> access_token
                                        ↓
                            /v1/rdb/rest/{table}（PostgREST 风格）
```

**关键结论**：CloudBase **无官方 Python 服务端 SDK**（PyPI 无 `cloudbase` / `tcb` 包），
故按其 Node SDK（`@cloudbase/node-sdk`）协议在 `src/cloudbase_client.py` 复刻。

**复刻要点（已实证）**：
- 网关：`https://{envId}.api.tcloudbasegateway.com`（非中国大陆区域为 `.api.intl.`）
- 签名：标准 TC3-HMAC-SHA256，service=`tcb`，与官方 SDK 逐位比对一致
- ⚠️ `Authorization` 末尾必须追加 `, Timestamp={秒级时间戳}`（缺失会 401，曾排查较久）
- token 有效期 432000s，已做缓存与提前刷新
- 访问 rdb 需带 `X-Db-Instance` / `Accept-Profile` / `Content-Profile` 头

**兼容性处理**：
- `_openid`：REST 无法查 INFORMATION_SCHEMA，采用「先带 `_openid` 写入，
  报列不存在则自动去掉并记住该表」的降级策略
- upsert：`t_analysis_task.article_id` 唯一索引 → 用 `Prefer: resolution=merge-duplicates`
- CloudBase 为国内服务，**不走** `HTTP_PROXY`（该代理仅供爬虫访问 Google/Reuters）

**实测结果**（2026-09-14）：
```
CloudBase token 获取成功（有效期 432000s）
入库成功 id=303
t_analysis_task id=158 status=pending（触发成功）
t_crawler_logs id=7 记录正确
```

### 11.4 服务器实测：IP 层拦截与诊断增强（2026-09-15 实施）

**实测现象**（海外服务器，`xvfb-run -a ./venv/bin/python main.py --once --dry-run`）：

```
auto 抓取失败，转浏览器: HTTP Error 401                      ← curl_cffi
第 1/3 次被反爬拦截（身份=edge124-win，1546 字节）            ← nodriver
第 2/3 次被反爬拦截（身份=firefox133-win，1546 字节）
第 3/3 次被反爬拦截（身份=safari17-mac，1577 字节）
浏览器抓取失败: Executable doesn't exist at .../chrome-headless-shell  ← playwright 内核未装
抓不到正文，降级使用 RSS 摘要
```

**结论**：
1. **三种完全不同的指纹（edge/firefox/safari）返回几乎一致的 1546 字节**，且 curl_cffi 先拿到 401
   → 排除 UA/指纹因素，判定为**出口 IP 被 Reuters 边缘标记**（云主机 IP 段封禁是常态）。
   无可用代理时，**换身份重试属于无效功**，只会拖长单轮耗时。
2. 1546 字节走的是 `is_blocked()` 中「响应过小」分支，**签名是否命中未知** → 必须先拿到内容才能定论。
3. Playwright 内核未安装是独立故障（且 headless 对 DataDome 本就无效），但当前实现会
   **每篇文章都重试启动一次**，20 篇 = 20 条噪音日志。

**实施项**：

| 优先级 | 改动 | 作用 |
|---|---|---|
| P0 | `NODRIVER_DUMP_BLOCKED` 开关，被拦时 HTML 落盘 `{LOG_DIR}/blocked/` | 唯一能确定挑战页类型的手段；缺省关闭，零开销 |
| P0 | 服务器执行 `playwright install chromium`；`BrowserFetcher` 启动失败后**本进程内禁用该通道** | 消除每篇重复的启动失败噪音 |
| P1 | **连续失败熔断**：连续 `NODRIVER_FAIL_FAST_THRESHOLD`（默认 3）篇全被拦 → 本轮剩余文章跳过 nodriver | 单轮耗时从 ~17 分钟降到 ~1 分钟级 |
| P2 | `NODRIVER_WAIT_SECONDS` 默认 8 → 15 | 若为挑战页等待不足，此项才有效（待 dump 结果确认） |

**熔断设计要点**：
- 计数按「**文章**」而非「身份重试次数」：同一篇内换 3 次身份仍算 1 次失败；
- 成功抓取即清零并解除熔断；
- 每轮 `run_once` 重新创建 `NodriverFetcher`，故**每轮自动重新探测一次**（IP 可能已恢复），
  最多浪费 1 篇的探测成本（≈3 次身份切换）；
- 提供 `reset()` 供外部复用实例时手动解除。

## 十二、LLM 分析微服务（2026-09-11 新增，2026-09-11 晚 迁移为 Go 实现）

> **现状：分析服务已迁移为独立 Go 微服务 `llm-analysis-server`**，本目录（crawler）
> 只保留「入库后写 `t_analysis_task(pending)`」的触发逻辑。
> Go 版设计文档见 `../llm-analysis-server/README.md`，以下为背景与职责说明。

> 背景：原设计「Python 写 `pending` 任务 → server worker 消费」经核实**不可行**——
> server worker 只在 `onModuleInit` 扫描一次数据库，运行期间仅消费内存队列，
> 从不轮询数据库，导致 Python 新建的任务会永久堆积（除非 server 重启）。

**为何改用 Go**：常驻内存约 20~50MB（Python 版约 150~250MB），对 1GB 内存 VPS 关键；
静态编译、无运行时依赖，部署只需一个二进制。功能与 Python 版完全对齐
（轮询、抢占、额度保护、指数退避、结果落库）。

### 12.1 服务边界（微服务拆分）

| 服务 | 入口 | 职责 | 不负责 |
|---|---|---|---|
| 爬虫服务 | `main.py` | RSS → 解码 → 抓取 → 入库 `t_articles` → 写 `pending` 任务 | 不调模型 |
| **分析服务** | Go 微服务 `llm-analysis-server` | 轮询 `pending` → 调 LLM → 写 `t_article_llm_analysis` → 更新任务状态 | 不抓数据 |

两服务通过 **`t_analysis_task` 表**解耦（共享数据库，不引入 MQ），各自独立进程、独立 systemd 单元、独立扩缩容。

### 12.2 已确认决策

| # | 决策项 | 结论 |
|---|--------|------|
| 1 | 与 server worker 共存 | **停掉 server 侧 worker**，分析全部收口到 Python 分析服务（避免重复消费烧 token） |
| 2 | 触发与限流 | **轮询 + 配额上限**：每 30s 轮询，单轮最多 N 条（可配），失败重试上限 2 次，额度耗尽自动降级不无限重试 |

### 12.3 分析服务流程

```
每 ANALYZER_POLL_SECONDS(30s):
  1. 查询待处理任务：status IN (pending) 或 (failed 且 retry_count < max 且 next_retry_at 到期)
  2. 抢占：UPDATE status='processing'（防止多实例重复消费）
  3. 取文章正文（t_articles.content / long_excerpt）
  4. 调 DashScope（OpenAI 兼容接口，qwen3.7-max + 系统 prompt）
  5. 写 t_article_llm_analysis（复用现有表结构，前端零改动）
  6. 任务置 success；失败则 retry_count+1 + next_retry_at 指数退避
  7. 写 t_crawler_logs
```

**降级**：识别 LLM 额度类错误（`Free quota exhausted` / `403` / `insufficient_quota`），
命中后本轮停止并拉长退避，**不无限重试烧钱**。

### 12.4 配置项（新增）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | — | 阿里百炼 key（必填） |
| `DASHSCOPE_BASE_URL` | 百炼兼容模式地址 | 见 server/.env.production |
| `DASHSCOPE_MODEL` | `qwen3.7-max` | 模型 |
| `ANALYSIS_SYSTEM_PROMPT` / `ANALYSIS_USER_PROMPT` | 内置默认 | 与 server 保持一致 |
| `ANALYZER_POLL_SECONDS` | `30` | 轮询间隔 |
| `ANALYZER_MAX_PER_ROUND` | `5` | 单轮最大处理条数（配额保护） |
| `ANALYZER_MAX_RETRY` | `2` | 单任务最大重试次数 |
| `ANALYZER_CONCURRENCY` | `2` | 并发分析数 |
| `ANALYZER_TIMEOUT` | `180` | 单次 LLM 调用超时（秒） |

### 12.5 停用 server 侧 worker

在 `ArticleLlmAnalysisWorkerService.onModuleInit` 增加开关：
`ANALYSIS_WORKER_ENABLED=false`（默认 true）时跳过启动，避免与新分析服务重复消费。
配置在 `server/.env.production`，可随时回滚。

### 12.6 验收标准

1. 分析服务独立启动后，能消费 `t_analysis_task` 的 pending 任务并写入 `t_article_llm_analysis`。
2. server 侧 `ANALYSIS_WORKER_ENABLED=false` 后不再消费任务（无重复分析）。
3. 额度耗尽时自动停止本轮并退避，不无限重试。
4. 单任务失败重试上限 2 次，超过后 `status=failed` 并记录 `last_error`。
5. `kill -9` 后 systemd 10 秒内自动拉起；分析服务崩溃不影响爬虫服务。

> 测试用例详见 [TEST-CASES.md](./TEST-CASES.md)。
