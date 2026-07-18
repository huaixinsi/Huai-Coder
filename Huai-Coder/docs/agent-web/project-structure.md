# Python Web Agent 项目结构

## 1. 技术约定

- 后端：Python 3.12+、FastAPI、SQLAlchemy 2.x、Alembic、asyncpg。
- 数据库：所有环境统一使用 PostgreSQL；开发、测试、生产使用不同数据库或实例。
- Agent：LangGraph 负责状态图、任务编排、流式执行、中断和恢复；LangChain 仅选择性使用模型、Prompt 和 Tool 抽象。
- 前端：React、TypeScript、Vite、Zustand、Ant Design 或 Tailwind CSS。
- 实时通信：第一阶段使用 SSE，双向控制需求增加后使用 WebSocket。
- 安全：工具注册、路径保护、命令风险分析、审批和审计由业务代码负责，不能依赖 Agent 框架默认行为。

## 2. 顶层结构

```text
Huai-Coder/
├── backend/
│   ├── app/
│   ├── migrations/
│   ├── tests/
│   ├── pyproject.toml
│   └── .env.example
├── frontend/
│   ├── src/
│   ├── public/
│   ├── package.json
│   └── vite.config.ts
├── docs/
│   ├── agent-web/
│   └── agent-tech-stack.md
├── deploy/
│   ├── docker-compose.yml
│   ├── Dockerfile.backend
│   └── Dockerfile.frontend
└── README.md
```

## 3. 后端结构

```text
backend/app/
├── main.py                         # FastAPI 入口
├── config.py                       # 环境变量和配置
├── database.py                     # PostgreSQL Engine、Session、事务
├── dependencies.py                 # API 依赖和权限注入
├── api/
│   ├── router.py                   # API 总路由
│   ├── projects.py                 # 项目和工作区
│   ├── sessions.py                 # 会话和消息
│   ├── runs.py                     # Agent 运行、取消、重试
│   ├── events.py                   # SSE 事件流
│   ├── approvals.py                # 审批操作
│   ├── plans.py                    # Plan 和 Task
│   └── files.py                    # 文件和 Diff 查询
├── models/
│   ├── project.py
│   ├── session.py
│   ├── run.py
│   ├── plan.py
│   ├── approval.py
│   ├── workspace.py
│   └── audit.py
├── schemas/
│   ├── common.py
│   ├── project.py
│   ├── session.py
│   ├── run.py
│   ├── event.py
│   └── approval.py
├── repositories/                   # 数据库访问封装
├── services/
│   ├── run_service.py
│   ├── event_service.py
│   ├── project_service.py
│   └── workspace_service.py
├── agents/
│   ├── state.py                    # LangGraph State
│   ├── events.py                   # 统一 AgentEvent
│   ├── router.py                   # ReAct/Plan/Team 路由
│   ├── react_graph.py              # ReAct Graph
│   ├── plan_graph.py               # Plan-and-Execute Graph
│   ├── team_graph.py               # Multi-Agent Graph
│   ├── nodes.py                    # 通用 Graph 节点
│   └── sub_agents.py
├── llm/
│   ├── factory.py
│   ├── openai_compatible.py
│   ├── models.py
│   └── callbacks.py
├── tools/
│   ├── registry.py                 # 自定义工具注册中心
│   ├── definitions.py              # 工具 Schema 和风险等级
│   ├── filesystem.py
│   ├── command.py
│   ├── search.py
│   ├── web.py
│   └── project.py
├── security/
│   ├── path_guard.py
│   ├── command_analyzer.py
│   ├── file_access_analyzer.py
│   ├── sensitive_paths.py
│   ├── permissions.py
│   └── audit.py
├── approval/
│   ├── policy.py
│   ├── service.py
│   └── state.py
├── plan/
│   ├── planner.py
│   ├── task_manager.py
│   ├── failure_classifier.py
│   └── validator.py
├── workspace/
│   ├── manager.py
│   ├── checkpoint.py
│   ├── diff.py
│   └── rollback.py
├── rag/
│   ├── indexer.py
│   ├── chunker.py
│   ├── parser.py
│   └── retriever.py
├── memory/
│   ├── conversation.py
│   ├── long_term.py
│   └── compactor.py
├── mcp/
│   ├── client.py
│   ├── manager.py
│   └── adapter.py
└── prompts/
    ├── base.md
    ├── react.md
    ├── plan.md
    ├── team.md
    └── approval.md
```

## 4. 前端结构

```text
frontend/src/
├── main.tsx
├── App.tsx
├── api/
│   ├── client.ts
│   ├── projects.ts
│   ├── sessions.ts
│   └── runs.ts
├── stores/
│   ├── projectStore.ts
│   ├── sessionStore.ts
│   └── runStore.ts
├── hooks/
│   ├── useSseEvents.ts
│   ├── useRunControl.ts
│   └── useApproval.ts
├── pages/
│   ├── WorkspacePage.tsx
│   ├── SessionPage.tsx
│   ├── SettingsPage.tsx
│   └── NotFoundPage.tsx
├── components/
│   ├── layout/
│   ├── chat/
│   ├── agent/
│   ├── plan/
│   ├── approval/
│   ├── diff/
│   └── common/
├── types/
│   ├── project.ts
│   ├── session.ts
│   ├── event.ts
│   └── task.ts
└── styles/
```

## 5. PostgreSQL 约定

```text
huai_coder_dev       # 本地开发
huai_coder_test      # 自动化测试
huai_coder_prod      # 生产环境
```

核心表建议包括：`users`、`projects`、`sessions`、`messages`、`agent_runs`、`agent_events`、`plans`、`tasks`、`approvals`、`audit_logs`、`checkpoints`、`memories`、`rag_documents` 和 `rag_chunks`。

LangGraph Checkpointer 使用 PostgreSQL 持久化 Agent 状态；业务表仍由 SQLAlchemy/Alembic 管理，二者不能混淆。
