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
CONTEXT_COMPACTION_THRESHOLD=0.8
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

GET    /api/projects/{project_id}/memories
GET    /api/projects/{project_id}/memories/overview
GET    /api/sessions/{session_id}/memories/overview
POST   /api/memories
PATCH  /api/memories/{memory_id}
DELETE /api/memories/{memory_id}
POST   /api/sessions/{session_id}/compact

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
npm.cmd run build
```

后端测试和 Docker 验证：

```powershell
docker compose up -d --build backend frontend
docker compose exec -T backend python -m compileall -q /app/app /app/migrations
docker compose exec -T backend pytest -q /workspace/backend/tests
Invoke-RestMethod http://localhost:8000/health
```

## 相关文档

- [长期记忆与上下文压缩设计](docs/memory-and-context-design.md)
- [Sub-Agent 架构重构说明](docs/sub-agent-architecture.md)

## 开发约定

- 每个阶段使用独立分支开发。
- 修改后运行前端构建、后端测试和 Docker 验证。
- 阶段完成后提交 Pull Request。
- 不提交真实密钥、密码、Token、Cookie 或私钥。
