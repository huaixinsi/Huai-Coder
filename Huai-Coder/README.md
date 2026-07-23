# Huai-Coder

Huai-Coder 是一个面向真实代码仓库的项目级 Web Agent。它可以读取项目上下文、生成执行计划、调用受控工具完成任务，并在多轮对话中保留长期记忆和会话摘要。

## 核心能力

| 能力 | 说明 |
| --- | --- |
| 项目工作区 | 为每个项目隔离文件和会话，支持绑定/切换完整文件夹；Agent 的代码改动由浏览器直接写入绑定的本地文件夹，不写回 Docker 工作区。 |
| Plan-and-Execute | 先生成结构化计划，用户确认后再按依赖执行，支持暂停、继续、取消和基础重试。 |
| 长期记忆 | 保存可复用的项目事实、技术决策、用户偏好、约束和待办事项，并支持检索、更新、删除和审计。 |
| 上下文压缩 | 接近模型上下文上限时保留系统规则、当前任务、记忆、会话摘要和最近对话；不再设置 Agent 的累计 Token 熔断。 |
| ReAct Agent | 根据模型决策循环执行 `list_dir`、`read_file`、`grep_code`、`write_file`、`execute_command` 等工具。 |
| 子 Agent | 提供 explorer、planner、coder、tester 四类受限子 Agent，每类 Agent 只有明确的工具白名单。 |
| 安全审批 | 写文件、执行命令和敏感配置访问等高风险操作支持可配置人工确认，并保留审计事件。 |
| 可追踪执行过程 | 前端聚合显示每次工具调用，展开后可以查看参数、结果和上下文压缩记录。 |
| 重复调用防护 | 标准化参数、有效结果和工作区均无变化时，连续第 3 次触发重新规划，第 4 次拒绝，第 5 次熔断该工具与参数组合。 |
| MCP 扩展 | 支持 stdio、Streamable HTTP 和兼容旧版 SSE 的 MCP Server；工具按 `mcp__server__tool` 命名空间注入 ReAct 循环。 |

## Docker 启动

在仓库根目录执行：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`，至少配置数据库密码和一个兼容 OpenAI Chat Completions API 的模型服务：

```env
POSTGRES_PASSWORD=your-local-password
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=your-api-key
LLM_MODEL=deepseek-chat
```

启动服务：

```powershell
docker compose up -d --build
```

服务地址：

- Web 首页：http://localhost
- API：http://localhost:8000
- 健康检查：http://localhost:8000/health

停止服务：

```powershell
docker compose down
```

## 一键启动完整环境

Windows 用户可以直接双击 `scripts/start-local.cmd`，或在项目根目录执行：

```powershell
.\scripts\start-local.cmd "F:\Dirty work"
```

脚本会自动启动 Runner、Playwright MCP、Docker，并等待 MCP 工具刷新完成。停止宿主机服务使用 `scripts/stop-local.cmd`；详细说明见 [docs/local-start.md](docs/local-start.md)。

## 使用流程

1. 打开 Web 首页并创建或选择一个项目。
2. 创建或选择会话，先绑定对应的完整项目目录；切换项目目录时点击“绑定/切换文件夹”。建议使用最新版 Chrome/Edge，以获得读写目录权限。
3. 确认当前会话顶部显示的工作区后再输入任务。未绑定文件夹时发送按钮会被阻止并提示绑定。
4. 检查计划和任务依赖，点击“确认计划”。
5. Agent 可通过 `write_file` 创建或修改代码；后端只发送 `file.write` 文件事件，浏览器使用目录读写权限直接写入已绑定的本地文件夹。Docker 中仅保留用于读取和上下文分析的文件快照。
6. 必须使用最新版 Chrome/Edge 的“绑定/切换文件夹”授予读写权限；普通“选择文件”上传无法获得写回权限。
7. 高风险审批由 `TOOL_APPROVAL_ENABLED` 控制，当前默认关闭；本地工作区模式下代码写入不会进入 Docker 审批执行路径。
8. 在“执行过程”面板中展开任意工具调用，查看具体参数、返回结果和重复调用防护事件。
9. 在项目面板中维护长期记忆，或点击“压缩会话”生成可追溯的会话摘要。

## MCP 接入与浏览器/GitHub 自动化

MCP 是可插拔的外部工具协议。Huai-Coder 会在每轮 Agent 执行前发现已启用 Server 的工具，把工具 Schema 注入现有 ReAct 循环；调用结果会作为 observation 返回模型，模型可以继续判断、修正参数、等待页面变化或结束任务。

复制示例配置并按部署方式选择一个浏览器入口：

```powershell
Copy-Item backend/mcp.example.json backend/mcp.json
```

宿主机直接运行后端时，可启用 `playwright`（stdio）：

```json
{
  "mcpServers": {
    "playwright": {
      "enabled": true,
      "transport": "stdio",
      "command": "npx.cmd",
      "args": ["-y", "@playwright/mcp@latest", "--isolated", "--headless"]
    }
  }
}
```

Docker 后端不能直接执行 Windows 宿主机的 `npx.cmd`。此时在宿主机启动 Playwright MCP 的 SSE 端口：

```powershell
npx.cmd -y @playwright/mcp@latest --port 8931 --host 0.0.0.0 --allowed-hosts "*" --isolated --headless
```

然后启用示例中的 `playwright-host`，它连接 `http://host.docker.internal:8931/sse`。`docker-compose.yml` 已为 backend 添加 `host.docker.internal` 网关映射。只允许本机使用时可以把 `--host` 改成 `127.0.0.1`，但此时 Docker 容器通常无法访问宿主机服务。

如果聊天里仍然回复“无法操作浏览器”，先检查 MCP 面板是否出现 `playwright-host · ready` 和浏览器工具列表。Docker 默认读取绑定目录中的 `/workspace/backend/mcp.json`；请先复制示例配置、启用 `playwright-host`，再重建服务：

```powershell
Copy-Item backend/mcp.example.json backend/mcp.json
# 将 backend/mcp.json 中 playwright-host.enabled 改为 true
docker compose up -d --build
```

只有当 MCP 面板显示 `ready` 且发现 `browser_navigate`、`browser_snapshot`、`browser_click` 等工具时，Agent 才会实际调用浏览器；未连接时会明确提示“浏览器 MCP 未连接”，不会把配置问题误报成项目不支持浏览器。

GitHub MCP 默认关闭。启用前在宿主机环境或 Docker Compose 环境中提供 `GITHUB_PERSONAL_ACCESS_TOKEN`，并建议只开放读取工具；创建 PR、写 Issue、发布等外部副作用工具仍会单独进入 MCP 审批流程：

```powershell
$env:GITHUB_PERSONAL_ACCESS_TOKEN = "仅在当前终端临时设置，不要提交到仓库"
```

子 Agent 运行资源默认受以下参数限制：`SUBAGENT_MAX_PARALLEL=4` 控制进程级并发数，`SUBAGENT_MAX_PER_RUN=4` 控制单个 Run 的并发配额，`SUBAGENT_QUEUE_TIMEOUT_SECONDS=5` 控制等待资源的最长时间。达到配额时会返回明确的 `SUBAGENT_RESOURCE_LIMIT` 结果，不会无限排队。

MCP 配置管理 API 默认是只读的。要通过 API 新增、修改或删除 Server，必须显式设置 `MCP_CONFIG_WRITE_ENABLED=true`；即使打开写入，API 也只接受 `${NAME}` 环境变量占位符，不会把明文 Token 写入配置文件。更推荐直接编辑本地 `backend/mcp.json`，并让敏感值留在宿主机环境变量中。

后端依赖 `mcp` Python SDK；在 Server 配置中设置 `"client": "auto"` 或 `"client": "python_sdk"` 才启用 SDK。默认使用已经过 Windows/Docker 双环境验证的内置 JSON-RPC 传输适配器，避免宿主机事件循环差异导致 stdio 卡住。两种路径共用同一套工具白名单、风险审批、超时、重试和审计逻辑。

MCP 管理接口：

```text
GET  /api/mcp/servers       # 查看配置、连接状态和工具数量，不返回密钥
POST /api/mcp/refresh       # 连接/刷新所有已启用 Server 并重新发现工具
GET  /api/mcp/tools         # 查看已发现的命名空间工具、风险级别和审批要求
POST /api/mcp/servers/{id}/connect
POST /api/mcp/servers/{id}/disconnect
POST /api/mcp/servers/{id}/reconnect
GET  /api/mcp/servers/{id}/tools
POST /api/mcp/servers
PATCH /api/mcp/servers/{id}
DELETE /api/mcp/servers/{id}
GET  /api/mcp/approvals/{approval_id}
POST /api/mcp/approvals/{approval_id}/approve
POST /api/mcp/approvals/{approval_id}/reject
GET  /api/browser/sessions
POST /api/browser/sessions
POST /api/browser/sessions/{session_id}/stop
POST /api/browser/sessions/{session_id}/reset
POST /api/runs/{run_id}/cancel
```

风险规则分三层：读取/快照/等待为低风险；普通外部操作为中风险；点击提交、创建 PR、写 Issue、删除、发布、发送等可能造成外部副作用的动作标记为高风险。`MCP_APPROVAL_ENABLED=true` 时，高风险 MCP 工具即使当前会话使用了本地工作区，也不会绕过外部副作用审批。

浏览器任务推荐使用“快照—操作—等待—再快照”的闭环提示词，例如：

```text
打开 https://www.selenium.dev/selenium/web/web-form.html。
先获取页面快照，使用快照中的 target/ref 填写 Text input，点击 Submit，
等待页面出现 Form submitted，再获取一次快照确认结果。若元素引用失效，
重新获取快照后再操作；不要执行删除、发布或提交订单等外部副作用操作。
```

配置中的环境变量使用 `${NAME}` 占位符解析，API 和前端状态面板只返回 `has_env`，不会返回 Token、Cookie 或完整环境变量值。

## 记忆与上下文压缩

长期记忆和会话摘要是两类不同的数据：

- 长期记忆保存跨会话仍然有价值的信息，例如项目技术栈、架构决策和编码约束。
- 会话摘要只覆盖当前会话的历史消息，用于减少下一次请求的上下文长度。
- 原始消息和工具事件仍然持久化，摘要只是构建 Prompt 时使用的派生数据。
- 密码、Token、API Key、Cookie、私钥和敏感环境变量不会保存为长期记忆。

默认配置可以在 `.env` 或 `backend/.env` 中调整：

```env
MEMORY_ENABLED=true
MEMORY_EXTRACTION_ENABLED=true
MEMORY_MAX_RETRIEVED=8
MEMORY_RETENTION_DAYS=90
CONTEXT_COMPACTION_ENABLED=true
CONTEXT_MAX_TOKENS=32768
TOOL_APPROVAL_ENABLED=false
CONTEXT_COMPACTION_THRESHOLD=0.75
CONTEXT_RECENT_TURNS=8
```

`CONTEXT_MAX_TOKENS` 仅用于上下文压缩，不作为整轮 Agent 的累计调用预算；Agent 的执行熔断以工具重复调用规则为准。当前版本使用保守的 Token 估算和关键词相关性排序，不要求额外部署 Redis 或向量数据库。

重复调用策略按工具注册项配置：普通工具使用 `guarded`，会改变工作区的工具使用 `stateful` 并比较工作区内容，轮询工具使用 `polling` 豁免普通重复调用规则。

## 安全模型

- 所有文件访问都限制在当前项目工作区内。
- 拒绝路径穿越、绝对路径和工作区外路径。
- 敏感文件和凭证内容默认不回显。
- 子 Agent 通过工具白名单和运行时校验进行双重限制。
- 高风险工具需要人工审批。
- Agent Run、工具事件、审批和记忆变更都保留审计记录。

## 主要 API

```text
GET    /api/projects
POST   /api/projects
GET    /api/projects/{project_id}/sessions
GET    /api/projects/{project_id}/workspace
POST   /api/projects/{project_id}/files
POST   /api/sessions
POST   /api/runs
POST   /api/runs/{run_id}/cancel

GET    /api/mcp/servers
POST   /api/mcp/refresh
GET    /api/mcp/tools
POST   /api/mcp/servers/{server_id}/connect
POST   /api/mcp/servers/{server_id}/disconnect

GET    /api/projects/{project_id}/memories
GET    /api/projects/{project_id}/memories/overview
GET    /api/sessions/{session_id}/memories/overview
GET    /api/projects/{project_id}/memories/audit?include_session=true
POST   /api/memories
PATCH  /api/memories/{memory_id}
DELETE /api/memories/{memory_id}
GET    /api/memories/{memory_id}/audit
POST   /api/sessions/{session_id}/compact

GET    /api/subagents

GET    /api/plans/{plan_id}
GET    /api/plans/{plan_id}/tasks
POST   /api/plans/{plan_id}/confirm
POST   /api/plans/{plan_id}/pause
POST   /api/plans/{plan_id}/resume
POST   /api/plans/{plan_id}/cancel
```

## 项目结构

```text
Huai-Coder/
├─ backend/
│  ├─ app/agent.py              # 主 Agent 与 ReAct 循环
│  ├─ app/agents/               # 子 Agent 配置、权限和执行器
│  ├─ app/memory.py             # 长期记忆提取、检索和生命周期
│  ├─ app/context.py            # 上下文预算、摘要和压缩
│  ├─ app/registry.py           # 工具注册、风险与审批
│  ├─ migrations/               # 数据库迁移
│  └─ tests/                    # 后端测试
├─ frontend/
│  └─ src/main.tsx              # Web 首页和对话界面
├─ docs/                        # 架构与设计文档
├─ docker-compose.yml
└─ README.md
```

## 本地验证

前端构建：

```powershell
cd frontend
npm.cmd run typecheck
npm.cmd run build
```

后端测试和 Docker 验证：

```powershell
docker compose up -d --build backend frontend
docker compose exec -T backend python -m compileall -q /app/app /app/migrations
docker compose exec -T backend pytest -q /workspace/backend/tests
Invoke-RestMethod http://localhost:8000/health
```

MCP 现场验收（只调用低风险读取工具，不会创建 PR、Issue 或修改远程数据）：

```powershell
docker compose exec -T backend python -m app.mcp_smoke --config /workspace/backend/mcp.json --github
docker compose exec -T backend python -m app.mcp_smoke --config /workspace/backend/mcp.json --browser
```

脚本会输出 Server 状态、发现到的命名空间工具、风险等级和读取结果；如果没有发现目标 Server 或读取失败，会以非零退出码结束，适合放进部署验收流程。

## 相关文档

- [长期记忆与上下文压缩设计](docs/memory-and-context-design.md)
- [Sub-Agent 架构重构说明](docs/sub-agent-architecture.md)

## 开发约定

- 每个阶段使用独立分支开发。
- 修改后运行前端构建、后端测试和 Docker 验证。
- 阶段完成后提交 Pull Request。
- 不提交真实密钥、密码、Token、Cookie 或私钥。
- [Local Runner 自动依赖安装与本地执行](docs/local-runner.md)
- [MCP、浏览器交互与 GitHub 扩展详细变更](docs/mcp-browser-integration-change.md)
## GitHub MCP 的 Docker 部署方式

示例配置中的 GitHub Server 使用 GitHub 官方 Remote MCP 的 Streamable HTTP 地址，不要求 backend 容器内部执行 `docker run`，因此不会依赖 Docker Socket。Token 只通过 Header 的环境变量占位符传入：

```json
{
  "enabled": true,
  "transport": "streamable_http",
  "url": "https://api.githubcopilot.com/mcp/",
  "headers": {
    "Authorization": "Bearer ${GITHUB_PERSONAL_ACCESS_TOKEN}"
  }
}
```

本地 GitHub MCP Docker/stdio 仍可作为备用配置，但应由宿主机或独立 MCP 进程启动，不应让 backend 容器嵌套调用 Docker。Remote Server 的工具仍会经过 Huai-Coder 的白名单和高风险审批。
