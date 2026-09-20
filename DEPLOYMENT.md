# 部署文档（Reuters 爬虫服务）

> **LLM 分析服务已迁移为独立 Go 微服务**：`../llm-analysis-server`
> 其编译/配置/部署见 [../llm-analysis-server/README.md](../llm-analysis-server/README.md)
> 本文档只覆盖**爬虫服务**的部署。

> 配套一键脚本：`deploy/setup.sh`
> 适用：Debian 12/13、Ubuntu 20.04+（已在 Debian 12 1GB 内存 VPS 上按本文档设计）

## 一、架构与部署单元

两个**独立进程**，各自 systemd 服务，互不影响的启停与重启：

| 服务 | 入口 | systemd | 是否需要浏览器 |
|---|---|---|---|
| 爬虫服务 | `main.py` | `reuters-crawler.service` | ✅ 需要（xvfb + Chrome） |
| 分析服务 | 已迁移为 Go 微服务 `../llm-analysis-server` | `llm-analyzer.service` | ❌ 只调 LLM API |

本脚本仅部署**爬虫服务**（含 xvfb + Chrome）；分析服务需到 Go 项目单独部署。

```
爬虫服务：RSS → 解码 → 抓正文 → 入库 t_articles → 写 pending
                                                      ↓
分析服务：轮询(30s) → 调 DashScope → 写 t_article_llm_analysis
```

## 二、服务器要求

| 项 | 最低 | 推荐 | 说明 |
|---|---|---|---|
| 系统 | Debian 12+ | Debian 12/13 | Debian 12 自带 Python 3.11，满足 nodriver 的 3.10+ |
| **内存** | 1 GB + 2GB swap | **2 GB+** | ⚠️ Chrome 有头模式很吃内存，1GB 必须配 swap |
| 磁盘 | 10 GB | 20 GB | Chrome 约 1GB + Python 依赖 + 日志 |
| 网络 | 可访问 Google | 海外机房 | 需访问 `news.google.com` 与 `reuters.com` |

**内存测算**（1 GB 机器）：

```
Debian 系统        ~250 MB
Chrome（有头）     ~300~500 MB   ← 大头
Xvfb              ~50~80 MB
Python + trafilatura ~150~250 MB
合计              ~750~1080 MB  ← 逼近上限，必须配 swap
```

## 三、一键部署（推荐）

```bash
# 1) 拉取代码
git clone -b feature/reuters-crawler-llm-analyzer \
  https://git.weixin.qq.com/sanshox/news-server.git
cd news-server/crawler

# 2) 执行部署（需 root）
sudo bash deploy/setup.sh

# 可选参数
sudo bash deploy/setup.sh --with-playwright   # 额外装 playwright 内核（兜底通道）
sudo bash deploy/setup.sh --no-chrome         # 已装 Chrome 时跳过
sudo bash deploy/setup.sh --no-systemd        # 不装 systemd（手动运行）
SWAP_SIZE=4G sudo bash deploy/setup.sh        # 自定义 swap
```

脚本会依次完成：系统依赖 → swap → Chrome → venv + Python 依赖 → `.env` → systemd 注册。

## 四、配置 `.env`

脚本会从 `config.example.env` 生成 `.env`，**必须手动填写**：

```bash
sudo -u www-data nano /opt/reuters-crawler/.env
```

| 变量 | 必填 | 说明 |
|---|---|---|
| `CLOUDBASE_ENV_ID` / `CLOUDBASE_SECRETID` / `CLOUDBASE_SECRETKEY` | ✅ | CloudBase 凭证（与 `server/.env.production` 一致） |
| `CLOUDBASE_RDB_INSTANCE` / `CLOUDBASE_RDB_DATABASE` | 可选 | MySQL 实例/库名（留空用 default / envId） |
| `CLOUDBASE_OPENID` | 可选 | 默认 `system`，用于含 NOT NULL `_openid` 列的表 |
| `TRIGGER_LLM` | 可选 | 默认 `true`：入库后写 `t_analysis_task` 由 Go 服务消费 |
| `DASHSCOPE_API_KEY` 等 LLM 配置 | — | **不在本服务配置**，请到 `llm-analysis-server/.env` |
| `RSS_URL` | 可选 | 默认 Reuters 财经关键词 |
| `INTERVAL_SECONDS` | 可选 | 抓取间隔，默认 300 |
| `MAX_PER_ROUND` | ⚠️ | 单轮篇数，**小内存建议 3~5** |
| `NODRIVER_REQUEST_INTERVAL` | ⚠️ | 每篇冷却秒数，默认 5，**建议 8** |
| `NODRIVER_MAX_SWITCH` | 可选 | 被拦后换身份重试次数，默认 3 |

> **代理说明**：若服务器本机已跑代理（如 `http://127.0.0.1:7928`），请在 `.env` 保留并填 `HTTPS_PROXY` / `HTTP_PROXY` 指向它——爬虫访问 Google/Reuters 走代理；**CloudBase 入库始终直连、不受代理影响**（`repository.py` 已 `proxies=None`）。仅在服务器直连海外无障碍时才删除这两行。

## 五、验证（按顺序）

```bash
cd /opt/reuters-crawler
sudo -u www-data ./venv/bin/activate 2>/dev/null || source venv/bin/activate

# 1) 抓取链路（不入库，看能否拿到正文）
sudo -u www-data xvfb-run -a ./venv/bin/python main.py --once --dry-run
#    预期：打印标题/URL/作者/正文长度；若"完整正文 0 条"说明被反爬拦（见第七节）

# 2) 真实入库
sudo -u www-data xvfb-run -a ./venv/bin/python main.py --once
#    预期：日志出现「入库成功 id=...」

# 3) 分析服务（Go 版，在 llm-analysis-server 目录）
cd ../llm-analysis-server && ./llm-analyzer --once
#    预期：日志出现「分析完成 task_id=... article_id=... result_len=...」
```

小内存机器建议带上限流参数验证：

```bash
MAX_PER_ROUND=3 NODRIVER_REQUEST_INTERVAL=8 xvfb-run -a python main.py --once --dry-run
```

另开终端观察内存：`watch -n 2 free -h`

## 六、运维

```bash
# 启动 / 停止 / 重启
sudo systemctl enable --now reuters-crawler     # 开机自启并立即启动
sudo systemctl restart llm-analyzer     # Go 分析服务（在 llm-analysis-server 部署）
sudo systemctl stop reuters-crawler

# 状态与日志
sudo systemctl status reuters-crawler
journalctl -u reuters-crawler -f                # 实时
journalctl -u reuters-crawler --since "1 hour ago"
tail -f /opt/reuters-crawler/logs/crawler.log   # 文件日志（10MB×5 轮转）

# 崩溃自动拉起验证
PID=$(systemctl show -p MainPID --value reuters-crawler)
sudo kill -9 $PID && sleep 12
systemctl show -p MainPID --value reuters-crawler   # 应为新的非 0 PID
```

## 七、故障排查

| 现象 | 原因 | 解决 |
|---|---|---|
| `nodriver 通道不可用（需 Python 3.10+）` | Python 版本过低 | Debian 12 自带 3.11 正常；若用其他系统请升级 |
| Chrome 启动失败/缺库 | 缺运行依赖 | `apt-get -f install -y`，或重装脚本第 1 步的库 |
| `--dry-run` 全部降级（正文 0 条） | 被 DataDome 拦截 | 见下方「反爬要点」 |
| `Can't connect to MySQL` | DB 配置或白名单 | 检查 `.env`；确认 MySQL 放行服务器 IP（参考 REQUIREMENTS 第十二章） |
| 进程被 OOM kill | 内存不足 | 增大 swap；调小 `MAX_PER_ROUND`；升级内存 |
| `Loop ... is closed` 告警 | 事件循环问题 | 已用 `uc.loop()` 修复，若仍出现请升级到最新代码 |
| 分析服务不消费任务 | Go 服务未启动 / `.env` 未配 key | 到 `llm-analysis-server` 检查；额度耗尽时会停止本轮（属预期保护） |

## 八、反爬要点（Reuters = CloudFront + DataDome）

已实测结论，**务必遵守**：

1. **必须「有头」模式**：`headless=True` 与传统 `--headless=new` 均被拦（1584 字节挑战页）。
   因此用 `xvfb-run`（脚本已内置到 systemd 的 `ExecStart`）。
2. **必须控制频率**：密集请求会让出口 IP 被 DataDome 拉黑。
   - `NODRIVER_REQUEST_INTERVAL`（默认 5，建议 8）
   - `MAX_PER_ROUND`（建议 3~5，别用默认的 20）
3. **IP 被拉黑的表现**：原本成功的有头模式突然持续返回 1584 字节挑战页。
   需换 IP 或等待较长时间冷却；新 IP 首次运行成功率最高。
4. **自动换身份**：被识别后会自动切换 UA/指纹重试（`NODRIVER_MAX_SWITCH` 次），日志可见
   「第 N 次被反爬拦截（身份=xxx）→ 切换身份重试」。

## 九、退路方案（若反爬始终无法突破）

| 方案 | 说明 |
|---|---|
| 只存元数据 | `FETCH_MODE=requests`，接受 RSS 标题/摘要降级（内存 < 200MB，极稳） |
| 换源 | 改 `RSS_URL` 为 BBC / AP News（实测无 DataDome，可抓全文） |
| 住宅代理 | DataDome 对住宅 IP 宽松，需付费代理 |
