# 阶段七：MCP、Multi-Agent 与长期记忆

## 目标

在单 Agent、工具安全、Plan 和 RAG 稳定后，增加外部工具、子 Agent 和长期记忆能力。

## 后端任务

1. 接入 MCP Python SDK。
2. 支持 Stdio 和 Streamable HTTP。
3. 将 MCP 工具转换为内部 Tool Registry 定义。
4. 为 MCP 工具应用同样的权限和审计策略。
5. 使用 LangGraph Subgraph 实现 SubAgent。
6. 增加并行任务数量和资源限制。
7. 实现会话记忆和长期记忆。
8. 增加记忆查询、删除和审计。

## 前端任务

1. MCP Server 管理页面。
2. 工具列表和连接状态页面。
3. SubAgent 运行拓扑和任务详情。
4. 长期记忆管理页面。

## 验收

- MCP 工具可以动态发现和调用。
- MCP 工具不能绕过本地安全层。
- SubAgent 有独立上下文和权限边界。
- 记忆可以查询、修改和删除。
