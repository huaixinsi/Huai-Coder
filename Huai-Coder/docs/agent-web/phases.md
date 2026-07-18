# Python Web Agent 阶段实施文档

## 阶段总览

| 阶段 | 分支建议 | 主要目标 | 交付结果 |
| --- | --- | --- | --- |
| 1 | `feature/backend-frontend-skeleton` | 前后端和 PostgreSQL 基础设施 | 可启动的 Web 项目 |
| 2 | `feature/langgraph-react-agent` | LangGraph ReAct 和 LLM 接入 | 可对话、可读代码的 Agent |
| 3 | `feature/tools-and-hitl-security` | 工具、安全和审批 | 可安全修改代码和执行命令 |
| 4 | `feature/plan-execute` | 计划和任务状态机 | 可执行多步骤任务 |
| 5 | `feature/checkpoint-diff` | Checkpoint、Diff、回滚 | 可恢复和可审查交付 |
| 6 | `feature/code-rag` | 代码索引和检索 | 跨文件代码理解 |
| 7 | `feature/mcp-and-memory` | MCP、Multi-Agent、记忆 | 扩展能力和长期协作 |

每个阶段独立开发、测试、校对和提交 PR。阶段之间只通过已稳定的接口衔接。

## 阶段一：基础设施

### 范围

- FastAPI 项目初始化。
- React + TypeScript 项目初始化。
- Docker Compose 启动 PostgreSQL。
- SQLAlchemy、Alembic 和基础表。
- 项目、会话、消息 REST API。
- 前端工作区、项目列表和聊天页面骨架。

### 验收标准

- 后端可以健康检查。
- 前端可以创建和切换项目。
- 数据写入 PostgreSQL 并能重新读取。
- 测试环境使用独立 `huai_coder_test` 数据库。

## 阶段二：LangGraph ReAct Agent

### 范围

- OpenAI-compatible LLM Client。
- LangGraph State 和 ReAct Graph。
- `read_file`、`list_dir`、`grep_code` 工具。
- AgentEvent 统一事件模型。
- SSE 实时输出。
- LangGraph PostgreSQL Checkpointer。

### 验收标准

- 用户发送问题后，Agent 能调用工具读取项目。
- 前端能实时显示消息、工具调用和最终结果。
- 服务重启后可以查询运行记录。
- Agent 状态和业务记录均持久化到 PostgreSQL。

## 阶段三：工具、安全和 HITL

### 范围

- `write_file` 和 `execute_command`。
- 自定义 Tool Registry。
- PathGuard、命令风险分析、敏感路径策略。
- LangGraph interrupt。
- 审批表、审批 API 和前端审批弹窗。
- AuditLog。

### 验收标准

- 未经策略检查不能执行工具。
- 高风险操作可以中断并等待用户审批。
- 用户批准后 Graph 可以恢复。
- 拒绝、取消和异常都有审计记录。

## 阶段四：Plan-and-Execute

### 范围

- Plan、Task、TaskDependency 数据模型。
- LangGraph Plan Graph。
- 任务状态机：PENDING、RUNNING、WAITING_APPROVAL、SUCCEEDED、FAILED、CANCELLED。
- 失败分类、有限重试和 Replan。
- 前端 PlanPanel。

### 验收标准

- 多步骤任务可以生成计划。
- 任务依赖顺序正确。
- 单个任务失败不会破坏整个运行记录。
- 用户可以取消或继续任务。

## 阶段五：Checkpoint、Diff 和回滚

### 范围

- 工作区管理。
- 每个任务执行前创建 checkpoint。
- GitPython 或受控 Git CLI。
- Diff 生成和文件变更预览。
- 当前任务回滚。
- LangGraph 状态恢复和业务 checkpoint 对齐。

### 验收标准

- 每个任务都有可查询 checkpoint。
- 失败时只回滚当前任务。
- 成功任务的变更不会被后续失败覆盖。
- 前端可以查看并确认 Diff。

## 阶段六：代码 RAG

### 范围

- 文件扫描和索引任务。
- Tree-sitter / Python AST 代码解析。
- 代码分块。
- PostgreSQL FTS 检索。
- 后续接入 pgvector 向量检索。
- 索引进度 SSE 事件。

### 验收标准

- 支持 Python、Java、JavaScript 基本代码索引。
- Agent 可以检索跨文件代码。
- 索引任务可查看进度和错误。
- 项目之间的索引数据隔离。

## 阶段七：MCP、Multi-Agent 和记忆

### 范围

- MCP Stdio 和 Streamable HTTP。
- MCP 工具适配到自定义 Tool Registry。
- LangGraph Subgraph 和 SubAgent。
- 并行任务限制和资源配额。
- 会话记忆、长期记忆和记忆管理。
- 多用户权限和项目隔离。

### 验收标准

- MCP 工具可动态发现和调用。
- 外部工具仍然经过本地安全策略。
- SubAgent 有独立上下文和权限边界。
- 长期记忆可以查询、删除和审计。

## 阶段交付规范

每个阶段的 PR 必须包含：

- 变更说明和架构影响。
- API 或数据库迁移说明。
- 后端单元测试和接口测试。
- 前端组件测试或端到端测试。
- 安全影响说明。
- 手工验证结果。
- 必要的文档更新。
