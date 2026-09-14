# Reuters 爬虫测试用例

> 对应需求：[REQUIREMENTS.md](./REQUIREMENTS.md)

## 一、单元测试（离线，已通过）

运行方式：

```bash
cd crawler
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
python -m unittest discover -s tests -t . -v
```

**结果：Ran 59 tests — OK（全部通过）**

> 其中 `tests/test_pipeline_local.py` 为**本地端到端链路**用例（见第二节 TC-R2.0），
> 用真实 headless 浏览器抓取本地 HTML，验证 RSS→解码→抓取→抽取 全流程可用（不依赖外网）。

| 编号 | 用例 | 验证点 |
|---|---|---|
| TC-R1.1 ~ 1.5 | `TestCleanTitle` | RSS 标题去掉 ` - Reuters` 后缀；保留标题内短横线；空串/空白安全返回 |
| TC-R1.6 ~ 1.9 | `TestBuildExcerpt` | `None` → `None`；短文本原样；长文本截断到 500；自定义 limit 生效 |
| TC-R1.10 ~ 1.14 | `TestConfig` | 默认值（300s / 20 / 1 / true）；env 覆盖；非法 int 回退默认；缺配置 `validate()` 抛错；代理解析 |
| TC-R1.15 ~ 1.19 | `TestDecodeGoogleNewsUrl` | 空 URL → None；成功返回真实 URL；`status=false` → None；解码库抛异常被吞掉不中断；proxy 正确透传 |
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
