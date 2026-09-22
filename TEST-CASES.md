# Reuters 爬虫测试用例

> 对应需求：[REQUIREMENTS.md](./REQUIREMENTS.md)

## 一、单元测试（离线，已通过）

运行方式：

```bash
cd crawler
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
python -m unittest discover -s tests -t . -v
```

**结果：Ran 138 tests — OK（全部通过）**（2026-09-22 更新：新增三级 fallback 解码 4 项）

> 其中 `tests/test_pipeline_local.py` 为**本地端到端链路**用例（见第二节 TC-R2.0），
> 用真实 headless 浏览器抓取本地 HTML，验证 RSS→解码→抓取→抽取 全流程可用（不依赖外网）。

| 编号 | 用例 | 验证点 |
|---|---|---|
| TC-R1.1 ~ 1.5 | `TestCleanTitle` | RSS 标题去掉 ` - Reuters` 后缀；保留标题内短横线；空串/空白安全返回 |
| TC-R1.6 ~ 1.9 | `TestBuildExcerpt` | `None` → `None`；短文本原样；长文本截断到 500；自定义 limit 生效 |
| TC-R1.10 ~ 1.14 | `TestConfig` | 默认值（300s / 20 / 1 / true）；env 覆盖；非法 int 回退默认；缺配置 `validate()` 抛错；代理解析 |
| TC-R1.15 ~ 1.28 | `TestDecodeGoogleNewsUrl` | 空 URL → None；库成功/失败（status + success 键兼容）；异常被吞；proxy 透传；**base64 本地解码命中真实 URL**；base64 对垃圾数据返回 None；**base64 命中时不调用库（零网络）**；库失败时 **HTTP 重定向 fallback** 取 Location |
| TC-R1.20 ~ 1.22 | `TestRepositoryOpenidPatch` | `_openid` NOT NULL → 补 `system`；可空 → 不补；列不存在 → 不补 |
| TC-R1.23 ~ 1.24 | `TestExtractArticle`（test_fetch） | 真实 HTML 抽出正文；空 HTML → `None` |
| TC-R1.25 ~ 1.31 | `TestFetchStrategy` | requests 成功/抛错/不启浏览器；**auto 无正文才启浏览器、有正文不启**；requests 失败转浏览器；playwright 恒定用浏览器；未注入浏览器不抛异常 |
| TC-R1.32 ~ 1.35 | `TestBrowserFetcher` | 不可用返回 None；成功返回 HTML 且 goto/close 被调用；导航失败返回 None；空 URL 返回 None |
| TC-R1.36 | `TestExcerpt` | 摘要按 limit 截断 |

### 1.1 LLM 分析服务用例（已迁移到 Go 项目 `llm-analysis-server`）

> 原 Python 版 20 项用例（`tests/test_analyzer.py`）随服务迁移已移除。
> Go 版（`../llm-analysis-server`）能力完全对齐，验证方式见其 README。
> 能力对照（迁移后）：
>
> | 能力 | 对应原用例 |
> |---|---|
> | 额度错误识别 `IsQuotaError` | TC-A1 ~ A4 |
> | LLM 客户端（占位符替换、空 choices、无 key 报错） | TC-A5 ~ A9 |
> | 任务处理（抢占失败跳过、正文缺失置 failed、重试退避、额度停止） | TC-A10 ~ A16 |
> | 单轮编排（无任务/无 key 跳过、额度受限立即停止本轮） | TC-A17 ~ A20 |

### 1.2 失败自动换 IP（2026-09-20 新增）

`tests/test_node_rotator.py`（节点轮换器，mock 面板、不发起真实请求）：
- 未配置面板 → `enabled=False`、`switch()` 返回 None（不影响主流程）
- 节点优选：住宅 + `quality=normal` 优先 > 住宅被标代理 > 移动 > 未知；排除已用节点；
  全部轮换一轮后重置
- 节点列表缓存生效（不重复请求面板）；面板请求失败返回空列表而不崩溃
- `connect` 正常 / 非 200 / 异常 三种结果处理
- **切换前预检 `test_node`**：`available` 通过；`unavailable` / `ok=false` / 非 200 /
  异常 均判失败；`probe_status` 缺失时以 `ok` 为准（兼容不同面板版本）
- **预检失败 → 换下一个候选**并最终成功；全部候选都失败 → 放弃换 IP
- 候选数达 `MAX_NODE_CANDIDATES` 上限后停止尝试；关闭预检（`precheck=False`）时
  不做 `test_node`、直接 `connect`
- **代理健康检查**：切换前探不到出口 IP（代理已断）→ 告警且仍尝试换节点恢复
- **`switch` 成功**（出口 IP 变化）/ **失败**（IP 未变、探测不到 IP、`connect`
  失败、无节点）
- 切换成功后标记该节点已用；节点优选 `probe_status=available`、排除 `unavailable`
- **防自锁**：访问面板的请求 `proxies=None`（不走爬虫代理）；仅配置
  `NODE_PANEL_PROXY` 时才走代理
- 探测出口 IP **必须**走爬虫代理（出口由代理提供）

`tests/test_nodriver.py::TestIpRotationOnDataDome`（换 IP 触发逻辑）：
- 命中 DataDome → 换 IP → 新 IP 上抓取成功
- 未配置面板（`rotator=None`）→ 即便命中 DataDome 也只换身份 3 次（保持原兜底）
- 非 DataDome 的一般拦截 → 只换身份，**不换 IP**
- 换 IP 次数达到 `MAX_IP_SWITCH` 上限后停止再换
- 换 IP 失败（返回 None）→ 停止重试
- 面板未启用（`enabled=False`）→ 不换 IP，退回换身份

### 1.3 dry-run 落本地 JSON（2026-09-21 新增）

`tests/test_dry_run_output.py`（离线，无需外网/数据库）：
- `_slugify`：保留字母/数字 → `_`；首尾标点剥离；空串回退 `article`；超长截断至 50
- `_save_dry_run_article`：写出 `<dir>/<序号>_<slug>.json`，含**完整正文**，
  `content_length` / `author` / `extraction_strategy` / `publish_time` / `index` / `saved_at` 均正确；
  文件名形如 `01_Test_Title.json`
- `_log_dry_run(..., out_dir=<dir>)`：控制台打印摘要并**额外打印落盘路径**，且文件已生成
- `_log_dry_run(..., out_dir=None)`：仅打印、**不落盘**（验证非 dry-run 路径行为不变）

### 1.4 正文图片抽取（2026-09-21 新增）

`tests/test_dry_run_output.py` + `src/article_parser.extract_images`（离线，无需外网）：
- 正文容器（`<article>`）内 `<img src>` → 转绝对 URL（`urljoin`）
- 懒加载 `data-src` / `data-lazy-src` 可识别
- `srcset` 取首个 URL
- **过滤** logo/avatar/icon/advert/banner/placeholder/spinner/pixel/1x1 等非正文图
- **容器外**（如 `<div class="ad">` 内的图）不抽取
- 空 html → `[]`；同一 URL 去重
- `_save_dry_run_article(..., images=[...])`：落盘 JSON 含 `images` 数组
- `_log_dry_run` 带 `images` 时控制台打印「图片 N 张」及落盘路径

### 1.5 正文图片单一数据源：content + position（2026-09-21 新增，取代旧锚点方案）

`src/article_parser.build_content_with_images` + `reconstruct_with_images` + `tests/test_dry_run_output.py`（离线）：
- 一次遍历正文块级元素：文本块拼入 `content`（段间空行，段落不丢失），图片块记录 `position`（在 content 中的字符偏移）
- 返回 `(content, images)`，`images=[{"url":..., "position":int}, ...]`，与 `content` 同源
- `position` 精确：`[图1]` 在第一段之后（`position == 第一段长度`）；过滤 logo/广告/头像类（`logo.png` 不计）
- 空 html → `("", [])`
- `reconstruct_with_images(content, images)` 按 `position` 从右往左插入，还原图文混排（验证 `[IMG:url]` 落点正确）
- `_save_dry_run_article(..., images=[{url,position},...])`：落盘 JSON 含 `images`，**不含** `content_anchored`
- `_log_dry_run` 带 images 时控制台打印「图片 N 张」及每张 `pos=`（如 `https://x.com/a.jpg (pos=3)`）

### 1.6 解码成功键兼容：success / status（2026-09-22 新增，对应需求 11.9）

`tests/test_units.py::TestDecodeGoogleNewsUrl`（离线，mock 库、不发起真实请求）：

- **库返回 `{"success": True, "decoded_url": ...}` → 能取到真实 URL**（核心回归用例：
  修复前因只认 `status` 键被误判为失败）
- 库返回 `{"success": False, "message": "..."}` → 返回 None（真失败）
- 旧版 `{"status": True, "decoded_url": ...}`（无 `success` 键）→ 仍能取到 URL（兼容）
- 旧版 `{"status": False, "message": ...}` → None（原有用例保持）
- `success=False` 时**不回退** `status`：即便同时带 `status=True` 也判失败
- 失败且 `message` 缺失 → 日志显示 `（库未返回错误信息）`，**不再打出 `None`**
- proxy 透传、库抛异常被吞掉、空 URL → None（原有用例保持）

### 1.7 三级 fallback 解码器（2026-09-22 新增，对应需求 11.10）

`tests/test_units.py::TestDecodeGoogleNewsUrl`（离线，不发起真实网络请求）：

- **base64 本地解码含明文 URL → 直接返回，无需网络**（零请求、不怕封）
- base64 内容不含 URL（如垃圾数据 `xyz`）→ 返回 None（不报错，fall through）
- **base64 命中时库不被调用**（验证优先级：base64 > 库）
- 库解码失败 → **HTTP 重定向 fallback** 取 `Location` 头（302 → 真实 URL）
- 三级全部失败 → 日志打 `三级解码均失败`，返回 None

## 二、集成测试（需海外服务器 + 数据库凭据，待验证）

> 本地（国内网络）`news.google.com` 不可达（`ConnectTimeout`），故以下用例需在海外服务器执行。
> 建议先执行 `python main.py --once` 完成一次真实抓取来验证 TC-R2.1 ~ TC-R2.5。

| 编号 | 用例 | 步骤 | 期望 |
|---|---|---|---|
| TC-R2.1 | RSS 拉取 | `python main.py --once` | 日志出现「RSS 拉取完成，条目数=N」且 N > 0 |
| TC-R2.2 | 链接解码 | 同上 | 日志出现「待入库=M」，M 条 URL 均为 `reuters.com` 真实地址 |
| TC-R2.3 | 正文抽取 | 同上 | 入库记录 `content` 非空、`content_length > 0`、`extraction_strategy='trafilatura'` |
| TC-R2.4 | 入库 + 去重 | 连续跑两次 `--once` | 第二次「跳过已存在」= 首次新增数，且不产生重复 `url` |
| TC-R2.5 | LLM 触发 | 查库 | `SELECT * FROM t_analysis_task WHERE article_id = <新文章id>` 存在 `pending` 记录 |
| TC-R2.6 | 作者关联 | 有作者的文章 | `t_authors` 新增作者，`t_article_authors` 存在关联行 |
| TC-R2.7 | 爬虫日志 | 查库 | `t_crawler_logs` 有 `platform='reuters'` 的记录，含 added/skipped/failed |
| TC-R2.8 | 定时循环 | `python main.py` 常驻 | 每 300 秒执行一轮（日志时间戳间隔 ≈ 5 分钟） |

## 三、部署验证（systemd）

| 编号 | 用例 | 步骤 | 期望 |
|---|---|---|---|
| TC-R3.1 | 服务启动 | `systemctl enable --now reuters-crawler` | `systemctl status` 为 `active (running)` |
| TC-R3.2 | **崩溃自动拉起** | `kill -9 <MainPID>`，等 12 秒 | MainPID 变为新的非 0 值（10 秒内拉起） |
| TC-R3.3 | 日志轮转 | 持续运行至日志 > 10MB | 生成 `crawler.log.1` 等，最多 5 个备份，不无限增长 |
| TC-R3.4 | 优雅退出 | `systemctl stop reuters-crawler` | 日志出现「收到信号 15，准备退出」，进程正常结束 |
| TC-R3.5 | 数据库断开恢复 | 临时断开 DB 后恢复 | 本轮失败记 error 日志，DB 恢复后下一轮自动成功（依赖 `ping(reconnect=True)`） |

## 四、已知环境限制

- 本地（国内网络）无法访问 `news.google.com`，集成测试必须在**海外服务器**执行（与部署决策一致）。
- LLM 免费额度已耗尽，`TRIGGER_LLM=true` 时分析任务会失败重试，属预期，**不影响爬虫入库**。
