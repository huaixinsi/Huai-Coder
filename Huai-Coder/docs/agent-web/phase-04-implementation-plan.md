# 阶段四：Plan-and-Execute 计划与任务执行系统实施计划

## 1. 目标与默认策略

将一次请求对应一次 Agent 执行升级为可追踪、可暂停、可恢复的多步骤任务系统：

```text
用户请求 → Planner → Plan Validator → 用户确认
        → Task Dispatcher → 工具执行 → 验证
        → 成功 / 重试 / Replan / 取消
```

默认策略：

- Plan 生成后必须用户确认。
- 任务按依赖关系执行。
- 第一版串行执行，避免工作区冲突。
- 复用阶段三的安全工具、审批和审计机制。
- 每个 Task 独立记录输入、输出、状态和日志。

## 2. 数据模型

### Plan

字段：

```text
id, project_id, session_id, run_id
goal, summary, status, version
created_at, updated_at, confirmed_at, cancelled_at
```

状态：

```text
DRAFT / WAITING_CONFIRMATION / READY / RUNNING
PAUSED / SUCCEEDED / FAILED / CANCELLED / REPLANNING
```

### Task

字段：

```text
id, plan_id, task_key, title, description, task_type
status, input_data, output_data
error_type, error_message
retry_count, max_retries
started_at, finished_at
```

状态：

```text
PENDING / BLOCKED / READY / RUNNING / WAITING_APPROVAL
RETRYING / SUCCEEDED / FAILED / CANCELLED / SKIPPED
```

### TaskDependency

字段：

```text
id, plan_id, task_id, depends_on_task_id, dependency_type
```

第一版只支持完成依赖：依赖任务成功后，下游任务才可执行。

阶段三的 `Approval` 和 `AuditLog` 增加可选的 `plan_id`、`task_id`，用于追踪任务级审批和审计。

## 3. Planner 与计划校验

Planner 只输出结构化 JSON，不直接执行工具：

```json
{
  "goal": "完成用户目标",
  "summary": "计划摘要",
  "tasks": [
    {
      "task_key": "inspect_project",
      "title": "检查项目结构",
      "description": "读取关键目录和配置",
      "task_type": "inspect",
      "depends_on": [],
      "risk_level": "low",
      "success_criteria": "得到项目结构和主要技术栈"
    }
  ]
}
```

限制：

- 最多 20 个任务。
- `task_key` 必须唯一。
- 依赖只能引用当前计划任务。
- 禁止循环依赖。
- 任务类型只能来自受支持列表。
- 禁止在计划中写入密钥、密码或 Token。

Plan Validator 检查 JSON、字段、依赖、循环、工具名称、风险等级、敏感信息和路径越权。

校验失败时允许 Planner 自动修正一次，二次失败则 Plan 标记为 `FAILED`。

## 4. 计划确认与 API

计划生成后：

1. 保存 Plan、Task 和依赖关系。
2. 推送 `plan.created`。
3. 推送 `plan.confirmation_required`。
4. Plan 进入 `WAITING_CONFIRMATION`。
5. 用户确认后进入执行。

新增接口：

```text
POST /api/runs/{run_id}/plan
GET  /api/plans/{plan_id}
GET  /api/plans/{plan_id}/tasks
POST /api/plans/{plan_id}/confirm
POST /api/plans/{plan_id}/pause
POST /api/plans/{plan_id}/resume
POST /api/plans/{plan_id}/cancel
POST /api/plans/{plan_id}/replan
POST /api/tasks/{task_id}/retry
GET  /api/tasks/{task_id}
GET  /api/tasks/{task_id}/events
GET  /api/plans/{plan_id}/audit-events
```

所有接口必须验证 Plan、Task、Session 和 Project 的归属，禁止跨项目访问。

## 5. Task Dispatcher

调度器每次选择一个满足以下条件的 Task：

- 状态为 `PENDING` 或 `READY`。
- 所有依赖任务为 `SUCCEEDED`。
- Plan 状态为 `RUNNING`。
- 未超过最大重试次数。
- 未被取消。

调度结果：

- 所有任务成功：Plan 为 `SUCCEEDED`。
- 不可重试任务失败：Plan 为 `FAILED`。
- 依赖失败：下游任务为 `SKIPPED`。
- 等待审批：Plan 为 `PAUSED`。

第一版严格串行，一次只运行一个 Task。

## 6. 任务执行与阶段三安全层整合

每个 Task 的执行上下文包括：

- 当前项目工作区。
- Plan 目标和 Task 描述。
- 依赖任务结果摘要。
- 当前会话历史。
- 成功标准和风险等级。

所有工具必须经过：

```text
Tool Registry → PathGuard → Risk Analyzer → Approval → AuditLog
```

可使用的工具：

- `list_dir`
- `read_file`
- `grep_code`
- `write_file`
- `execute_command`

任务需要审批时：

1. Task 进入 `WAITING_APPROVAL`。
2. Plan 进入 `PAUSED`。
3. 创建关联 Approval。
4. 推送 `task.approval_required`。
5. 批准后恢复当前 Task。
6. 拒绝或取消后按失败/取消策略结束。

## 7. 任务事件与前端 PlanPanel

SSE 事件：

```text
plan.created
plan.confirmation_required
plan.started
task.created
task.ready
task.started
task.tool_started
task.approval_required
task.tool_finished
task.validation_started
task.succeeded
task.failed
task.retrying
plan.paused
plan.replanning
plan.succeeded
plan.failed
plan.cancelled
```

事件必须包含：

```text
plan_id, task_id, run_id, type, content, status, tool, created_at
```

PlanPanel 展示：

- Plan 目标、摘要和状态。
- Task 列表、状态、依赖和风险等级。
- 当前任务、重试次数、错误和工具日志。
- 审批状态。

交互：

- 确认计划。
- 暂停、继续和取消。
- 重试失败任务。
- 重新规划。
- 查看任务详情和执行日志。

刷新页面后重新加载当前会话的活动 Plan，不重复执行已完成任务。

## 8. 失败、重试与 Replan

统一错误类型：

```text
VALIDATION_ERROR
PATH_SECURITY_ERROR
APPROVAL_REJECTED
COMMAND_TIMEOUT
TOOL_ERROR
LLM_ERROR
DEPENDENCY_BLOCKED
OUTPUT_INVALID
UNKNOWN_ERROR
```

每个 Task 默认最多自动重试 2 次，仅重试网络、LLM、临时工具和命令超时错误。

不自动重试：

- 路径越界。
- 审批拒绝。
- 参数错误。
- 敏感路径违规。
- 未知工具。
- 权限错误。

Replan 流程：

1. 保留原 Plan 和已完成任务。
2. 原 Plan 标记为 `REPLANNING`。
3. Planner 接收原始目标、成功任务、失败原因和当前工作区状态。
4. 生成新版本 Plan。
5. 用户重新确认。
6. 从未完成任务继续执行。

已成功任务默认不重复执行。

## 9. 暂停、继续和取消

### Pause

- 当前工具允许完成。
- 不启动下一个 Task。
- Plan 状态变为 `PAUSED`。

### Resume

- 仅允许从 `PAUSED` 恢复。
- 从第一个可执行任务继续。
- 已成功任务不重复执行。

### Cancel

- 未开始任务全部取消。
- 等待审批的 Approval 标记为取消。
- Plan 标记为 `CANCELLED`。
- 写入审计日志。

### Retry

- 仅允许重试 `FAILED` Task。
- 保留已成功依赖。
- 超过最大重试次数时拒绝操作。

## 10. 数据库兼容

新增迁移：

- `plans`
- `tasks`
- `task_dependencies`

扩展：

- `agent_runs.plan_id`
- `approvals.plan_id`
- `approvals.task_id`
- `audit_logs.plan_id`
- `audit_logs.task_id`

兼容规则：

- 没有关联 Plan 的旧 Run 继续使用阶段三单步流程。
- 新请求默认进入 Plan-and-Execute。
- 旧 Checkpoint 不强制迁移。
- 现有项目、会话、消息、审批和审计数据保留。

## 11. 测试与验收

### 单元测试

- Plan JSON 校验。
- 重复任务 key、非法依赖和循环依赖。
- 工具名称和风险等级校验。
- Task 状态转换。
- 依赖排序和串行调度。
- 失败分类和重试次数限制。
- Replan 生成。
- 暂停、恢复、取消和跨项目访问拒绝。

### API 测试

- 创建、查询和确认 Plan。
- 暂停、继续、取消和重试。
- 审批中断、批准恢复和拒绝失败。
- Plan、Task、Approval 归属校验。
- SSE 事件顺序和字段完整性。

### Docker 端到端测试

1. 创建项目和会话。
2. 提交多步骤请求。
3. 生成并展示 Plan。
4. 用户确认计划。
5. Task 按依赖串行执行。
6. 写文件任务触发审批。
7. 批准后继续执行。
8. 模拟失败并重试。
9. 触发 Replan 并从未完成任务继续。
10. 验证暂停、恢复、取消和审计记录。

## 12. 默认假设

- 继续使用 FastAPI、PostgreSQL、SQLAlchemy、LangGraph、React 和 SSE。
- Planner 只生成结构化计划，不直接执行工具。
- 计划默认需要用户确认。
- 任务默认串行执行。
- 单个任务最多自动重试 2 次。
- 阶段三的安全策略、审批和审计继续有效。
- 不引入多用户权限系统和并行任务执行。
