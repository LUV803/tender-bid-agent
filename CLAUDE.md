# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 固定任务

- 每次修改完成后，都应创建一个对应的 Git commit，便于后续追踪和回滚。

## 项目概况

`bidding` 项目目前处于脚手架阶段，尚无后端业务代码。技术栈如下：

- **后端**：Python 3.12 + FastAPI/uvicorn；SQLAlchemy 2.0 + asyncpg；Celery + Redis；ChromaDB 向量库；LangGraph；OpenAI SDK / zai-sdk；python-docx / PyMuPDF。
- **前端**：React 19 + TypeScript + Vite 8 + Tailwind CSS 4；Electron 44 桌面外壳。
- **包管理**：Python 使用 uv（`pyproject.toml` + `uv.lock`）；前端使用 npm（`frontend/package.json` + `package-lock.json`）。

## 目录结构

```text
src/bidding/                  Python 包（src 布局）；目前仅 __init__.py
frontend/src/                 React 前端源码
frontend/electron/main.cjs    Electron 主进程
redis.conf                    Redis 本地开发配置
.tools/                       本机下载的 Redis 可执行文件（git 忽略，不入库）
redis_data/                   Redis 持久化数据（git 忽略）
chroma_db/                    ChromaDB 持久化数据（git 忽略）
.env                          本地环境变量与密钥（git 忽略，禁止提交）
```

## 常用命令

### Python

```bash
uv sync                  # 按 uv.lock 同步依赖
uv add <package>         # 新增运行时依赖
uv add --dev <package>   # 新增开发依赖
uv run pytest            # 运行测试（当前尚无测试文件）
```

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
- 修改功能后运行相关检查（`uv run pytest`、`npm --prefix frontend run build`），并按「固定任务」创建提交。
