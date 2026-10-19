# bidding

招标文件解析、结构化抽取、检索索引与多 Agent 标书编写。当前已打通单文档的
**解析 → 结构化抽取 → 分块嵌入 → Chroma 检索自检** 流水线，入口为 CLI 与
FastAPI HTTP API。

## 技术栈

- 后端：Python 3.12、uv、FastAPI、Celery + Redis、PyMuPDF、MinerU 云解析、
  OpenAI SDK（DeepSeek / 智谱）、zai-sdk（embedding-3）、ChromaDB、LangGraph、
  python-docx。
- 规划中：SQLAlchemy + asyncpg；标书编写的 HTTP/Celery 入口。
- 前端：React 19、TypeScript、Vite 8、Tailwind CSS 4、Electron 44（已有脚手架）。

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
  --max-revisions 2
```

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
  支持的事实；高优先级问题可触发最多 `--max-revisions` 个章节的自动修订。
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
API 进程只负责收文件、写 Redis 任务记录和投递 Celery 消息；解析、抽取和入库都在
worker 中执行，因此 POST 会立即返回。

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
- `stage`：`queued` → `parse` → `extract`（可选）→ `ingest`（可选）→ `done`；
  失败时停留在出错阶段。
- `progress`：最近 200 条进度消息；`result` 随阶段完成逐步出现，包含
  `parse`（manifest）、`extract`（抽取结果）和 `ingest`（索引 manifest）。
- 抽取通常需要 2–4 分钟，客户端只需轮询，不要为 POST 设置长读超时。

任务记录与进度保存在 Redis（`bidding:job:<id>`、`bidding:active-doc:<doc_id>`），
结果随阶段完成写入任务哈希；Celery 结果后端只保存很小的任务返回值。API 重启不会
中断正在执行的任务，轮询记录也不会丢，记录在 `API_JOB_TTL_SECONDS`（默认 24 小时）
后过期。API 与 worker 必须共享 `upload/` 文件系统。

同一 `doc_id` 同时只允许一个排队/执行中的任务（Redis 原子锁，重复上传返回 409），
避免多个 worker 同时写 `upload/parsed/<doc_id>/`。Redis 或 broker 不可用时，提交
返回 503；轮询在 Redis 不可用时同样返回 503。

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
CLI 错误处理，以及 API 上传校验、202 轮询、可选抽取/入库、内存任务表、Redis
任务状态与 TTL/文档锁、Celery 任务生命周期和双后端切换；标书编写部分覆盖
profile 占位符、招标上下文与页码引用、固定大纲/评分矩阵、LangGraph 全流程、
预算金额误用修订、Markdown/Word 产物和 CLI。全部为离线测试，不访问外部 API
或真实 Redis。

## 前端

```bash
npm --prefix frontend run dev
npm --prefix frontend run build
npm --prefix frontend run electron:dev
```
