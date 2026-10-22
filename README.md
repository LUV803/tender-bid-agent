# Tender Bid Agent

Tender document intelligence and grounded bid generation.

招标文件解析、结构化抽取、检索索引与多 Agent 标书编写。当前已打通单文档的
**解析 → 结构化抽取 → 分块嵌入 → Chroma 检索自检 → 招标检索 → 多 Agent 标书编写**
流水线，入口为 CLI、FastAPI HTTP API 与 React/Electron 工作台。公司资料可保存
到 PostgreSQL，并在写标时一键载入。

## 技术栈

- 后端：Python 3.12、uv、FastAPI、Celery + Redis、PyMuPDF、MinerU 云解析、
  OpenAI SDK（DeepSeek / 智谱）、zai-sdk（embedding-3）、ChromaDB、LangGraph、
  python-docx、MinIO 对象存储（可选）、SQLAlchemy 2.0 + asyncpg（公司资料库）。
- 前端：React 19、TypeScript、Vite 8、Tailwind CSS 4、Electron 44。

## 界面预览

| 上传解析 | 招标检索 |
| --- | --- |
| ![上传解析](docs/screenshots/01-upload.png) | ![招标检索](docs/screenshots/02-search.png) |

| 投标编写 | 投标人资料 |
| --- | --- |
| ![投标编写](docs/screenshots/04-write.png) | ![投标人资料](docs/screenshots/03-write-profile.png) |

| 公司资料库 | 文档库 |
| --- | --- |
| ![公司资料库](docs/screenshots/05-companies.png) | ![文档库](docs/screenshots/06-library.png) |

| 历史产物 |
| --- |
| ![历史产物](docs/screenshots/07-history.png) |

## 安全边界

本项目按**单机工作台**设计：默认仅监听 `127.0.0.1:8001`，目前**没有账号体系、登录态、角色权限或用户数据隔离**。任何能访问 API 或 `upload/` 目录的人，都可以上传解析文档、检索、修改公司资料、发起写标并下载产物。

不要把 API、Redis、ChromaDB、PostgreSQL、MinIO 直接暴露到公网。多人或多机访问必须在外部实现访问控制（VPN、带 SSO/OIDC 的反向代理、IP 白名单、TLS 等），并为不同用户隔离文件系统与数据库。账号体系与权限隔离仍是待办项。

完整边界与部署建议见 [SECURITY.md](SECURITY.md)。

## 快速开始

```bash
uv sync
cp .env.example .env        # 填入 API key；.env 已被 git 忽略

# 必需：Redis（API 默认使用 Redis + Celery 后台任务）
.tools\redis-8.10.2\Redis-8.10.2-Windows-x64-msys2\redis-server.exe redis.conf

# 另开终端：Celery worker（Windows 必须 --pool=solo）
uv run celery -A bidding.celery_app worker --loglevel=info --pool=solo --queues=bidding

# 仅在需要入库/检索时启动 Chroma
uv run chroma run --path ./chroma_db

# 可选：MinIO 对象存储（先在 .env 设置 MINIO_* 并启用）
D:\minio\bin\start.bat
```

Windows 上请保留 `.env` 中的 `NO_PROXY=localhost,127.0.0.1`，否则系统代理可能
拦截对本地 Chroma 的请求。Redis 需 8.x（redis-py 8 默认发送 `HELLO 3`）。API
与 worker 必须在同一项目目录运行并共享 `upload/` 文件系统。

## CLI

```bash
# 检查配置与服务连通性（不打印密钥）
uv run python -m bidding doctor

# 1) 解析 PDF：auto 优先 MinerU，未配置 key 或失败时回退 PyMuPDF
uv run python -m bidding parse upload/files/<file>.pdf --parser auto

# 2) 结构化抽取（按章节 map-reduce，可断点续跑）
uv run python -m bidding extract <doc_id>
uv run python -m bidding extract <doc_id> --force          # 忽略缓存
uv run python -m bidding extract <doc_id> --limit 3       # 调试；结果标记 partial

# 3) 分块 + 嵌入 + 写入 Chroma，并做检索自检
uv run python -m bidding ingest <doc_id>
uv run python -m bidding ingest upload/files/<file>.pdf    # 解析 → 抽取 → 入库
uv run python -m bidding ingest <doc_id> --skip-extract    # 只解析并索引
uv run python -m bidding ingest <doc_id> --no-verify

# 4) 查看集合统计并做检索测试
uv run python -m bidding inspect <doc_id> --query "评标办法 评分标准 资格审查"
```

`ingest` 也可以直接接收 PDF 路径；`--force` 会强制重解析和重抽取。

## 多 Agent 投标文件编写

在解析和结构化抽取完成后，用 LangGraph 多 Agent 流程生成 Markdown 草稿和
Word 文件：

```bash
# 1) 填写投标人资料（缺失字段会生成【待填写：...】，系统不编造事实）
cp bidder_profile.example.json upload/bidder_profile.json

# 2) 生成标书草稿、Word 和内部审查报告
uv run python -m bidding write <doc_id>
# 等价入口：uv run python -m src.cli write <doc_id>

# 也可以指定资料和输出目录；--no-revise 跳过自动修订
uv run python -m bidding write <doc_id> \
  --profile upload/bidder_profile.json \
  --output-dir upload/bids/<doc_id> \
  --max-revisions 2 \
  --max-sections 0
```

`--max-sections` 控制分析师新增章节上限（0–3，默认 3）；固定 11 章始终保留。HTTP
接口默认 0，只生成固定章节。

产物默认在 `upload/bids/<doc_id>/`：

| 文件 | 内容 |
| --- | --- |
| `bid.md` | 可用于继续编辑的 Markdown 草稿 |
| `bid.docx` | 带封面、目录、标题层级、表格、页眉页脚和页码的 Word 文件 |
| `bid_review.md` | 内部审查报告：待填写/待核对清单、资料缺口、审查问题、评分响应矩阵、修订记录和生成警告 |
| `bid_manifest.json` | 输入来源、章节结构、LLM 调用记录、统计、矩阵、警告与 Agent trace |

流程与事实边界：

- **分析师**：分析评标办法、资格条件和格式要求，给出响应策略与章节提示；只能
  在固定大纲上补充最多 3 个章节，不能删除投标函、资格、报价、施工方案等必备章节。
- **检索器**：本地按标题、路径、正文关键词和结构化抽取条目打分，返回带页码的
  原文证据；不依赖 Chroma，保证 CLI 离线可审计。
- **编写 Agent**：按行政/商务/技术三个角色顺序编写，共 11 个固定章节；提示词中
  招标文件事实只来自解析产物，投标人事实只来自 profile。
- **审查 Agent**：检查评分覆盖、强制要求、格式/签章、投标预算误用和未经资料
  支持的事实；高优先级问题可触发最多 `--max-revisions` 个章节的自动修订，
  事实/编造类问题优先于评分覆盖类问题。
- **汇总器**：生成 Markdown/Word，并由系统（而非 LLM）把投标人资料渲染成
  “投标人基本情况、资质、业绩、人员、财务”等附表，避免表格数据被改写。

占位符约定：投标人资料缺失写 **【待填写：具体事项】**；招标文件事实缺失或无法
逐字核对写 **【待核对：具体事项】**。`bid.md`/`bid.docx` 始终是待补齐草稿，
提交前必须按 `bid_review.md` 逐项填写、核对签章和报价。

## HTTP API

```bash
# 终端 1：Celery worker（Windows 必须 solo pool）
uv run celery -A bidding.celery_app worker --loglevel=info --pool=solo --queues=bidding

# 终端 2：默认 127.0.0.1:8001，端口在 .env 的 API_HOST / API_PORT 配置
uv run python -m api
# 等价：uv run uvicorn api.app:app --host 127.0.0.1 --port 8001

# 连通性检查：result 应包含 "pong"
uv run celery -A bidding.celery_app call bidding.ping
```

接口文档：<http://127.0.0.1:8001/docs>（端口 8001 避免与 Chroma 的 8000 冲突）。
API 进程只负责接收上传/请求、写 Redis 任务记录和投递 Celery 消息；解析、抽取、
入库与标书编写都在 worker 中执行，因此 POST 会立即返回。

### 上传并创建解析任务

```bash
curl -X POST "http://127.0.0.1:8001/api/parse?parser=auto&extract=true&ingest=true" \
  -F "file=@upload/files/<file>.pdf"
```

- `file`：PDF 表单字段，最大 `API_MAX_UPLOAD_MB`（默认 200 MB）。
- `parser`：`auto`（默认）/ `cloud` / `local`。
- `extract`：解析后继续结构化抽取，默认 `false`。
- `ingest`：解析后继续分块、嵌入并写入 Chroma，默认 `false`；与 `extract`
  相互独立，可只入库不抽取。
- 立即返回 `202 Accepted`，`Location` 与 `status_url` 指向任务地址：

```json
{
  "job_id": "1f0e...",
  "status": "queued",
  "stage": "queued",
  "doc_id": "6055594244948",
  "status_url": "/api/jobs/1f0e..."
}
```

### 轮询任务

```bash
curl "http://127.0.0.1:8001/api/jobs/<job_id>"
```

- `status`：`queued` → `running` → `succeeded` / `failed`。
- `kind`：`parse`（解析流水线）或 `bid`（标书编写）；`options` 只包含该类型
  对应的参数。
- `stage`：解析任务为 `queued` → `parse` → `extract`（可选）→ `ingest`
  （可选）→ `done`；写标任务为 `queued` → `write` → `done`。失败时停留在
  出错阶段。
- `progress`：最近 200 条进度消息；`result` 随阶段完成逐步出现，包含
  `parse`（manifest）、`extract`（抽取结果）、`ingest`（索引 manifest）或
  `bid`（写标摘要）。
- 抽取通常需要 2–4 分钟，写标可能更久；客户端只需轮询，不要为 POST 设置长读
  超时。

任务记录与进度保存在 Redis（`bidding:job:<id>`、`bidding:active-doc:<doc_id>`），
结果随阶段完成写入任务哈希；Celery 结果后端只保存很小的任务返回值。API 重启不会
中断正在执行的任务，轮询记录也不会丢，记录在 `API_JOB_TTL_SECONDS`（默认 24 小时）
后过期。API 与 worker 必须共享 `upload/` 文件系统。

同一 `doc_id` 同时只允许一个排队/执行中的任务（Redis 原子锁，重复提交返回 409），
避免多个 worker 同时写 `upload/parsed/<doc_id>/` 或 `upload/bids/<doc_id>/`。
Redis 或 broker 不可用时，提交返回 503；轮询在 Redis 不可用时同样返回 503。

### 异步编写投标文件

前提：该 `doc_id` 已完成解析与结构化抽取。`profile` 直接放在 JSON 请求体中，
字段与 `bidder_profile.json` 相同；缺失字段仍会生成【待填写】占位符。

```bash
curl -X POST "http://127.0.0.1:8001/api/bids" \
  -H "Content-Type: application/json" \
  -d '{
    "doc_id": "6055594244948",
    "profile": {"company": {"name": "示例投标人"}},
    "max_sections": 0,
    "max_revisions": 2
  }'
```

- `doc_id`：必填，必须是 `upload/parsed/<doc_id>/` 下已有解析产物的安全目录名。
- `profile`：JSON 对象，最大 2 MB；空对象合法，将生成全占位符草稿。
- `max_sections`：分析师新增章节上限，0–3，默认 0（固定 11 章始终保留）。
- `max_revisions`：自动修订章节数上限，0–10，默认 2。
- 返回与 `/api/parse` 相同的 `202`、`job_id` 与 `status_url`，继续用
  `GET /api/jobs/<job_id>` 轮询；该任务 `kind` 为 `bid`，完成后
  `result.bid` 包含 `doc_id`、`project_name`、`bidder`、`stats`、`artifacts`
  和 `warnings`。
- 文档未解析返回 404，缺少 `extraction.json`、已有同文档任务或参数不合法分别
  返回 409 / 409 / 422。完整产物仍在 `upload/bids/<doc_id>/`，其中
  `bid_manifest.json` 含完整统计与 Agent trace。
- API 把请求体写成 `upload/tmp/<job_id>/bidder_profile.json`，worker 通过
  Celery 任务 `bidding.write_bid` 读取并在结束后删除；API 与 worker 必须共享
  `upload/` 文件系统。
- 可选 `company_id`：先从 PostgreSQL 公司资料库载入已存资料，再把请求体
  `profile` 深合并覆盖到其上（嵌套对象逐键合并，列表和标量整体替换）。

### 招标检索

对已解析文档做同步检索，不需要先入库；配置了智谱 key 时自动融合 Chroma 向量
结果，不可用则降级为关键词 + 结构化检索并在 `warnings` 中说明。

```bash
curl "http://127.0.0.1:8001/api/search?doc_id=6055594244948&q=投标报价得分怎么算&limit=10&mode=auto"
```

- `doc_id`：必填，`upload/parsed/<doc_id>/` 下已有解析产物；本地缺失时会先尝试
  从 MinIO 恢复。
- `q`：必填，最多 200 字，支持自然语言问题、关键词或表号。
- `limit`：1–30，默认 10。
- `mode`：`auto`（默认，向量可用则混合）、`lexical`（不调用嵌入）、`hybrid`
  （强制合并向量层）。
- 返回 `mode`（实际使用的模式）、`vector_used`、`warnings`、`elapsed_ms`、
  `counts`、`tables` 与 `results`。每条结果带 `page_hint` / `page_start` /
  `page_end`、`section_path`、`matched_terms` 与 `source`；评分因素、资格要求、
  评审标准、关键日期、合同条款等结构化条目与章节、表格统一排序。
- 检索层会过滤目录页，按解析章节的页码范围定位多页表格，并把表号/章节重复项
  合并为一条。页码引用用于回查 PDF，仍需人工核对原文。

### 公司资料库（PostgreSQL）

公司资料以整份 profile JSON 存在 `companies` 表，首次访问自动建表。配置
`DATABASE_URL=postgresql+asyncpg://user:password@host:5432/bidding`；数据库未配置
或不可达时，公司接口返回 503，不影响解析、检索与写标。

| 接口 | 说明 |
| --- | --- |
| `GET /api/companies` | 按更新时间倒序列出公司，含名称、统一社会信用代码、联系人、资质数量等摘要 |
| `POST /api/companies` | 新建，返回 `201` 与完整记录；请求体 `{"profile": {...}}` |
| `GET /api/companies/{company_id}` | 读取单条记录，不存在返回 `404` |
| `PUT /api/companies/{company_id}` | 全量替换 profile，返回更新后的记录 |
| `DELETE /api/companies/{company_id}` | 删除记录，成功返回 `204` |

`profile` 字段与 `bidder_profile.json` 相同，最大 2 MB。工作台“公司资料”页可直接
编辑常用字段或高级 JSON，并在“投标编写”页载入后发起写标。

### 文档库、历史产物与文件预览

前端工作台使用以下只读接口；它们扫描本地文件系统，并在启用 MinIO 时合并远端
清单，因此 CLI 生成的历史产物也会立即出现在列表中：

| 接口 | 说明 |
| --- | --- |
| `GET /api/health` | 服务状态、版本、任务后端与存储模式（`minio` / `local`） |
| `GET /api/search` | 在已解析文档中做混合/关键词检索，返回页码、表号与章节引用 |
| `GET /api/documents` | 扫描 `upload/parsed/`，返回解析清单、抽取/索引状态与关联标书摘要 |
| `GET /api/bids` | 扫描 `upload/bids/`，按生成时间倒序返回历史产物摘要 |
| `GET /api/bids/{doc_id}` | 返回产物详情：评分项覆盖、章节映射、审查问题、待填写清单与修订记录 |
| `GET /api/bids/{doc_id}/files/{filename}` | 预览或下载产物；`?download=1` 作为附件，附件名只允许 `bid.md`、`bid.docx`、`bid_review.md`、`bid_manifest.json` |

`doc_id` 与文件名都经过白名单和路径校验，不能读取 `upload/bids/<doc_id>/`
之外的文件。评分覆盖只表示“评分项是否已映射到章节”，**不是实际得分**；草稿
仍需按 `bid_review.md` 人工核对后提交。

CORS 仅允许 `localhost` / `127.0.0.1` 的任意端口与 Electron 的 `null` 源，便于
Vite 开发服务器和 `file://` 桌面渲染进程直接访问本地 API。

Windows 上 worker 请使用 `--pool=solo`；需要并发时在 POSIX 主机上运行 prefork
worker，或在本机启动多个 solo worker（自行控制 CPU 与 API 配额）。硬/软超时由
`CELERY_TASK_TIME_LIMIT`、`CELERY_SOFT_TIME_LIMIT` 控制；Windows 的 solo pool
对软超时支持有限，以硬超时为准。

本地快速调试可切回进程内线程池，不需要 Redis 和 worker：

```bash
TASK_BACKEND=memory uv run python -m api
```

```powershell
$env:TASK_BACKEND="memory"; uv run python -m api
```

memory 模式下任务表随 API 进程重启丢失，必须以单进程运行；并发数与容量由
`API_JOB_WORKERS`、`API_MAX_JOBS` 控制，`API_JOB_TTL_SECONDS` 控制记录过期。

## MinIO 对象存储（可选）

本地 `upload/` 仍是流水线的工作缓存；启用 MinIO 后，每个阶段完成后都会把产物
镜像到 bucket，并在本地文件缺失时按需恢复。MinIO 不可用或未配置不会中断解析、
抽取、入库和写标，只会记录警告；`bidding doctor` 与 `bidding storage status`
会显示连接状态。

```dotenv
MINIO_ENABLED=true
MINIO_ENDPOINT=127.0.0.1:9005   # S3 API 端口，不是网页控制台的 9000
MINIO_ACCESS_KEY=...
MINIO_SECRET_KEY=...
MINIO_BUCKET=bidding
MINIO_SECURE=false
```

对象布局（每个文档一个前缀）：

| 前缀 | 内容 |
| --- | --- |
| `documents/<doc_id>/source/` | 原始 PDF |
| `documents/<doc_id>/parsed/` | `content.md`、`manifest.json`、`raw/sections.json`、`extraction.json`、`extraction_parts/`、`index_manifest.json` |
| `documents/<doc_id>/images/` | 解析出的图片 |
| `documents/<doc_id>/bids/` | `bid.md`、`bid.docx`、`bid_review.md`、`bid_manifest.json` |

```bash
# 检查配置、bucket 连通性（doctor 同时检查 PyMuPDF/Chroma/Redis）
uv run python -m bidding doctor
uv run python -m bidding storage status

# 将本地历史产物全量补传到 MinIO（也可只同步一个 doc_id）
uv run python -m bidding storage sync
uv run python -m bidding storage sync <doc_id>

# 从 MinIO 恢复到 upload/（parse、图片、源 PDF 与投标产物）
uv run python -m bidding storage restore <doc_id>
```

HTTP 工作台会自动使用 MinIO：`GET /api/documents` 与 `GET /api/bids` 合并本地
和远端记录；`POST /api/bids` 在本地缺少解析/抽取文件时会先恢复；预览/下载产物
时若本地没有对应文件，会先从 MinIO 取回再返回。`GET /api/health` 的 `storage`
字段显示 `minio` 或 `local`。API 与 worker 使用同一 bucket；即使不共享 `upload/`
文件系统，解析完成后的阶段产物也可以从对象存储恢复。

## 解析策略

- MinerU 云端 vlm 模型为主，PyMuPDF 本地兜底。MinerU 单文件上限 200 页，
  超出时按页均分为多份上传，合并 Markdown 后按原 TOC 重新对齐章节。
- 未配置 `MINERU_API_KEY` 时，`auto` 会记录降级原因并使用 PyMuPDF；需要更完整的
  表格/扫描件还原时请配置 key 后加 `--force` 重跑。
- 产物在 `upload/parsed/<doc_id>/`：
  - `content.md`：带 `<!-- page: N -->` 页码注释的权威全文。
  - `manifest.json`：页面、TOC、图片、章节摘要与警告。
  - `raw/sections.json`：各章节正文缓存。
  - `extraction.json`：结构化抽取结果与 `ungrounded_fields`。
  - `extraction_parts/`：逐单元抽取缓存，中断后可续跑。
  - `index_manifest.json`：嵌入模型、分块参数、写入数量与自检结果。
- 图片在 `upload/images/<doc_id>/`。

## 抽取与索引

- 抽取按章节构建单元（每单元上限 `EXTRACT_MAX_CHARS=12000` 字），合同条款等
  通用章节按大小压缩，评分/资格/格式章节优先细分。
- LLM 输出经容错归一化、去重合并，并用 `source_quote` 对原文做逐行 grounding；
  无法在原文中定位的字段列入 `ungrounded_fields`，不会静默丢弃。
- 分块目标 450 字、硬上限 700 字、重叠 80 字；嵌入文本去掉页码标记，页码保留在
  metadata（`page_start` / `page_end` / `page_hint`）中，便于引用回 PDF。
- 嵌入使用智谱 `embedding-3`（2048 维），写入 `tender_document`；公告类文档写入
  `tender_announcement`。按 `doc_id` 先删后写，可安全重跑。

## LLM 配置

`LLM_PROVIDER` 可选 `deepseek` 或 `zhipu`：

```dotenv
LLM_PROVIDER=zhipu
ZHIPU_API_KEY=...
ZHIPU_CHAT_MODEL=glm-4-plus
```

- DeepSeek 走 `DEEPSEEK_*` 配置；智谱聊天走 `ZHIPU_CHAT_MODEL`（非推理模型，
  默认 `glm-4-plus`），两者均为 OpenAI 兼容接口。
- 若 DeepSeek key 不可用，可将 `LLM_PROVIDER` 切换为 `zhipu`，无需改代码。
- 向量嵌入始终使用智谱 `embedding-3`，与聊天 provider 无关。

## 测试

```bash
uv run pytest
```

测试覆盖页码分片、结构识别、抽取归一化/合并/grounding、单元构建、分块与页码映射、
CLI 错误处理，以及 API 上传/写标校验、202 轮询、可选抽取/入库、内存任务表、
Redis 任务状态与 TTL/文档锁、Celery 任务路由与生命周期、双后端切换和
`POST /api/bids` 端到端流程；文档库与产物接口覆盖清单扫描、路径白名单与文件
预览；招标检索覆盖多页表定位、目录页过滤、结构化去重、混合/关键词模式与
`/api/search` 参数校验；公司资料库覆盖 PostgreSQL 存取、`company_id` 载入覆盖
与不可用时的 503；标书编写部分覆盖 profile 占位符、招标上下文与页码引用、
固定大纲/评分矩阵、LangGraph 全流程、事实优先修订、Markdown/Word 产物和 CLI。
MinIO 镜像、恢复与 API 远端回退使用内存 Fake 客户端覆盖。全部为离线测试，
不访问外部 API、真实 Redis、真实 PostgreSQL 或真实 MinIO。

## 前端工作台

```bash
# 终端 1：API（默认 127.0.0.1:8001；需 Redis + worker 时按上文启动）
uv run python -m api

# 终端 2：Vite 开发服务器（/api 代理到 8001）
npm --prefix frontend run dev

# 生产构建；类型检查由 tsc -b 完成
npm --prefix frontend run build
npm --prefix frontend run lint

# Electron：dev 模式复用 5173 开发服务器；start 先构建再加载 dist
npm --prefix frontend run electron:dev
npm --prefix frontend run electron:start
```

工作台固定为左侧步骤导航 + 右侧工作区，顶部显示后端服务状态：

1. **上传解析**：拖拽或选择 PDF，设置解析器（auto/MinerU/PyMuPDF）、结构化
   抽取与向量入库，实时展示队列阶段、进度日志和解析/抽取/入库摘要。
2. **招标检索**：选择已解析文档，输入自然语言问题或表号；可在智能混合与
   关键词 + 结构化两种模式间切换。结果展示页码、表号、章节路径和命中词，
   相关表格卡片可一键继续检索；命中词在摘要中高亮。
3. **投标编写**：选择已完成抽取的文档；常用投标人字段用表单填写，复杂结构在
   可折叠 JSON 编辑器中编辑，两种视图同步。可从公司资料库载入资料，设置新增
   章节与自动修订上限，完成后展示评分项/分值覆盖、章节映射、审查问题与修订记录。
4. **公司资料**：管理 PostgreSQL 中的公司资料，支持新建、编辑、删除与高级 JSON；
   保存后可在投标编写页直接载入。
5. **文档库**：列出 `upload/parsed/` 下已有文档、抽取/索引状态和历史标书，可
   直接检索、发起编写或跳转到产物。
6. **历史产物**：浏览 `upload/bids/` 下的历史结果，在线预览 Markdown、下载
   Markdown/Word/清单，并在 Electron 中通过受限 IPC 在资源管理器中定位产物目录。

顶栏提供深色/浅色主题切换，首次访问跟随系统偏好，手动选择后写入
`localStorage`（`bidding-theme`）并优先于系统设置；`index.html` 的内联脚本在
首屏渲染前应用主题，避免闪烁。窄屏下按钮只保留图标，`aria-label` 仍会说明目标主题。

Vite 的 `base` 固定为 `./`，构建产物可以直接由 Electron 以 `file://` 加载；
浏览器开发时使用相对 `/api` 路径并由 Vite 代理，桌面构建则默认请求
`http://127.0.0.1:8001`（可用 `VITE_API_BASE_URL` 覆盖）。Markdown 预览不启用
原始 HTML，且会隐藏内部页码/分页注释。
