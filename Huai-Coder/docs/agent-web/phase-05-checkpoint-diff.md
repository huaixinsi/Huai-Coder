# 阶段五：Checkpoint、Diff 与回滚

## 目标

为每个 Plan Task 建立可恢复工作区，支持查看变更、确认结果和只回滚当前失败任务。

## 技术方案

- 使用 GitPython 或受控 Git CLI。
- PostgreSQL 保存 checkpoint 元数据、任务关联和 Diff 摘要。
- 工作区文件和 Git 对象保存在项目工作目录。
- LangGraph Checkpointer 保存 Agent 状态；Workspace Checkpoint 保存代码状态。

## 后端任务

1. 实现 Workspace Manager。
2. 任务首次执行前创建 checkpoint。
3. 任务成功后生成 Diff。
4. 任务校验失败时回滚当前任务。
5. 实现文件变更查询。
6. 实现 checkpoint 清理和保留策略。

## 前端任务

1. 实现 DiffViewer。
2. 支持文件列表和代码高亮。
3. 展示新增、修改、删除内容。
4. 提供确认保留和回滚操作。

## 验收

- 每个任务都有可查询 checkpoint。
- 当前任务回滚不会覆盖前置成功任务。
- 用户可以在页面查看完整 Diff。
- Agent、任务和工作区状态可以在服务重启后恢复。
