# Huai-Coder

Huai-Coder 是一个面向本地项目的 AI 编程 Agent。它把对话、项目工作区、ReAct 工具循环、长期记忆、上下文压缩、子 Agent、浏览器 MCP 和本地命令 Runner 组合成一个可运行的开发工作台。

项目的目标不是只返回代码片段，而是让 Agent 能够在用户绑定的工作区内完成一条可追踪的任务链：理解需求、制定计划、调用工具、观察结果、遇到错误后重新思考、修改文件、运行验证，并把过程和结果展示在前端。

> 当前项目处于持续开发阶段。涉及真实文件写入、命令执行、浏览器操作或远程仓库写入时，请先在测试工作区验证权限和安全策略。

## 目录

- [项目定位](#项目定位)
- [核心能力](#核心能力)
- [系统架构](#系统架构)
- [一次任务如何执行](#一次任务如何执行)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [工作区与本地 Runner](#工作区与本地-runner)
- [浏览器 MCP](#浏览器-mcp)
- [记忆与上下文压缩](#记忆与上下文压缩)
- [子 Agent](#子-agent)
- [HTTP API](#http-api)
- [测试与质量检查](#测试与质量检查)
- [故障排查](#故障排查)
- [安全边界](#安全边界)
- [开发与扩展](#开发与扩展)
- [相关文档](#相关文档)

## 项目定位

Huai-Coder 适合以下场景：

- 在本地项目目录中分析代码、修改文件和运行测试；
- 让 Agent 根据工具返回结果持续推进任务，而不是只执行一次调用；
- 对较长会话保留项目事实、决策和用户偏好；
- 在上下文接近模型限制时自动压缩历史消息，继续当前任务；
- 通过本地 Runner 执行宿主机上的 Python、Node、Java、Go 等开发命令；
- 通过 MCP 接入浏览器、GitHub 或其他外部能力；
- 让高风险工具调用进入审批、审计和可恢复的执行流程。

## 核心能力

| 能力 | 说明 | 主要实现位置 |
| --- | --- | --- |
| 对话式编程 | 以会话为单位保存消息、工具调用和执行过程 | `backend/app/main.py`、`frontend/src/` |
| ReAct 循环 | 在“思考—行动—观察”之间迭代，直到完成、失败或达到预算 | `backend/app/agent.py`、`backend/app/executor.py` |
| 原生工具 | 目录浏览、文件读取、代码搜索、文件写入、命令执行等 | `backend/app/tools.py`、`backend/app/registry.py` |
| 本地工作区 | 项目可以绑定到宿主机目录，代码和生成文件直接落盘到该目录 | `backend/app/local_runner.py`、`backend/app/runner_server.py` |
| 长期记忆 | 支持用户级、项目级、会话级记忆，保存事实、决策和可复用经验 | `backend/app/memory.py`、`backend/app/models.py` |
| 上下文压缩 | 按 token 预算和消息重要性压缩历史，保留最近对话与任务摘要 | `backend/app/context.py` |
| 子 Agent | Explorer、Planner、Coder、Tester 分工协作，并限制并发和单轮预算 | `backend/app/agents/` |
| MCP | 动态连接外部工具服务器，支持工具发现、状态刷新、审批和审计 | `backend/app/mcp/`、`backend/app/main.py` |
| 浏览器自动化 | 通过 Playwright MCP 执行导航、点击、输入、等待和页面快照 | `backend/mcp.example.json` |
| 可观测执行 | 实时展示计划、工具状态、结果、审批和审计事件 | `backend/app/main.py`、`frontend/src/main.tsx` |

## 系统架构

### 组件架构

~~~mermaid
flowchart LR
    U[用户浏览器] --> FE[Frontend\nReact + Vite + Nginx]
    FE --> API[Backend API\nFastAPI]
    API --> AGENT[Agent / ReAct Loop]
    API --> RUN[Run / Plan / Approval]
    API --> DB[(PostgreSQL)]

    AGENT --> REG[Tool Registry]
    REG --> NATIVE[原生工具\n文件 / 搜索 / 命令]
    REG --> MCP[MCP Client Manager]
    REG --> SUB[Sub-agents\nExplorer / Planner / Coder / Tester]

    NATIVE --> WS[/绑定工作区/]
    NATIVE --> RUNNER[Local Runner\n宿主机 :8765]
    MCP --> PW[Playwright MCP\n宿主机 :8931]
    MCP --> EXT[其他 MCP Server\nGitHub / 自定义服务]

    AGENT --> MEMORY[Memory Service\n用户 / 项目 / 会话]
    AGENT --> CONTEXT[Context Manager\n预算 / 压缩 / 摘要]
    MEMORY --> DB
    CONTEXT --> DB
    RUN --> DB
~~~

### Docker 与宿主机拓扑

Docker 负责运行 Web、API 和数据库；需要访问用户本机文件系统或本机浏览器时，再通过宿主机 Runner 和 Playwright MCP 桥接出去。

~~~mermaid
flowchart TB
    B[浏览器] -->|http://localhost| F[frontend 容器\nNginx :80]
    F -->|/api| A[backend 容器\nFastAPI :8000]
    A --> D[(db 容器\nPostgreSQL :5432)]
    A -->|host.docker.internal:8765| R[宿主机 Local Runner]
    A -->|host.docker.internal:8931| P[宿主机 Playwright MCP]
    R --> W[用户绑定的本地工作区]
    P --> C[浏览器实例]
    A -->|挂载 /workspace| S[项目源代码]
~~~

### 目录与运行时边界

| 区域 | 作用 | 生命周期 |
| --- | --- | --- |
| `frontend` 容器 | 提供用户界面和静态资源 | Docker Compose 管理 |
| `backend` 容器 | Agent、API、工具、记忆、MCP 客户端 | Docker Compose 管理 |
| `db` 容器 | 保存项目、会话、消息、记忆、运行记录 | `postgres_data` volume 持久化 |
| `/workspace` | 容器内映射的 Huai-Coder 源码目录 | 容器重建后仍由宿主机文件提供 |
| Local Runner | 在宿主机绑定目录执行命令和自动准备依赖 | Windows 本地进程 |
| Playwright MCP | 在宿主机控制浏览器 | Windows 本地进程 |
| `.huai-coder-runtime` | Runner/MCP 日志、npm 缓存、进程状态 | 本地运行时目录，已加入忽略规则 |

## 一次任务如何执行

~~~mermaid
sequenceDiagram
    participant User as 用户
    participant UI as 前端
    participant API as FastAPI
    participant Agent as Agent/ReAct
    participant Tool as 工具或 MCP
    participant WS as 工作区/Runner
    participant DB as PostgreSQL

    User->>UI: 提交任务
    UI->>API: 创建 run
    API->>DB: 保存会话与任务状态
    API->>Agent: 载入消息、记忆和项目上下文
    Agent->>Agent: 规划并选择下一步工具
    Agent->>Tool: 请求原生工具或 MCP 工具
    Tool->>WS: 读取/写入文件或执行命令
    WS-->>Tool: 返回 stdout、stderr、退出码
    Tool-->>Agent: 返回结构化 observation
    Agent->>Agent: 判断完成、修正或继续
    Agent->>DB: 写入消息、工具事件、记忆和审计记录
    Agent-->>UI: 推送过程与最终结果
    UI-->>User: 展示计划、状态、结果和下一步
~~~

执行循环的核心规则是：

1. 先读取任务相关上下文和可用工具；
2. 需要时生成计划，并把计划拆成可追踪的任务节点；
3. 每次只选择一个或一组明确的工具动作；
4. 把工具结果作为下一轮推理输入；
5. 如果命令失败、文件不存在或测试失败，重新分析原因并修正；
6. 达到完成条件、token 预算、迭代预算或安全边界时结束本轮；
7. 任务结束后提取可复用信息，写入对应层级的长期记忆。

## 项目结构

~~~text
Huai-Coder/
├─ backend/
│  ├─ app/
│  │  ├─ agents/                 # 子 Agent：探索、规划、编码、测试
│  │  ├─ mcp/                    # MCP 配置、连接、工具发现和调用
│  │  ├─ agent.py                # 主 Agent / ReAct 编排
│  │  ├─ context.py              # token 预算与上下文压缩
│  │  ├─ database.py             # SQLAlchemy 异步数据库
│  │  ├─ executor.py             # 计划、任务、工具执行
│  │  ├─ local_runner.py         # 后端到宿主机 Runner 的客户端
│  │  ├─ llm.py                  # LLM 请求适配
│  │  ├─ main.py                  # FastAPI 路由和应用入口
│  │  ├─ memory.py               # 长期记忆提取、检索和分层
│  │  ├─ models.py               # 数据模型
│  │  ├─ planner.py              # 任务计划与状态转换
│  │  ├─ registry.py             # 原生工具和 MCP 工具注册表
│  │  ├─ runner_server.py        # 宿主机 Local Runner HTTP 服务
│  │  ├─ security.py             # 路径、命令和审批安全策略
│  │  └─ tools.py                # 文件、搜索、命令等工具实现
│  ├─ migrations/                # 数据库迁移
│  ├─ tests/                     # 后端单元测试和集成测试
│  ├─ mcp.example.json           # MCP 配置模板
│  ├─ pyproject.toml             # Python 依赖与测试配置
│  └─ start_server.py            # 本地后端辅助入口
├─ frontend/
│  └─ src/
│     ├─ main.tsx                # React 页面、状态和 API 调用
│     └─ style.css               # 页面主题和组件样式
├─ scripts/
│  ├─ start-local.cmd            # Windows 一键启动入口
│  ├─ start-local.ps1            # 启动 Runner、MCP 和 Docker
│  ├─ stop-local.cmd             # Windows 一键停止入口
│  └─ stop-local.ps1             # 停止本地服务和 Docker
├─ docs/                         # 设计、部署和功能说明
├─ docker-compose.yml            # frontend、backend、db 编排
├─ Dockerfile.backend            # 后端镜像
├─ Dockerfile.frontend           # 前端镜像
├─ .env.example                  # Docker 环境变量模板
└─ README.md
~~~

## 快速开始

### 前置依赖

| 依赖 | 用途 | 说明 |
| --- | --- | --- |
| Docker Desktop | 运行 frontend、backend、PostgreSQL | Docker Engine 和 Compose 均需可用 |
| Python 3.11+ | 运行宿主机 Local Runner | 仅使用 Docker + MCP 时不需要手动启动 Runner |
| Node.js / npm / npx | 运行 Playwright MCP | 一键脚本会用 `npx` 获取 MCP 包并缓存 |
| PowerShell | Windows 一键脚本 | Windows 10/11 推荐使用 PowerShell 5+ |
| LLM API | 驱动 Agent 推理 | 在 `.env` 中填写兼容 OpenAI Chat Completions 的地址、密钥和模型 |

### 方式一：只启动 Docker

在项目根目录执行：

~~~powershell
Copy-Item .env.example .env
~~~

编辑 `.env`，至少设置本地数据库密码和 LLM 配置：

~~~dotenv
POSTGRES_PASSWORD=change-me
LLM_BASE_URL=https://your-llm-endpoint/v1
LLM_API_KEY=your-api-key
LLM_MODEL=your-model-name
~~~

启动服务：

~~~powershell
docker compose up -d --build
~~~

访问：

- Web：<http://localhost>
- Backend 健康检查：<http://127.0.0.1:8000/health>

停止 Docker 服务：

~~~powershell
docker compose down
~~~

> 该方式适合文件分析、代码生成和容器内工作流。若要让 Agent 操作宿主机上任意本地项目，使用下面的一键启动方式，并把工作区路径传给 Runner。

### 方式二：Windows 一键启动完整环境

一键脚本会依次启动：

1. 宿主机 Local Runner（`8765`）；
2. 宿主机 Playwright MCP（`8931`）；
3. Docker Compose 的 backend、frontend 和 PostgreSQL；
4. MCP 配置生成、健康检查和工具刷新。

在项目根目录执行：

~~~powershell
.\\scripts\\start-local.cmd "F:\\Dirty work"
~~~

也可以让浏览器以无头模式运行：

~~~powershell
powershell -ExecutionPolicy Bypass -File .\\scripts\\start-local.ps1 -Workspace "F:\\Dirty work" -Headless
~~~

成功时应看到类似输出：

~~~text
Browser MCP connected. Found 6 tools.
Startup complete.
  Web:      http://localhost
  Runner:   http://127.0.0.1:8765/health
  MCP:      http://127.0.0.1:8931/mcp
~~~

停止本地 Runner、Playwright MCP 和 Docker：

~~~powershell
.\\scripts\\stop-local.cmd -StopDocker
~~~

只停止宿主机 Runner 和 Playwright MCP、保留 Docker：

~~~powershell
.\\scripts\\stop-local.cmd
~~~

脚本日志位于：

~~~text
.huai-coder-runtime/logs/runner.stdout.log
.huai-coder-runtime/logs/runner.stderr.log
.huai-coder-runtime/logs/playwright-mcp.stdout.log
.huai-coder-runtime/logs/playwright-mcp.stderr.log
~~~

## 配置说明

Docker 部署使用根目录 `.env`；宿主机直接运行后端时可以使用 `backend/.env`。推荐从模板复制，不要把真实密钥提交到 Git：

~~~powershell
Copy-Item .env.example .env
Copy-Item backend\\.env.example backend\\.env
~~~

### 基础配置

| 变量 | 示例 | 作用 |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | `change-me` | PostgreSQL 密码，Docker Compose 必填 |
| `DATABASE_URL` | `postgresql+asyncpg://...` | 后端数据库连接串；Compose 会自动覆盖为容器地址 |
| `CORS_ORIGINS` | `http://localhost:5173,http://localhost` | 允许访问后端的前端来源 |
| `LLM_BASE_URL` | `https://.../v1` | LLM API 地址 |
| `LLM_API_KEY` | `...` | LLM API 密钥 |
| `LLM_MODEL` | `...` | 使用的模型名称 |
| `WORKSPACE_ROOT` | `/workspace` | 容器内允许访问的项目根目录 |

### 记忆与上下文配置

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `MEMORY_ENABLED` | `true` | 是否启用长期记忆 |
| `MEMORY_EXTRACTION_ENABLED` | `true` | 任务结束后是否自动提取记忆 |
| `MEMORY_MAX_RETRIEVED` | `8` | 每轮最多注入的记忆条数 |
| `MEMORY_DEFAULT_IMPORTANCE` | `5` | 新记忆默认重要性 |
| `MEMORY_RETENTION_DAYS` | `90` | 默认记忆保留周期 |
| `CONTEXT_COMPACTION_ENABLED` | `true` | 是否启用上下文压缩 |
| `CONTEXT_MAX_TOKENS` | `32768` | 单轮上下文预算 |
| `CONTEXT_COMPACTION_THRESHOLD` | `0.75` | 达到预算比例后触发压缩 |
| `CONTEXT_RECENT_TURNS` | `8` | 压缩后保留的最近对话轮数 |

### 工具、MCP 与子 Agent 配置

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `TOOL_APPROVAL_ENABLED` | `false` | 原生高风险工具是否需要审批 |
| `MCP_ENABLED` | `true` | 是否启用 MCP 客户端 |
| `MCP_CONFIG_PATH` | `/workspace/backend/mcp.json` | MCP 配置文件路径 |
| `MCP_CONFIG_WRITE_ENABLED` | `false` | 是否允许通过 API 写入 MCP 配置 |
| `MCP_APPROVAL_ENABLED` | `true` | MCP 高风险工具是否需要审批 |
| `MCP_TOOL_TIMEOUT_SECONDS` | `120` | MCP 工具调用超时时间 |
| `SUBAGENT_MAX_PARALLEL` | `4` | 子 Agent 最大并发数 |
| `SUBAGENT_MAX_PER_RUN` | `4` | 单次运行最多创建的子 Agent 数 |
| `SUBAGENT_QUEUE_TIMEOUT_SECONDS` | `5` | 子 Agent 排队超时 |

## 工作区与本地 Runner

### 为什么需要 Runner

Docker 容器只能直接看到容器内的 `/workspace`。如果用户在 Windows 上选择 `F:\\Dirty work` 作为项目工作区，容器中的 Agent 无法仅凭浏览器选择结果访问该宿主机目录，因此需要一个运行在宿主机上的 Local Runner：

~~~text
Agent -> backend container -> Local Runner -> F:\\Dirty work
~~~

Runner 负责在绑定工作区内执行受控命令、准备项目依赖并返回结构化结果。它不是第二个 Agent，推理和工具选择仍由 backend 完成。

### 手动启动 Runner

在项目根目录打开 PowerShell：

~~~powershell
cd backend
python -m app.runner_server --workspace "F:\\Dirty work" --host 127.0.0.1 --port 8765
~~~

其中 `--workspace` 推荐使用绝对路径。Runner 关闭后不会删除工作区；只要工作区路径不变，Docker 重启一般不需要重新下载 Runner 依赖。Playwright MCP 的 npm 缓存保存在 `.huai-coder-runtime/npm-cache`。

检查 Runner：

~~~powershell
Invoke-RestMethod http://127.0.0.1:8765/health
~~~

### 依赖自动准备

Runner 会根据工作区中的项目文件识别常见技术栈，并在需要时准备依赖，例如：

- Python：`pyproject.toml`、`requirements.txt`；
- Node.js：`package.json` 和 lock 文件；
- Java：`pom.xml`、`build.gradle`；
- Go：`go.mod`；
- Rust：`Cargo.toml`；
- Ruby：`Gemfile`；
- PHP：`composer.json`。

依赖准备、命令执行、重试次数和安全拒绝结果都会返回给 Agent，Agent 可以基于错误重新规划，而不是把失败当成最终答案。

## 浏览器 MCP

浏览器能力由 Playwright MCP 提供，Huai-Coder 只负责连接、发现工具、编排调用和展示过程。

### 运行 Playwright MCP

推荐使用一键脚本。需要手动启动时，在宿主机执行：

~~~powershell
npx.cmd -y @playwright/mcp@latest --port 8931 --host 0.0.0.0 --allowed-hosts "*" --isolated
~~~

Docker 容器通过以下地址访问宿主机服务：

~~~text
http://host.docker.internal:8931/sse
~~~

Windows 本地直接运行时可以使用 stdio 配置。模板位于 [`backend/mcp.example.json`](backend/mcp.example.json)。一键脚本会生成或更新 `backend/mcp.json`，并启用 `playwright-host` 配置。

### 浏览器工具

当前浏览器 MCP 允许的核心工具包括：

- `browser_navigate`：打开页面；
- `browser_tabs`：查看标签页；
- `browser_snapshot`：读取页面可交互结构；
- `browser_click`：点击元素；
- `browser_type`：输入文字；
- `browser_wait_for`：等待页面、元素或状态变化。

典型流程是：打开页面 → 获取快照 → 根据快照定位元素 → 点击或输入 → 等待结果 → 再次读取快照。不要只依赖固定坐标，优先使用页面快照中的可访问名称、角色和文本。

### 连接检查

1. 确认宿主机 `8931` 端口正在监听；
2. 确认 Playwright MCP 的 `--host 0.0.0.0` 和 `--allowed-hosts "*"` 参数正确；
3. 在 Huai-Coder 页面点击“MCP 工具”中的“刷新”；
4. `playwright-host` 状态应为 `ready`，并显示工具数量；
5. 如果状态为 `failed`，先查看 `.huai-coder-runtime/logs/playwright-mcp.stderr.log`。

## 记忆与上下文压缩

### 记忆分层

长期记忆按作用域隔离：

~~~text
用户记忆（user）
└─ 对所有项目和会话有效的偏好、习惯和约束

项目记忆（project）
└─ 当前项目的架构事实、技术约定、决策和常见命令

会话记忆（session）
└─ 当前会话中的临时目标、已完成步骤和未决事项
~~~

新任务开始时，系统按当前用户、项目和会话检索相关记忆；任务结束后根据重要性、复用价值和作用域写入长期记忆。前端可以查看当前会话、对应项目和用户范围的记忆概览。

### 上下文压缩策略

当估算 token 使用量达到 `CONTEXT_COMPACTION_THRESHOLD * CONTEXT_MAX_TOKENS` 时，Context Manager 会：

1. 保留系统指令、当前任务、最近若干轮对话和未完成计划；
2. 把较早的消息、工具结果和重复信息归纳为摘要；
3. 保留错误、决策、文件变更、测试结果等高价值事实；
4. 丢弃已消费且不会影响后续决策的冗余输出；
5. 用压缩后的上下文继续 ReAct 循环。

这是一种预算控制，不是简单截断字符串。可以通过 `CONTEXT_MAX_TOKENS`、`CONTEXT_COMPACTION_THRESHOLD` 和 `CONTEXT_RECENT_TURNS` 调整取舍。

更多设计说明见 [`docs/memory-and-context-design.md`](docs/memory-and-context-design.md)。

## 子 Agent

复杂任务可以拆分给不同职责的子 Agent：

| 子 Agent | 适合工作 |
| --- | --- |
| Explorer | 浏览目录、定位入口、查找相关代码和配置 |
| Planner | 分析需求、拆分步骤、识别依赖和风险 |
| Coder | 编写或修改代码、遵循已有项目约定 |
| Tester | 运行测试、复现问题、验证修复结果 |

主 Agent 负责分派、合并结果和最终决策。`SUBAGENT_MAX_PARALLEL`、`SUBAGENT_MAX_PER_RUN` 与 `SUBAGENT_QUEUE_TIMEOUT_SECONDS` 用于控制资源消耗和并发风险。

## HTTP API

后端默认地址为 `http://127.0.0.1:8000`。常用接口如下，完整实现位于 [`backend/app/main.py`](backend/app/main.py)。

### 项目与会话

~~~text
GET    /api/projects
POST   /api/projects
DELETE /api/projects/{project_id}
GET    /api/projects/{project_id}/workspace
GET    /api/projects/{project_id}/sessions
POST   /api/sessions
GET    /api/sessions/{session_id}/messages
~~~

### 运行、计划与审批

~~~text
POST /api/runs
GET  /api/runs/{run_id}/events
GET  /api/runs/{run_id}/approvals
POST /api/runs/{run_id}/tool-results
POST /api/runs/{run_id}/cancel
GET  /api/plans/{plan_id}
POST /api/plans/{plan_id}/confirm
POST /api/plans/{plan_id}/pause
POST /api/plans/{plan_id}/resume
POST /api/tasks/{task_id}/retry
POST /api/approvals/{approval_id}/approve
POST /api/approvals/{approval_id}/reject
~~~

### 记忆与上下文

~~~text
GET  /api/projects/{project_id}/memories
GET  /api/projects/{project_id}/memories/overview
GET  /api/sessions/{session_id}/memories/overview
POST /api/memories
PATCH /api/memories/{memory_id}
DELETE /api/memories/{memory_id}
GET  /api/sessions/{session_id}/summary
POST /api/sessions/{session_id}/compact
~~~

### MCP 与浏览器

~~~text
GET  /api/mcp/servers
POST /api/mcp/servers
POST /api/mcp/refresh
GET  /api/mcp/tools
POST /api/mcp/servers/{server_id}/connect
POST /api/mcp/servers/{server_id}/reconnect
GET  /api/browser/sessions
POST /api/browser/sessions
POST /api/browser/sessions/{session_id}/stop
POST /api/browser/sessions/{session_id}/reset
~~~

健康检查：

~~~text
GET /health
~~~

## 测试与质量检查

在后端目录创建或激活 Python 环境后运行：

~~~powershell
cd backend
python -m pytest -q
~~~

按能力运行专项测试：

~~~powershell
python -m pytest -q tests/test_memory_context.py
python -m pytest -q tests/test_mcp_client.py
python -m pytest -q tests/test_local_runner.py
python -m pytest -q tests/test_subagents.py
~~~

检查 Docker Compose 配置：

~~~powershell
docker compose config --quiet
~~~

检查文档和工作区变更：

~~~powershell
git diff --check
git status --short
~~~

如果只修改 README，通常不需要重建镜像；如果修改 backend/frontend 代码，建议重新执行：

~~~powershell
docker compose up -d --build --force-recreate
~~~

## 故障排查

### 页面可以打开，但工具一直是 `failed`

检查下面三项：

1. `http://127.0.0.1:8931/mcp` 是否能从宿主机访问；
2. Playwright MCP 是否使用 `--host 0.0.0.0`；
3. Docker 配置中的地址是否为 `host.docker.internal:8931`，而不是容器内的 `localhost:8931`。

然后重新点击页面中的 MCP 刷新按钮。

### `Access is only allowed at localhost:8931`

这通常表示 MCP 服务只允许本机 Host。重新启动时加上：

~~~powershell
--host 0.0.0.0 --allowed-hosts "*"
~~~

### Runner 健康检查失败

查看：

~~~text
.huai-coder-runtime/logs/runner.stderr.log
~~~

确认：

- `python` 可在当前 PowerShell 中执行；
- `backend` 目录是当前目录；
- `--workspace` 指向存在的文件夹；
- `8765` 没有被旧进程占用；
- Docker Desktop 正在运行。

也可以手动验证：

~~~powershell
Invoke-RestMethod http://127.0.0.1:8765/health
~~~

### 文件没有保存到绑定目录

确认前端选择的项目目录和 Runner 的 `--workspace` 是同一个绝对路径。Docker 容器中的 `/workspace` 是 Huai-Coder 源码挂载点，不等于用户任意选择的 Windows 目录；宿主机项目写入必须经过 Runner。

### 端口被占用

默认端口如下：

| 端口 | 服务 |
| --- | --- |
| `80` | frontend |
| `8000` | backend |
| `5432` | PostgreSQL 容器内部端口 |
| `8765` | Local Runner |
| `8931` | Playwright MCP |

优先执行：

~~~powershell
.\\scripts\\stop-local.cmd -StopDocker
~~~

如果仍有旧进程，确认进程后再结束它；不要在不确认 PID 的情况下批量终止系统进程。

### 审批后工具仍显示进行中

刷新会话页面并检查运行事件、审批状态和 Runner 日志。工具调用需要同时完成“审批确认 → 后端继续执行 → 工具结果回传 → Agent 消费结果”四个阶段，单纯点击批准并不代表宿主机命令已经返回。

## 安全边界

- 只允许工具访问当前配置的工作区；
- 路径规范化后再做越界检查，避免通过 `..` 逃逸；
- 高风险命令、写文件、浏览器点击、远程仓库写入可以进入审批流程；
- MCP 工具允许列表和审批策略由 `mcp.json` 控制；
- GitHub Token、LLM API Key 等密钥只放在 `.env` 或本地配置中；
- 不要把 `.env`、浏览器用户数据目录、数据库数据卷和运行日志提交到仓库；
- 使用真实项目之前，建议先绑定一个可恢复的测试目录。

## 开发与扩展

### 增加原生工具

1. 在 `backend/app/tools.py` 实现工具逻辑；
2. 在 `backend/app/registry.py` 注册工具定义；
3. 在 `backend/app/security.py` 增加路径、参数或审批约束；
4. 在 `backend/tests/` 增加成功、失败和越界测试；
5. 在前端补充工具状态和结果展示；
6. 运行完整测试和 Docker Compose 配置检查。

### 增加 MCP Server

1. 在 `backend/mcp.example.json` 增加服务模板；
2. 配置 `transport`、连接地址或启动命令；
3. 设置 `allowedTools`，只暴露业务需要的工具；
4. 对写入、删除、远程操作设置 `approval`；
5. 通过 `/api/mcp/refresh` 发现工具并验证状态；
6. 为连接失败、超时、工具错误增加测试。

### 增加新的记忆类型

先明确记忆的作用域、生命周期、重要性和检索条件，再修改模型、提取器、检索器、API 和前端展示，避免把临时执行日志误写成长久事实。

## 相关文档

- [本地一键启动](docs/local-start.md)
- [Local Runner 与依赖自动准备](docs/local-runner.md)
- [记忆与上下文压缩设计](docs/memory-and-context-design.md)
- [MCP 浏览器接入说明](docs/mcp-browser-integration-change.md)
- [子 Agent 架构](docs/sub-agent-architecture.md)
- [Agent Web 实施阶段记录](docs/agent-web/phases.md)

## License

当前仓库未在 README 中声明开源许可证。若要对外发布，请在根目录补充 `LICENSE` 文件，并在这里明确许可证名称和第三方依赖的授权信息。
