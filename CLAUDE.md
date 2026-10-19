# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 固定任务

- 每次修改完成后，都应创建一个对应的 Git commit，便于后续追踪和回滚。

## 项目概况

`bidding` 项目用于招标文件解析、结构化抽取、检索索引与多 Agent 标书编写。技术栈如下：

- **后端**：Python 3.12 + FastAPI/uvicorn；SQLAlchemy 2.0 + asyncpg；Celery + Redis；ChromaDB 向量库；LangGraph；OpenAI SDK / zai-sdk；python-docx / PyMuPDF。
- **前端**：React 19 + TypeScript + Vite 8 + Tailwind CSS 4；Electron 44 桌面外壳。
- **包管理**：Python 使用 uv（`pyproject.toml` + `uv.lock`）；前端使用 npm（`frontend/package.json` + `package-lock.json`）。

## 目录结构

```text
src/bidding/                  Python 包（src 布局）
src/bidding/cli.py            命令行入口：doctor / parse / extract / ingest / inspect / write
src/bidding/config.py         .env 配置与路径约定
src/bidding/models.py         解析、抽取、索引共用数据结构
src/bidding/parsing/          PDF 解析：结构还原、PyMuPDF、MinerU、产物落盘
src/bidding/extraction/       结构化抽取：章节单元、LLM 容错、归一化/合并/grounding
src/bidding/indexing/         检索索引：分块、智谱 embedding、Chroma 读写与自检
src/bidding/writing/          多 Agent 标书编写：profile、招标上下文、检索、大纲、LangGraph、渲染与流水线
src/cli.py                    `python -m src.cli` 兼容入口（委托 bidding.cli）
bidder_profile.example.json   投标人资料模板（留空字段生成【待填写】占位符）
src/bidding/job_pipeline.py   解析 → 抽取 → 入库与写标的共享执行函数
src/bidding/job_state.py      Redis 任务状态、进度、TTL 与 doc_id 互斥锁
src/bidding/celery_app.py     Celery 应用、队列、超时与 broker 配置
src/bidding/tasks.py          Celery ping / parse_pipeline / write_bid 任务
api/                          FastAPI 应用（根目录应用层，导入 bidding 包）
api/app.py                    上传解析、异步写标、轮询接口与任务后端选择
api/celery_runner.py          默认的 Celery 投递器（按 kind 路由 parse/bid）
api/jobs.py                   内存任务表与线程池（TASK_BACKEND=memory 与测试）
api/__main__.py               `python -m api` 启动入口
tests/                        离线 pytest 测试
frontend/src/                 React 前端源码
frontend/electron/main.cjs    Electron 主进程
upload/                       运行时上传、解析、图片与标书产物（git 忽略）
redis.conf                    Redis 本地开发配置
.tools/                       本机下载的 Redis 可执行文件（git 忽略，不入库）
redis_data/                   Redis 持久化数据（git 忽略）
chroma_db/                    ChromaDB 持久化数据（git 忽略）
.env                          本地环境变量与密钥（git 忽略，禁止提交）
.env.example                  配置模板（密钥为空，可入库）
```

## 常用命令

### Python

```bash
uv sync                  # 按 uv.lock 同步依赖
uv add <package>         # 新增运行时依赖
uv add --dev <package>   # 新增开发依赖
uv run pytest            # 运行离线测试
uv run python -m bidding doctor                         # 检查配置与本地服务
uv run python -m bidding parse <pdf> --parser auto      # 解析 PDF
uv run python -m bidding.parsing <pdf>                  # 等价的快捷入口
uv run python -m bidding extract <doc_id>               # 结构化抽取
uv run python -m bidding ingest <doc_id|pdf>            # 分块、嵌入并写入 Chroma
uv run python -m bidding inspect <doc_id> --query "评标办法"  # 检索测试
uv run python -m bidding write <doc_id>                 # 多 Agent 生成投标文件
uv run python -m src.cli write <doc_id>                 # 等价兼容入口
```

解析产物在 `upload/parsed/<doc_id>/`：`content.md` 为带页码注释的权威全文，`manifest.json` 为摘要（不含全文），`raw/sections.json` 保存章节正文缓存；图片在 `upload/images/<doc_id>/`。`auto` 模式优先 MinerU，未配置 `MINERU_API_KEY` 时按设计跳过云端并使用 PyMuPDF 本地解析，降级原因会写入 manifest。

抽取产物为同目录的 `extraction.json`；逐单元结果缓存在 `extraction_parts/`（按内容哈希与提示词/模型版本失效），中断后可直接续跑。合同等通用章节按大小压缩，评分/资格/格式章节优先细分。`ungrounded_fields` 列出无法在原文中逐字定位的字段，`extraction_meta.partial` 表示使用了 `--limit` 或存在失败单元。

索引产物为同目录的 `index_manifest.json`。分块目标 450 字、硬上限 700 字、重叠 80 字；页码从嵌入文本中移除但保留在 metadata。嵌入使用智谱 `embedding-3`（2048 维），标书写入 `tender_document`，公告写入 `tender_announcement`；按 `doc_id` 先删后写，可安全重跑。`ingest` 先完成全部 embedding 再删除旧数据，避免中途失败丢失索引。

投标文件编写以 `extraction.json` 为前置条件，默认读取 `upload/bidder_profile.json`，产物写入 `upload/bids/<doc_id>/`：`bid.md`、`bid.docx`、`bid_review.md`、`bid_manifest.json`。固定 11 个章节，分析师最多新增 3 个章节；行政/商务/技术三个编写 Agent 顺序执行，审查 Agent 可对最多 `--max-revisions` 个章节触发自动修订（事实/编造类问题优先于评分覆盖类问题）。招标文件事实只来自解析/抽取产物，投标人事实只来自 profile：缺失投标人资料写【待填写：...】，缺失招标事实写【待核对：...】；系统用确定性代码把 profile 渲染成附表，避免 LLM 改写数据。`bid_review.md` 是内部自检报告，不得随投标文件提交。CLI 与 HTTP/Celery（`POST /api/bids` → `bidding.write_bid`）共用同一 `write_bid` 流水线；CLI 的 `--max-sections` 默认 3，API 默认 0。

### HTTP API

```bash
# 终端 1：Celery worker（Windows 必须 --pool=solo）
uv run celery -A bidding.celery_app worker --loglevel=info --pool=solo --queues=bidding

# 终端 2：读取 API_HOST / API_PORT，默认 127.0.0.1:8001
uv run python -m api
uv run uvicorn api.app:app --host 127.0.0.1 --port 8001

# 连通性检查，返回值应为 "pong"
uv run celery -A bidding.celery_app call bidding.ping
```

`POST /api/parse` 以 multipart `file` 字段上传 PDF，立即返回 202、`job_id` 与 `status_url`；`GET /api/jobs/{job_id}` 返回 `status`、`kind`、`stage`、`progress` 与分阶段 `result`。`parser`、`extract`、`ingest` 是查询参数，`extract` 与 `ingest` 相互独立。

`POST /api/bids` 接受 JSON `{"doc_id","profile","max_sections","max_revisions"}`：`profile` 是对象（≤2 MB，字段同 `bidder_profile.json`），无需文件上传。要求该 `doc_id` 已有解析与抽取产物，否则 404/409。API 把 profile 写到 `upload/tmp/<job_id>/bidder_profile.json` 并投递 `bidding.write_bid`；返回同样的 202/`job_id`/`status_url`，轮询同一任务接口，`kind=bid`、`stage=write`、`result.bid` 为摘要。`max_sections` 0–3（默认 0），`max_revisions` 0–10（默认 2）；写标与解析任务共用同一 `doc_id` 锁。

默认 `TASK_BACKEND=celery`：API 把上传/临时 profile 保存到 `upload/tmp/<job_id>/`，在 Redis 创建任务记录后按 `kind` 投递 Celery；worker 调用共享的 `job_pipeline.execute_pipeline` 或 `execute_bid_job`，状态、进度和阶段结果写回 Redis（`bidding:job:*`），Celery 结果后端只保存很小的任务返回值。API 重启不会丢失任务记录或中断 worker，记录在 `API_JOB_TTL_SECONDS` 后过期；API 与 worker 必须共享 `upload/` 文件系统。同一 `doc_id` 由 Redis 原子锁互斥（重复上传 409）；Redis/broker 不可用时提交与轮询返回 503。Windows 只能使用 `--pool=solo`；状态在 Redis，因此 `uvicorn --workers N` 可以正常轮询。

`TASK_BACKEND=memory` 回退到 `api/jobs.py` 的进程内任务表 + 线程池，仅用于离线测试或单进程调试：重启丢任务，不能使用 `uvicorn --workers N`，容量、保留时间与并发数由 `API_MAX_JOBS`、`API_JOB_TTL_SECONDS`、`API_JOB_WORKERS` 控制。默认端口 8001，Swagger 在 `/docs`。

### 前端

```bash
npm --prefix frontend run dev             # Vite 开发服务器，端口 5173
npm --prefix frontend run build           # tsc 类型检查 + 生产构建
npm --prefix frontend run lint            # oxlint
npm --prefix frontend run electron:dev    # 需先启动 dev server；加载 localhost:5173
npm --prefix frontend run electron:start  # 先构建，再加载 dist/index.html
```

Electron 主进程带 `--dev` 参数时加载 `http://localhost:5173`，否则加载 `frontend/dist/index.html`。因此 Vite 配置了 `base: './'`，不要移除。Vite 固定使用 5173 端口（`strictPort`）。

### 本地服务

在项目根目录执行：

```powershell
# Redis（127.0.0.1:6379，无密码；任务状态 + Celery broker）
.tools\redis-8.10.2\Redis-8.10.2-Windows-x64-msys2\redis-server.exe redis.conf

# Celery worker（Windows 必须 solo；保持前台或另开终端）
uv run celery -A bidding.celery_app worker --loglevel=info --pool=solo --queues=bidding

# 验证 worker 可达，result 中应出现 "pong"
uv run celery -A bidding.celery_app call bidding.ping

# ChromaDB（http://localhost:8000，仅入库/检索时需要）
uv run chroma run --path ./chroma_db
```

- FastAPI/uvicorn 默认端口也是 8000，启动后端时使用 `--port 8001`（或为 Chroma 指定其他端口），避免冲突。
- Redis 版本需支持 RESP3（当前为 8.10.2）；redis-py 8.x 默认发送 `HELLO 3`，Redis 5 等旧版本会连接失败。
- Redis 同时承载任务状态（`REDIS_URL`，db 0）和 Celery broker（`CELERY_BROKER_URL`，db 0）；结果后端在 db 1。键前缀 `bidding:` 避免冲突。
- Windows 系统代理会影响 Python 对 localhost 的 HTTP 请求。`.env` 已配置 `NO_PROXY=localhost,127.0.0.1`；运行 Chroma 客户端前需确保该变量已加载，否则请求可能返回 502。

## 环境要求与约定

- Python 3.12（见 `.python-version`）。
- Node.js ≥ 22.12（本机为 24.22.0；Vite 8 / Tailwind CSS 4 / Electron 44 的要求）。
- SQLAlchemy 固定为 2.0.x（`>=2.0,<2.1`）。
- Tailwind CSS v4 通过 `@tailwindcss/vite` 插件接入，入口是 `frontend/src/index.css` 中的 `@import "tailwindcss";`，无需 `tailwind.config.js`。
- `.env` 包含 LLM（DeepSeek/智谱）、PostgreSQL、Redis、Chroma、MinerU 等配置，已被 `.gitignore` 排除；不要提交或打印其中的密钥。
- API 任务后端由 `TASK_BACKEND` 选择：`celery`（默认，需 Redis + worker）或 `memory`（单进程调试）。Celery broker/结果后端与超时由 `CELERY_*` 配置；运行 worker 前先加载 `.env`（`uv run` 会自动读取 `config.py` 的 `load_dotenv`）。
- 聊天模型由 `LLM_PROVIDER` 选择 `deepseek` 或 `zhipu`。DeepSeek key 不可用时可切换为 `LLM_PROVIDER=zhipu` + `ZHIPU_CHAT_MODEL=glm-4-plus`，无需改代码；嵌入固定走智谱 `embedding-3`。
- 标书编写的投标人资料来自 CLI 的 `upload/bidder_profile.json`（模板 `bidder_profile.example.json`）或 `POST /api/bids` 请求体中的 `profile`。`upload/` 整体被 git 忽略；不要编造投标人事实，缺失字段必须保留【待填写】占位符；临时 profile 文件结束后由 worker 删除。
- `write` 有 CLI 与 HTTP/Celery 两个入口，共用同一流水线；运行前需完成解析与抽取，且 LLM key 可用。
- 修改功能后运行相关检查（`uv run pytest`、`npm --prefix frontend run build`），并按「固定任务」创建提交。
