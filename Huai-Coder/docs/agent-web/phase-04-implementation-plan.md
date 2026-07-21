# 阶段四：Plan-and-Execute 计划与任务执行系统

## 总结

将当前“一次请求对应一次 Agent 执行”升级为可追踪的多步骤任务系统：

```text
用户请求
  → Planner 生成计划
  → Plan Validator 校验计划
  → 用户确认计划
  → Task Dispatcher 按依赖调度
  → 工具执行
  → 验证结果
  → 成功 / 重试 / 重新规划 / 取消
```

默认策略：

- 计划生成后必须用户确认。
- 任务按依赖关系执行。
- 无依赖任务默认串行执行，避免工作区文件冲突。
- 继续复用阶段三的审批、工具安全和 SSE 机制。
- 每个任务都必须有独立状态、输入、输出和执行日志。

## 一、数据模型与状态机

新增三类核心数据。

### Plan

字段：

- `id`
- `project_id`
- `session_id`
- `run_id`
- `goal`
- `summary`
- `status`
- `version`
- `created_at`
- `updated_at`
- `confirmed_at`
- `cancelled_at`

Plan 状态：

```text
DRAFT
WAITING_CONFIRMATION
READY
RUNNING
PAUSED
SUCCEEDED
FAILED
CANCELLED
REPLANNING
```

### Task

字段：

- `id`
- `plan_id`
- `task_key`
- `title`
- `description`
- `task_type`
- `status`
- `input_data`
- `output_data`
- `error_type`
- `error_message`
- `retry_count`
- `max_retries`
- `started_at`
- `finished_at`

Task 状态：

```text
PENDING
BLOCKED
READY
RUNNING
WAITING_APPROVAL
RETRYING
SUCCEEDED
FAILED
CANCELLED
SKIPPED
```

### TaskDependency

字段：

- `id`
- `plan_id`
- `task_id`
- `depends_on_task_id`
- `dependency_type`

第一版只支持完成依赖：

```text
task B 只有在 task A 成功后才能执行
```

阶段三的 `Approval` 和 `AuditLog` 增加可选的：

- `plan_id`
- `task_id`

这样可以追踪某一次审批属于哪个任务，而不是只属于整个 Run。

## 二、Planner 设计

### 1. 结构化计划输出

Planner 不再返回普通文本，而是输出严格 JSON：

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

Planner 必须遵守：

- 不直接执行工具。
- 不生成不存在的工具名称。
- 任务数量设置上限，例如最多 20 个。
- 每个任务必须有唯一 `task_key`。
- 依赖只能引用同一计划中的任务。
- 禁止循环依赖。
- 高风险任务必须标记风险等级。
- 不允许在计划中直接写入密钥、密码或 Token。

### 2. 计划校验器

Plan Validator 检查：

- JSON 格式。
- 必填字段。
- 任务数量。
- `task_key` 唯一性。
- 依赖任务是否存在。
- 是否存在循环依赖。
- 任务类型是否受支持。
- 工具名称是否在 Tool Registry 中。
- 风险等级是否与工具安全策略一致。
- 是否包含敏感信息。
- 是否存在越权路径。
- 任务是否具备可验证的成功条件。

校验失败时：

1. 记录校验错误。
2. 不进入执行阶段。
3. 允许 Planner 自动修正一次。
4. 二次修正仍失败时将 Plan 标记为 `FAILED`。

### 3. 计划确认

Plan 生成并校验通过后：

- 保存 Plan 和 Task。
- SSE 推送 `plan.created`。
- 前端展示计划面板。
- Plan 状态设置为 `WAITING_CONFIRMATION`。
- 用户点击确认后进入 `READY`。
- 用户取消后进入 `CANCELLED`。
- 用户确认前不得执行任何任务。

新增接口：

```text
POST /api/plans/{plan_id}/confirm
POST /api/plans/{plan_id}/cancel
GET  /api/plans/{plan_id}
GET  /api/plans/{plan_id}/tasks
```

## 三、Task Dispatcher 调度器

### 1. 依赖调度

调度器每次选择一个满足以下条件的任务：

- 状态为 `PENDING` 或 `READY`。
- 所有依赖任务状态为 `SUCCEEDED`。
- 当前 Plan 状态为 `RUNNING`。
- 任务未被取消。
- 任务没有超过最大重试次数。

没有满足条件的任务时：

- 如果所有任务成功，Plan 标记为 `SUCCEEDED`。
- 如果存在失败任务且不可重试，Plan 标记为 `FAILED`。
- 如果存在等待审批任务，Plan 标记为 `PAUSED`。
- 如果存在未满足依赖但依赖任务失败，阻塞任务标记为 `SKIPPED`。

### 2. 串行执行

第一版只允许一个任务同时运行：

```text
一次只执行一个 Task
```

原因：

- 避免多个任务同时修改相同文件。
- 审批事件顺序更容易追踪。
- 失败恢复和回滚边界更清晰。
- 与当前单工作区模型兼容。

### 3. 任务级事件

通过 SSE 推送：

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

每个事件至少包含：

- `plan_id`
- `task_id`
- `run_id`
- `type`
- `content`
- `status`
- `tool`
- `created_at`

## 四、任务执行与工具整合

### 1. 任务执行上下文

每个 Task 执行时获得：

- 当前项目工作区。
- Plan 目标。
- 当前任务描述。
- 依赖任务的结果摘要。
- 当前会话历史。
- 允许使用的工具列表。
- 当前任务风险等级。
- 成功标准。

Agent 不再把整个 Plan 作为无结构文本传递，而是使用明确的任务上下文。

### 2. 工具调用

所有工具继续经过阶段三的 Tool Registry：

- `list_dir`
- `read_file`
- `grep_code`
- `write_file`
- `execute_command`

任务执行禁止绕过：

- `PathGuard`
- 风险分析器
- Approval
- AuditLog

### 3. 审批关联

如果任务需要审批：

1. Task 状态变为 `WAITING_APPROVAL`。
2. Plan 状态变为 `PAUSED`。
3. 创建关联的 Approval。
4. SSE 推送 `task.approval_required`。
5. 用户批准后恢复当前 Task。
6. 用户拒绝后按失败策略处理。
7. 用户取消后 Task 和 Plan 进入取消流程。

审批只影响当前任务，不会丢失整个计划。

## 五、失败分类、重试和 Replan

### 1. 失败分类

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

### 2. 重试策略

默认重试配置：

- 每个任务最多重试 2 次。
- 只对临时性错误重试：
  - 网络错误
  - LLM 超时
  - 工具临时失败
  - 命令超时
- 不自动重试：
  - 路径越界
  - 审批拒绝
  - 参数校验失败
  - 敏感路径违规
  - 不存在的工具
  - 权限错误

重试时：

- `retry_count + 1`
- Task 状态为 `RETRYING`
- 记录失败原因
- SSE 推送 `task.retrying`
- 重试次数耗尽后进入失败处理。

### 3. Replan

以下情况触发 Replan：

- 任务失败且无法自动重试。
- 依赖任务输出与预期不符。
- 工具返回项目状态发生变化。
- 验证步骤发现原计划无法继续。
- 用户主动要求调整计划。

Replan 流程：

1. 保留原 Plan 和历史任务。
2. 新建 Plan 版本。
3. 标记原 Plan 为 `REPLANNING`。
4. Planner 输入：
   - 原始目标
   - 已成功任务
   - 失败任务
   - 错误原因
   - 当前工作区状态
5. 生成新任务集合。
6. 用户重新确认新 Plan。
7. 从未完成任务继续执行。

成功任务不重复执行，除非新计划明确要求重做。

## 六、暂停、继续和取消

新增接口：

```text
POST /api/plans/{plan_id}/pause
POST /api/plans/{plan_id}/resume
POST /api/plans/{plan_id}/cancel
POST /api/tasks/{task_id}/retry
POST /api/plans/{plan_id}/replan
```

行为定义：

### Pause

- 当前正在执行的工具允许完成。
- 不启动下一个 Task。
- Plan 状态变为 `PAUSED`。
- 新的任务不再调度。

### Resume

- 校验 Plan 当前状态。
- 从第一个可执行任务继续。
- 已成功任务不重复执行。

### Cancel

- 当前工具尽量安全停止。
- 未开始任务全部标记为 `CANCELLED`。
- 等待审批任务对应 Approval 标记为 `CANCELLED`。
- Plan 标记为 `CANCELLED`。
- 写入审计日志。

### Retry

- 仅允许重试 `FAILED` 任务。
- 重置任务执行状态。
- 不重置已完成依赖任务。
- 超过最大重试次数时拒绝请求。

## 七、前端 PlanPanel

新增计划展示区域，包含：

- Plan 目标和摘要。
- Plan 当前状态。
- 任务列表。
- 每个任务的状态。
- 任务依赖关系。
- 风险等级。
- 当前任务。
- 重试次数。
- 错误信息。
- 工具调用日志。
- 审批状态。

任务展示示例：

```text
1. 检查项目结构          已完成
2. 修改配置文件           等待审批
3. 执行测试               被阻塞
4. 生成结果报告           待执行
```

新增交互：

- 确认计划
- 取消计划
- 暂停执行
- 继续执行
- 重试失败任务
- 重新规划
- 查看任务详情
- 查看任务日志

PlanPanel 通过现有 SSE 实时更新，不使用轮询作为主要状态来源。

页面刷新后：

- 重新加载当前会话的活动 Plan。
- 恢复任务状态。
- 显示未完成任务。
- 显示等待审批任务。
- 不重复触发已经完成的任务。

## 八、API 设计

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

所有接口必须验证：

- Plan 属于指定项目。
- Plan 属于指定会话。
- Task 属于指定 Plan。
- Task 不属于其他项目。
- 当前状态允许执行该操作。
- 不允许跨项目读取或修改 Plan。

原有接口兼容：

- `/api/runs`
- `/api/runs/{run_id}/events`
- `/api/approvals/*`
- 项目、会话、消息接口

## 九、数据库迁移与兼容

新增迁移：

- `plans`
- `tasks`
- `task_dependencies`

扩展现有表：

- `agent_runs.plan_id`
- `approvals.plan_id`
- `approvals.task_id`
- `audit_logs.plan_id`
- `audit_logs.task_id`

兼容策略：

- 旧 Run 没有关联 Plan 时，继续使用原有单步 Agent 流程。
- 新请求默认进入 Plan-and-Execute 流程。
- 可通过请求参数或系统配置暂时关闭 Planner，回退到单步执行。
- 已完成的阶段三审批和 Run 数据保持可查询。
- 旧 Checkpoint 不强制迁移，恢复时根据是否存在 Plan 判断使用旧流程还是新流程。

## 十、测试计划

### 后端单元测试

覆盖：

- Plan JSON 校验。
- 缺少必填字段。
- 重复任务 key。
- 不存在的依赖。
- 循环依赖。
- 非法工具名称。
- 风险等级不匹配。
- 任务状态转换。
- 非法状态转换。
- 依赖排序。
- 串行调度。
- 失败分类。
- 重试次数限制。
- Replan 生成。
- Plan 取消和暂停。
- 跨项目访问拒绝。

### API 集成测试

覆盖：

- 创建 Plan。
- 查询 Plan 和 Task。
- 确认 Plan。
- 取消 Plan。
- 暂停和恢复 Plan。
- 重试失败任务。
- 触发 Replan。
- 审批中断任务。
- 批准审批后继续执行。
- 拒绝审批后任务失败。
- Plan、Task、Approval 归属校验。
- SSE 事件顺序和字段完整性。

### Agent 测试

覆盖：

- 多步骤请求生成多个任务。
- 计划校验成功后等待用户确认。
- 未确认前不调用工具。
- 任务按依赖顺序执行。
- 任务成功后进入下一个任务。
- 工具失败触发有限重试。
- 不可重试错误触发失败。
- 任务失败后生成新 Plan。
- 暂停后不继续调度。
- 恢复后从正确任务继续。
- 取消后不执行后续任务。
- 审批恢复后继续当前任务。

### 前端测试

覆盖：

- PlanPanel 展示计划。
- 任务状态实时更新。
- 依赖关系展示。
- 确认、取消、暂停、继续。
- 失败任务重试。
- Replan 操作。
- 审批弹窗与任务状态联动。
- 页面刷新恢复活动 Plan。
- SSE 断开后重新连接并恢复状态。

### Docker 端到端测试

验证完整流程：

1. 创建项目和会话。
2. 提交一个需要多个步骤的请求。
3. Agent 生成 Plan。
4. 前端展示任务和依赖。
5. 用户确认计划。
6. 任务按顺序执行。
7. 写文件任务触发审批。
8. 用户批准后继续。
9. 测试任务失败并自动重试。
10. 重试耗尽后触发 Replan。
11. 用户确认新 Plan。
12. 新 Plan 从未完成任务继续。
13. 用户暂停并恢复。
14. 用户取消任务。
15. 查询 Plan、Task、Approval、AuditLog，确认记录完整。

## 交付结果

- 新分支：`feature/plan-execute`
- Plan、Task、TaskDependency 数据模型和迁移。
- Planner 和 Plan Validator。
- Task Dispatcher 和串行依赖调度器。
- 任务状态机、失败分类、有限重试和 Replan。
- Plan 确认、暂停、继续、取消和重试 API。
- Plan 级 SSE 事件。
- 前端 PlanPanel。
- 与阶段三审批和安全工具完整联动。
- 后端单元测试、API 测试、Agent 测试和 Docker 端到端测试。
- 更新阶段四文档和 README。
- 通过 Docker 验证后提交并创建阶段四 PR。

## 默认假设

- 阶段四继续使用 FastAPI、PostgreSQL、SQLAlchemy、LangGraph、React 和 SSE。
- Planner 只生成结构化计划，不直接调用工具。
- 计划默认需要用户确认。
- 任务默认串行执行。
- 单个任务最多自动重试 2 次。
- 成功任务在 Replan 时不重复执行。
- 阶段三的安全策略、敏感路径保护和审批机制继续有效。
- 当前阶段不引入多用户权限系统。
- 当前阶段不做并行任务执行，避免工作区冲突。


