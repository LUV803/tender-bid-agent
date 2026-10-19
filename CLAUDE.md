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
src/bidding/cli.py            命令行入口：doctor / parse / extract / ingest / inspect
src/bidding/config.py         .env 配置与路径约定
src/bidding/models.py         解析、抽取、索引共用数据结构
src/bidding/parsing/          PDF 解析：结构还原、PyMuPDF、MinerU、产物落盘
src/bidding/extraction/       结构化抽取：章节单元、LLM 容错、归一化/合并/grounding
src/bidding/indexing/         检索索引：分块、智谱 embedding、Chroma 读写与自检
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
```

解析产物在 `upload/parsed/<doc_id>/`：`content.md` 为带页码注释的权威全文，`manifest.json` 为摘要（不含全文），`raw/sections.json` 保存章节正文缓存；图片在 `upload/images/<doc_id>/`。`auto` 模式优先 MinerU，未配置 `MINERU_API_KEY` 时按设计跳过云端并使用 PyMuPDF 本地解析，降级原因会写入 manifest。

抽取产物为同目录的 `extraction.json`；逐单元结果缓存在 `extraction_parts/`（按内容哈希与提示词/模型版本失效），中断后可直接续跑。合同等通用章节按大小压缩，评分/资格/格式章节优先细分。`ungrounded_fields` 列出无法在原文中逐字定位的字段，`extraction_meta.partial` 表示使用了 `--limit` 或存在失败单元。

索引产物为同目录的 `index_manifest.json`。分块目标 450 字、硬上限 700 字、重叠 80 字；页码从嵌入文本中移除但保留在 metadata。嵌入使用智谱 `embedding-3`（2048 维），标书写入 `tender_document`，公告写入 `tender_announcement`；按 `doc_id` 先删后写，可安全重跑。`ingest` 先完成全部 embedding 再删除旧数据，避免中途失败丢失索引。

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
# Redis（127.0.0.1:6379，无密码）
.tools\redis-8.10.2\Redis-8.10.2-Windows-x64-msys2\redis-server.exe redis.conf

# ChromaDB（http://localhost:8000）
uv run chroma run --path ./chroma_db
```

- FastAPI/uvicorn 默认端口也是 8000，启动后端时使用 `--port 8001`（或为 Chroma 指定其他端口），避免冲突。
- Redis 版本需支持 RESP3（当前为 8.10.2）；redis-py 8.x 默认发送 `HELLO 3`，Redis 5 等旧版本会连接失败。
- Windows 系统代理会影响 Python 对 localhost 的 HTTP 请求。`.env` 已配置 `NO_PROXY=localhost,127.0.0.1`；运行 Chroma 客户端前需确保该变量已加载，否则请求可能返回 502。

## 环境要求与约定

- Python 3.12（见 `.python-version`）。
- Node.js ≥ 22.12（本机为 24.22.0；Vite 8 / Tailwind CSS 4 / Electron 44 的要求）。
- SQLAlchemy 固定为 2.0.x（`>=2.0,<2.1`）。
- Tailwind CSS v4 通过 `@tailwindcss/vite` 插件接入，入口是 `frontend/src/index.css` 中的 `@import "tailwindcss";`，无需 `tailwind.config.js`。
- `.env` 包含 LLM（DeepSeek/智谱）、PostgreSQL、Redis、Chroma、MinerU 等配置，已被 `.gitignore` 排除；不要提交或打印其中的密钥。
- 聊天模型由 `LLM_PROVIDER` 选择 `deepseek` 或 `zhipu`。DeepSeek key 不可用时可切换为 `LLM_PROVIDER=zhipu` + `ZHIPU_CHAT_MODEL=glm-4-plus`，无需改代码；嵌入固定走智谱 `embedding-3`。
- 修改功能后运行相关检查（`uv run pytest`、`npm --prefix frontend run build`），并按「固定任务」创建提交。
