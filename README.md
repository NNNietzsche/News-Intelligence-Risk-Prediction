# InfoRisk｜信息系统与风险预测

面向企业、市场和国际评级监测的本地风险情报工作台。系统采集可追溯的公开信息，整理为中文新闻、主体事件、市场信号与评级面板；AI 仅基于已抓取正文做摘要、翻译和风险分析，并保留原始来源链接供核验。

> 这是公开信息辅助监测工具，不构成授信审批、投资建议、信用评级或法律意见。

## 功能一览

| 页面 | 地址 | 主要用途 |
| --- | --- | --- |
| 新闻汇总 | `/daily-news` | 中东、日本方面、每日宏观与市场情报；可按日查看近 7 日历史资讯 |
| 主体评估 | `/entity-assessment` | 企业近三个月公开信息事件、最新消息、来源目录与财务背景 |
| 行业分析 | `/deep-reports` | 行业报告草稿、证据材料、版本和在线预览 |
| 国际评级 | `/intl-ratings` | 三大评级、市场信号与评级变动监测 |

### 新闻汇总

- 三个固定栏目：**中东方面**、**日本方面内容**、**每日宏观与市场情报**。
- 每条新闻展示中文标题、抓取正文整理出的内容详情、风险提示及原始来源链接。
- 统一采用近 24 小时新闻卡片排版，并可通过“今日、昨天、前天”等日期选项查看近 7 日已保存历史；全部按发布时间倒序排列。
- 标题左侧以色条标识风险等级：低（灰）、中（蓝）、高（橙）、极高（红）。
- 支持按七类风险标签和四档风险等级组合筛选；同一新闻可带主类型与一个辅类型。
- 支持页面手动刷新和定时采集；支持导出 PDF。

### 主体评估

- 针对配置的主体及其母公司、监管、交易所、官方新闻和关联方来源进行采集。
- “最新消息”提供近三个月公开信息概览与 AI 总结；“公开信息事件”按类别展示完整详情、风险提示和可点击来源。
- 事件分类：司法/行政监管、金融与经营数据、公开舆情、供应链/关联方监控。
- Godiva 等非上市品牌可展示母公司合并披露作为集团经营与偿债背景；该信息不会被表述为品牌独立财报。

### 国际评级与市场信号

- 展示可核验的 **S&P Global、Moody's、Fitch Ratings** 评级或无评级状态。
- 追踪评级、展望和变动；定时任务会刷新数据并记录变动。
- 同页提供市场信号，辅助查看利率、汇率、商品等行情变化。

## 风险分类与等级

新闻汇总使用银行视角的固定风险分类。每条有风险关联的新闻必须有 1 个主类型，最多有 1 个辅类型。

| 风险类型 | 典型情形 |
| --- | --- |
| 信贷风险 | 违约、重组、评级/展望变化、减值、现金流或偿债压力 |
| 市场风险 | 利率、汇率、股债、大宗商品价格对估值或头寸的影响 |
| 流动性与资产负债 | 融资冻结、存款流失、利差、期限错配、发债受阻 |
| 合规与反洗钱 | 制裁、罚单、许可、AML/KYC、出口管制 |
| 国别与地缘 | 战争、航道/港口/管道中断、资本管制、国有化 |
| 操作与网络安全 | 核心系统故障、网络攻击、支付清算或业务中断 |
| 治理与信息披露 | 会计更正、披露遗漏、造假嫌疑、高管异常、非标审计 |

| 等级 | 含义 |
| --- | --- |
| 低 | 无即时传导的例行、中性或正面信息 |
| 中 | 需要跟踪，但暂不构成授信、交易、融资或合规动作 |
| 高 | 负面事实明确、范围较大，但尚未达到当日必须执行指令的程度 |
| 极高 | 已发生违约、重组、融资冻结、有效制裁、关键航道关闭或支付清算中断等事件 |

保护规则：仅有标题或未取得正文的资讯最高为“中”；未经确认的传闻最高为“高”；正面或中性且无可执行影响的资讯最高为“低”。

## AI 在系统中的作用

AI 不替代信源，也不自行补充事实。它在取得的标题、摘要和新闻正文范围内完成以下工作：

1. 将日文、英文新闻翻译为简体中文。
2. 将抓取到的新闻正文整理为详细内容摘要。
3. 根据正文中的触发事实，分析可能影响的经营、合规、市场或融资环节。
4. 为新闻和主体事件匹配风险类型、风险等级和风险提示。
5. 在主源不足时，配合秘塔搜索补充候选信息；候选仍需保留可核验链接。

如果没有配置大模型密钥，页面和基础采集可以启动，但结构化摘要、翻译和 AI 风险分析会降级或不可用。

## 快速启动（macOS）

### 1. 安装依赖并初始化

```bash
cd /Users/dingjiaye/Desktop/risk-intel-system-main

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

cp .env.example .env
python scripts/init_db.py
```

### 2. 配置密钥

打开 `.env`，按需要填入：

```dotenv
MITA_API_KEY=你的秘塔搜索_API_Key
DEEPSEEK_API_KEY=你的DeepSeek_API_Key
GEMINI_API_KEY=你的Gemini_API_Key
```

| 配置项 | 用途 | 是否必需 |
| --- | --- | --- |
| `MITA_API_KEY` | 秘塔 AI 搜索，用于可靠来源检索与补缺 | 建议配置 |
| `DEEPSEEK_API_KEY` | 新闻结构化、翻译、国际评级主体对齐 | 建议配置 |
| `GEMINI_API_KEY` | 主体评估和行业分析 | 按需配置 |
| `OPENFIGI_API_KEY` | 国际评级发行体匹配 | 可选 |
| `TRADINGVIEW_USERNAME` / `TRADINGVIEW_PASSWORD` | 市场行情补充 | 可选 |

不要提交 `.env`、API Key 或本地数据库到 GitHub。

### 3. 启动服务

```bash
source .venv/bin/activate
python scripts/run_dev.py
```

浏览器访问：<http://127.0.0.1:8000>

`0.0.0.0` 是服务监听地址，不能作为浏览器访问地址；请使用 `127.0.0.1` 或 `localhost`。

如果 8000 端口已被占用，可改用 8001：

```bash
source .venv/bin/activate
python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

访问：<http://127.0.0.1:8001>

### 常见启动问题

| 问题 | 处理方式 |
| --- | --- |
| `address already in use` | 执行 `lsof -nP -iTCP:8000 -sTCP:LISTEN` 查找端口占用，或改用 8001 |
| 浏览器访问 `0.0.0.0` 超时 | 改为 `http://127.0.0.1:8000` |
| `Internal Server Error` | 查看运行终端的完整错误；确认已执行 `python scripts/init_db.py`，并重启服务 |
| AI 分析不可用 | 检查 `.env` 中对应密钥是否已填入、不是示例占位符，然后重启服务 |
| 新闻为空 | 在侧边栏手动运行采集；确认网络和 `MITA_API_KEY`，并检查对应数据源配置 |

## 采集与刷新

- 在新闻页面侧边栏点击“刷新”或运行对应流水线，可立即采集资讯。
- 服务进程保持运行时，系统会根据 `.env` 中的 `DAILY_PIPELINE_CRON` 执行每日采集。
- 国际评级与市场信号的自动刷新时间由 `INTL_RATINGS_REFRESH_CRON` 控制。
- 默认时区为东京；修改 cron 后需要重启服务。

常用配置：

```dotenv
# 每天 06:00 采集新闻
DAILY_PIPELINE_CRON=0 6 * * *

# 每天 06:30 更新国际评级与市场信号
INTL_RATINGS_REFRESH_CRON=30 6 * * *

# 新闻采集窗口：近 24 小时
NEWS_WINDOW_HOURS=24

# 是否抓取新闻正文；开启后摘要和风险判断更准确，但速度较慢
NEWS_FETCH_BODY=true

# 页面无已存新闻时不自动重新采集；需要更新时点击“刷新”
NEWS_AUTO_BACKFILL_ON_EMPTY=false
```

## 信息源与配置文件

系统优先使用官方、监管、交易所、公司 IR 与可信媒体来源。数据源目录和检索词均在配置文件中维护：

| 文件 | 内容 |
| --- | --- |
| [`config/rss_feeds.yaml`](config/rss_feeds.yaml) | 新闻 RSS、检索词与日报模块配置 |
| [`config/direct_sites.yaml`](config/direct_sites.yaml) | 直连官方网站与公告来源 |
| [`config/entity_targets.yaml`](config/entity_targets.yaml) | 主体评估对象、别名、官方来源和背景来源 |
| [`config/intl_ratings.yaml`](config/intl_ratings.yaml) | 国际评级发行体与市场信号配置 |
| [`DATA_SOURCES.md`](DATA_SOURCES.md) | 页面功能与数据源接入说明 |

修改 YAML 后重启应用，或通过流水线配置重载接口使其生效。

## 项目结构

```text
app/                 FastAPI 页面、接口、采集、分析和展示服务
app/templates/       页面模板
app/static/          CSS 和 JavaScript
config/              新闻、主体、直连站点和评级配置
data/                SQLite 数据库、报告文件与本地运行数据
scripts/             初始化和开发启动脚本
intl_ratings/        国际评级与市场信号流水线
```

## GitHub 更新

完成本地修改后：

```bash
git status
git add .
git commit -m "feat: 描述本次更新"
git push origin main
```

如果远端已有新提交：

```bash
git pull --rebase origin main
git push origin main
```

## 使用边界

- 资讯时效、正文可得性和来源可访问性会影响结果完整度。
- 风险提示是对公开信息潜在传导路径的辅助分析，不能替代人工核验。
- 对主体的关联度、事实范围、评级和财务数据，应通过页面原始来源链接复核。
- 非上市品牌、集团子公司和关联方的集团口径数据，不等同于其独立财务数据。



###后端说明书

| 模块 | 主要文件 | 实现功能 |
|---|---|---|
| 启动与页面 | `app/main.py` | 创建 FastAPI 服务、页面路由、读取数据库缓存、渲染新闻汇总/主题评估/国际评级/行业报告页面。 |
| API 接口 | `app/api/routes.py` | 提供采集、刷新、AI 搜索信源、主体事件、数据源、PDF 导出、报告生成等 `/api/v1/...` 接口。 |
| 请求校验 | `app/schemas.py` | 定义接口接收的数据格式；例如采集参数、时间窗口、模块代码、数据源勾选状态。 |
| 系统配置 | `app/config.py` | 读取 `.env` / Render 环境变量，包括 DeepSeek、秘塔、数据库、定时任务、采集窗口等。 |
| 数据库模型 | `app/database/models.py` | 定义 SQLite 数据表：新闻、主体风险、监控主体、报告、数据源、AI 缓存、评级变动等。 |
| 数据库连接与迁移 | `app/database/session.py` | 数据库初始化、连接、字段迁移。 |
| 新闻采集主流程 | `app/services/pipeline.py` | 抓取新闻、去重、正文提取、DeepSeek 分析、风险分类、入库。 |
| 异步任务与定时采集 | `app/services/pipeline_runner.py`、`app/services/scheduler.py` | 后台运行采集任务、记录进度、处理刷新按钮与定时更新。 |
| RSS 与官方来源抓取 | `app/services/rss_news.py`、`app/services/scrapers/` | RSS、官网、交易所、监管网站、TDnet、新浪 7×24 等来源抓取。 |
| 秘塔搜索 | `app/services/mita_search.py` | 使用 `MITA_API_KEY` 搜索可参考的新闻与行业信源。 |
| DeepSeek 分析 | `app/services/deepseek_analyzer.py` | 新闻摘要、中文翻译、风险类型、风险等级、风险提示、主题评估汇总报告。 |
| 主题评估 | `app/services/entity_briefing.py` | 生成“最新消息”、近三个月汇总报告、事件分类、风险传导提示及数据库缓存。 |
| 主体信源与相关性 | `app/services/entity_catalog.py`、`app/services/entity_relevance.py`、`app/services/entity_briefing_feed.py` | 管理 Godiva、GLP 等主体，匹配官网/监管/交易所/行业背景信源，过滤无关新闻。 |
| 财务信息 | `app/services/entity_kabutan.py`、`app/services/entity_financial_pdf.py` | 获取 Kabutan、PDF 财报等来源，整理主体财务展示。 |
| 风险判断 | `app/services/news_risk_tags.py`、`app/services/risk_reasoning.py` | 根据新闻正文划分信贷、市场、流动性、合规、国别地缘、操作安全、治理披露等风险标签，并生成风险提示。 |
| 国际评级 | `app/services/intl_ratings_service.py` | 三大评级、市场信号、评级变化与历史记录。 |
| 行业授信报告 | `app/services/industry_analysis.py`、`app/services/grounded_report.py` | 行业选择、资料上传、AI 搜索信源、勾选材料、DeepSeek 生成行业/授信分析报告。 |
| 信源与文件处理 | `app/services/data_source_service.py`、`app/services/content_extractor.py` | 上传 PDF、Word、Excel、网址；提取正文并保存为报告资料。 |
| 缓存 | `app/services/llm_cache.py` | 保存 DeepSeek 结构化分析、翻译和近三个月汇总报告，避免重复调用 AI。 |
| 导出 | `app/exporters/pdf_report.py`、`app/exporters/docx_report.py` | 导出 PDF 或 Word 报告。 |
| 启动脚本 | `scripts/run_dev.py`、`scripts/init_db.py` | 本地启动服务、初始化数据库。 |
