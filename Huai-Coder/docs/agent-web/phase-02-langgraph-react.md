# 阶段二：LangGraph ReAct Agent

## 目标

接入 OpenAI-compatible 模型，使用 LangGraph 实现 ReAct Agent，并通过 SSE 将 Agent 运行过程推送到前端。

## 技术设计

```text
用户消息
  ↓
FastAPI 创建 Agent Run
  ↓
LangGraph ReAct Graph
  ↓
LLM / Tool
  ↓
PostgreSQL Checkpointer
  ↓
AgentEvent
  ↓
SSE
  ↓
React ChatTimeline
```

## 后端任务

1. 定义 `AgentState`。
2. 实现 OpenAI-compatible Chat Model。
3. 实现 `read_file`、`list_dir`、`grep_code`。
4. 实现 LangGraph ReAct Graph。
5. 定义 `AgentEvent` 和事件持久化。
6. 创建运行、事件和消息表。
7. 接入 PostgreSQL Checkpointer。
8. 实现 SSE 事件接口。

## 前端任务

1. 创建 SSE Hook。
2. 实现消息增量渲染。
3. 实现 Tool Call 卡片。
4. 实现运行状态和连接状态。
5. 实现运行结束、失败和重连提示。

## 验收

- Agent 能读取项目文件并回答问题。
- 前端实时显示 Agent 内容和工具调用。
- 工具调用参数和结果可查询。
- 服务重启后可以查看历史运行记录。
