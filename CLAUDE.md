# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 固定任务

- 每次修改完成后，都应创建一个对应的 Git commit，便于后续追踪和回滚。

## 项目概况

`bidding` 项目用于招标文件解析、结构化抽取、检索索引与多 Agent 标书编写。技术栈如下：

- **后端**：Python 3.12 + FastAPI/uvicorn；SQLAlchemy 2.0 + asyncpg；Celery + Redis；ChromaDB 向量库；MinIO 对象存储（可选）；LangGraph；OpenAI SDK / zai-sdk；python-docx / PyMuPDF。
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
src/bidding/search.py         面向问题的混合检索：词法打分、结构化条目、表页定位与向量融合
src/bidding/companies.py      PostgreSQL 公司资料库：懒建表、JSON profile 与覆盖合并
src/bidding/writing/          多 Agent 标书编写：profile、招标上下文、检索、大纲、LangGraph、渲染与流水线
src/bidding/storage.py        MinIO 对象存储：产物镜像、按需恢复、远端清单与连通性检查
src/cli.py                    `python -m src.cli` 兼容入口（委托 bidding.cli）
bidder_profile.example.json   投标人资料模板（留空字段生成【待填写】占位符）
src/bidding/job_pipeline.py   解析 → 抽取 → 入库与写标的共享执行函数
src/bidding/job_state.py      Redis 任务状态、进度、TTL 与 doc_id 互斥锁
src/bidding/celery_app.py     Celery 应用、队列、超时与 broker 配置
src/bidding/tasks.py          Celery ping / parse_pipeline / write_bid 任务
api/                          FastAPI 应用（根目录应用层，导入 bidding 包）
api/app.py                    上传解析、异步写标、招标检索、公司资料、轮询、文档库/历史/文件接口与任务后端选择
api/library.py                只读扫描 upload/parsed 与 upload/bids，白名单文件预览
api/celery_runner.py          默认的 Celery 投递器（按 kind 路由 parse/bid）
api/jobs.py                   内存任务表与线程池（TASK_BACKEND=memory 与测试）
api/__main__.py               `python -m api` 启动入口
tests/                        离线 pytest 测试
frontend/src/                 React 前端源码
frontend/src/api.ts           类型化 API 客户端、API_BASE 与 artifact 文本读取
frontend/src/hooks.ts         任务轮询、健康检查与列表数据加载 hooks
frontend/src/views/           上传解析、招标检索、投标编写、公司资料、文档库、历史产物六个工作区
frontend/src/components/      UI 基础组件、报告、Markdown 预览与模态框
frontend/electron/main.cjs    Electron 主进程；`bidding:open-bid-folder` IPC 仅允许 upload/bids
frontend/electron/preload.cjs contextBridge 暴露受限桌面能力
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
uv run python -m bidding storage status                 # MinIO 配置与连通性
uv run python -m bidding storage sync [doc_id]          # 本地产物补传到 MinIO
uv run python -m bidding storage restore <doc_id>       # 从 MinIO 恢复到 upload/
```

解析产物在 `upload/parsed/<doc_id>/`：`content.md` 为带页码注释的权威全文，`manifest.json` 为摘要（不含全文），`raw/sections.json` 保存章节正文缓存；图片在 `upload/images/<doc_id>/`。`auto` 模式优先 MinerU，未配置 `MINERU_API_KEY` 时按设计跳过云端并使用 PyMuPDF 本地解析，降级原因会写入 manifest。

抽取产物为同目录的 `extraction.json`；逐单元结果缓存在 `extraction_parts/`（按内容哈希与提示词/模型版本失效），中断后可直接续跑。合同等通用章节按大小压缩，评分/资格/格式章节优先细分。`ungrounded_fields` 列出无法在原文中逐字定位的字段，`extraction_meta.partial` 表示使用了 `--limit` 或存在失败单元。

索引产物为同目录的 `index_manifest.json`。分块目标 450 字、硬上限 700 字、重叠 80 字；页码从嵌入文本中移除但保留在 metadata。嵌入使用智谱 `embedding-3`（2048 维），标书写入 `tender_document`，公告写入 `tender_announcement`；按 `doc_id` 先删后写，可安全重跑。`ingest` 先完成全部 embedding 再删除旧数据，避免中途失败丢失索引。

招标检索（`bidding.search`，`GET /api/search`）无需入库即可使用词法层：对章节正文、结构化评分/资格/评审/日期条目、表标题与页码建索引后加权排序，并用查询词覆盖率校准分数。表格正文按解析章节的 `page_start/page_end` 定位；表号出现在目录时优先引用正文页。`mode=auto` 在嵌入可用时再融合 Chroma 向量结果，不可用则返回 `warnings` 降级为 `lexical`；`mode=lexical` 不调用嵌入。结果统一带 `page_hint`、`section_path` 与表号，按标题去除表格/章节重复项。公司资料库（`bidding.companies`）在首次访问时懒建 `companies` 表，整份 profile 存在 JSON 列；`POST /api/bids` 的 `company_id` 会先载入已存资料，再用请求体 `profile` 深合并覆盖。

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

`POST /api/bids` 接受 JSON `{"doc_id","profile","company_id","max_sections","max_revisions"}`：`profile` 是对象（≤2 MB，字段同 `bidder_profile.json`），无需文件上传；可选的 `company_id`（32 位十六进制）先从 PostgreSQL 载入已存公司资料，再用 `profile` 深合并覆盖。要求该 `doc_id` 已有解析与抽取产物，否则 404/409。API 把 profile 写到 `upload/tmp/<job_id>/bidder_profile.json` 并投递 `bidding.write_bid`；返回同样的 202/`job_id`/`status_url`，轮询同一任务接口，`kind=bid`、`stage=write`、`result.bid` 为摘要。`max_sections` 0–3（默认 0），`max_revisions` 0–10（默认 2）；写标与解析任务共用同一 `doc_id` 锁。

`GET /api/search` 是同步只读检索：查询参数 `doc_id`（必填，已解析文档）、`q`（必填，≤200 字）、`limit`（1–30，默认 10）与 `mode`（`auto`/`lexical`/`hybrid`）。返回 `query`、`core_terms`、`mode`（实际为 `hybrid` 或 `lexical`）、`vector_used`、`warnings`、`elapsed_ms`、按类型统计、相关表格与 `results`；每条结果含 `kind`、`title`、`snippet`、`score`、`page_start/page_end/page_hint`、`section_path`、`source` 与 `matched_terms`。文档不存在返回 404，参数不合法返回 422；本地缺少解析产物时会先尝试从 MinIO 恢复。

公司资料接口：`GET /api/companies` 列出记录，`POST /api/companies`（201）新建，`GET/PUT/DELETE /api/companies/{company_id}` 读取、更新、删除（DELETE 返回 204）。请求体均为 `{"profile": {...}}`（≤2 MB），记录返回 `id`、`name`、`profile`、`summary` 与时间戳；`company_id` 必须是 32 位十六进制且不存在时 404，未配置或无法连接 PostgreSQL 时统一返回 503。首次访问自动建表。

只读工作台接口：`GET /api/health` 返回服务、任务后端与存储模式（`minio`/`local`）；`GET /api/documents` 扫描 `upload/parsed/` 并合并 MinIO 远端文档（含关联标书摘要）；`GET /api/bids` 扫描 `upload/bids/`；`GET /api/bids/{doc_id}` 返回评分项覆盖、章节映射、审查问题与待填写清单；`GET /api/bids/{doc_id}/files/{filename}?download=` 预览/下载四个白名单产物。`api/library.py` 负责路径校验，`doc_id` 必须是安全目录名，文件名仅限 `bid.md`、`bid.docx`、`bid_review.md`、`bid_manifest.json`。CORS 仅允许 localhost/127.0.0.1 任意端口与 Electron `null` 源。覆盖报告是评分项到章节的映射，不代表实际得分。

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

Electron 主进程带 `--dev` 参数时加载 `http://localhost:5173`，否则加载 `frontend/dist/index.html`。因此 Vite 配置了 `base: './'`，不要移除。Vite 固定使用 5173 端口（`strictPort`），开发时 `/api` 代理到 `http://127.0.0.1:8001`；`file://` 构建默认直连 8001，可用 `VITE_API_BASE_URL` 覆盖。`api.ts` 负责这一选择。

工作台为左侧步骤导航 + 右侧工作区，六个页面始终挂载并用 `hidden` 切换，以便保留上传/编写/检索状态：上传解析（拖拽 PDF、解析器与抽取/入库选项、轮询阶段与摘要）、招标检索（选文档、智能混合/关键词模式、示例问题、带页码与表号的结果）、投标编写（表单 + 高级 JSON 双向同步、参数、评分覆盖与审查报告）、公司资料（PostgreSQL 列表/编辑/删除与载入写标）、文档库（`upload/parsed/` 列表、检索、直接发起编写）、历史产物（`upload/bids/` 列表、Markdown 预览与下载）。文档库“检索”按钮通过 `searchFocus` token 跳到检索页并选中该文档。预览使用 `react-markdown` + `remark-gfm`，不启用原始 HTML，并隐藏内部页码/分页注释。顶栏提供深色/浅色主题切换：`theme.ts` 维护 `data-theme` 与 `localStorage` 键 `bidding-theme`，首次访问跟随系统偏好，`index.html` 的内联脚本在首屏前应用主题；新增颜色应使用 `index.css` 中的 CSS 变量并为深色主题补值。

Electron 通过 `preload.cjs` 的 contextBridge 暴露 `openBidFolder`；主进程 IPC `bidding:open-bid-folder` 只允许 `upload/bids` 下的目录，并使用 `shell.openPath` 打开。不要放宽该路径校验，也不要在渲染进程启用 Node 集成。

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

# MinIO（可选；S3 API 默认 127.0.0.1:9005，网页控制台 9000）
D:\minio\bin\start.bat
```

- FastAPI/uvicorn 默认端口也是 8000，启动后端时使用 `--port 8001`（或为 Chroma 指定其他端口），避免冲突。
- Redis 版本需支持 RESP3（当前为 8.10.2）；redis-py 8.x 默认发送 `HELLO 3`，Redis 5 等旧版本会连接失败。
- Redis 同时承载任务状态（`REDIS_URL`，db 0）和 Celery broker（`CELERY_BROKER_URL`，db 0）；结果后端在 db 1。键前缀 `bidding:` 避免冲突。
- MinIO 由 `MINIO_ENABLED` 开关；连接参数为 `MINIO_ENDPOINT`（S3 API，默认 `127.0.0.1:9005`）、`MINIO_ACCESS_KEY`、`MINIO_SECRET_KEY`、`MINIO_BUCKET`（默认 `bidding`）与 `MINIO_SECURE`。`MINIO_ENDPOINT` 不是网页控制台端口 9000。
- Windows 系统代理会影响 Python 对 localhost 的 HTTP 请求。`.env` 已配置 `NO_PROXY=localhost,127.0.0.1`；运行 Chroma/MinIO 客户端前需确保该变量已加载，否则请求可能返回 502。

## 环境要求与约定

- Python 3.12（见 `.python-version`）。
- Node.js ≥ 22.12（本机为 24.22.0；Vite 8 / Tailwind CSS 4 / Electron 44 的要求）。
- SQLAlchemy 固定为 2.0.x（`>=2.0,<2.1`）。
- Tailwind CSS v4 通过 `@tailwindcss/vite` 插件接入，入口是 `frontend/src/index.css` 中的 `@import "tailwindcss";`，无需 `tailwind.config.js`。
- `.env` 包含 LLM（DeepSeek/智谱）、PostgreSQL、Redis、Chroma、MinerU 等配置，已被 `.gitignore` 排除；不要提交或打印其中的密钥。
- API 任务后端由 `TASK_BACKEND` 选择：`celery`（默认，需 Redis + worker）或 `memory`（单进程调试）。Celery broker/结果后端与超时由 `CELERY_*` 配置；运行 worker 前先加载 `.env`（`uv run` 会自动读取 `config.py` 的 `load_dotenv`）。
- 聊天模型由 `LLM_PROVIDER` 选择 `deepseek` 或 `zhipu`。DeepSeek key 不可用时可切换为 `LLM_PROVIDER=zhipu` + `ZHIPU_CHAT_MODEL=glm-4-plus`，无需改代码；嵌入固定走智谱 `embedding-3`。
- 标书编写的投标人资料来自 CLI 的 `upload/bidder_profile.json`（模板 `bidder_profile.example.json`）、`POST /api/bids` 请求体中的 `profile`，或通过 `company_id` 从 PostgreSQL 公司资料库载入后再用 `profile` 覆盖。公司资料库使用 `DATABASE_URL`（`postgresql+asyncpg://...`），首次访问自动建表，数据库不可用时接口返回 503 但不影响其他工作流。`upload/` 整体被 git 忽略；不要编造投标人事实，缺失字段必须保留【待填写】占位符；临时 profile 文件结束后由 worker 删除。
- MinIO 采用“本地缓存 + 远端镜像”：`upload/` 仍是所有流水线的读写目录；解析、抽取、入库、写标完成后尽力上传对应产物（失败仅记录警告，不中断任务），本地缺失时从 bucket 恢复。对象前缀为 `documents/<doc_id>/{source,parsed,images,bids}/`。`GET /api/documents`、`GET /api/bids` 合并本地与远端记录，`POST /api/bids` 与产物下载会按需恢复；`GET /api/health.storage` 为 `minio` 或 `local`。测试通过 `tests/conftest.py` 强制关闭 MinIO，并使用内存 Fake 客户端。
- `write` 有 CLI 与 HTTP/Celery 两个入口，共用同一流水线；运行前需完成解析与抽取，且 LLM key 可用。
- 修改功能后运行相关检查（`uv run pytest`、`npm --prefix frontend run build`），并按「固定任务」创建提交。
