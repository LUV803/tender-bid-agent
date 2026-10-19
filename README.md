# bidding

招标文件解析、结构化抽取、检索索引与多 Agent 标书编写。当前已打通单文档的
**解析 → 结构化抽取 → 分块嵌入 → Chroma 检索自检** 流水线，入口为 CLI 与
FastAPI HTTP API。

## 技术栈

- 后端：Python 3.12、uv、FastAPI、PyMuPDF、MinerU 云解析、OpenAI SDK
  （DeepSeek / 智谱）、zai-sdk（embedding-3）、ChromaDB。
- 规划中：Celery + Redis 分布式任务、SQLAlchemy + asyncpg、LangGraph、python-docx。
- 前端：React 19、TypeScript、Vite 8、Tailwind CSS 4、Electron 44（已有脚手架）。

## 快速开始

```bash
uv sync
cp .env.example .env        # 填入 API key；.env 已被 git 忽略

# 可选：启动 Redis 与 Chroma
.tools\redis-8.10.2\Redis-8.10.2-Windows-x64-msys2\redis-server.exe redis.conf
uv run chroma run --path ./chroma_db
```

Windows 上请保留 `.env` 中的 `NO_PROXY=localhost,127.0.0.1`，否则系统代理可能
拦截对本地 Chroma 的请求。Redis 需 8.x（redis-py 8 默认发送 `HELLO 3`）。

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

## HTTP API

```bash
# 默认 127.0.0.1:8001，端口在 .env 的 API_HOST / API_PORT 配置
uv run python -m api
# 等价：uv run uvicorn api.app:app --host 127.0.0.1 --port 8001
```

接口文档：<http://127.0.0.1:8001/docs>（端口 8001 避免与 Chroma 的 8000 冲突）。

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

任务表保存在进程内存中：服务重启会丢失任务记录，但 `upload/parsed/<doc_id>/`
下的解析、抽取与索引产物仍在。也正因如此，必须以单 worker 运行（`python -m api`
默认如此）；`uvicorn --workers N` 会让轮询请求落到其他进程并返回 404。同一
`doc_id` 同时只允许一个排队/执行中的任务（重复上传返回 409）；任务表容量与
保留时间由 `API_MAX_JOBS`、`API_JOB_TTL_SECONDS` 控制，并发执行数由
`API_JOB_WORKERS` 控制。

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
CLI 错误处理，以及 API 上传校验、202 轮询、可选抽取/入库和任务状态管理；全部为
离线测试，不访问外部 API。

## 前端

```bash
npm --prefix frontend run dev
npm --prefix frontend run build
npm --prefix frontend run electron:dev
```
