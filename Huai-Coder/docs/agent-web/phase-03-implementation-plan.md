# 阶段三：工具安全与人工审批（HITL）实施计划

## 1. 目标与安全边界

在现有 LangGraph Agent 上增加安全可控的文件写入、命令执行和人工审批能力。

- 工具只能访问当前项目工作区。
- 禁止绝对路径、`..` 路径穿越和符号链接逃逸。
- 低风险只读操作自动执行。
- 写文件、命令执行、删除、安装依赖、构建部署等操作按风险等级进入审批。
- `.env`、API Key、Token、密码、SSH 配置、云凭证、证书等敏感路径需要明确审批。
- 敏感内容不回显到聊天窗口，也不原文写入审计日志。

## 2. Tool Registry

建立统一工具注册中心，工具描述至少包含：

- 工具名称和描述
- 参数定义
- 风险等级
- 是否允许自动执行
- 执行函数
- 审批说明

迁移现有工具：

- `list_dir`
- `read_file`
- `grep_code`

新增工具：

- `write_file`
- `execute_command`

Agent 不再直接调用底层工具，所有调用必须经过 Registry、PathGuard 和风险分析器。

## 3. PathGuard 与敏感路径策略

统一解析路径并校验最终目标：

- 相对路径解析到当前项目工作区。
- 拒绝绝对路径和工作区外路径。
- 拒绝 `../` 穿越。
- 检查符号链接最终目标。
- 识别 `.env`、`.env.*`、`.ssh`、`credentials`、`secret`、`token`、`id_rsa`、`*.pem`、`*.key` 等路径。

敏感操作流程：

1. 识别敏感路径。
2. 创建审批记录。
3. SSE 通知前端。
4. 等待用户批准、拒绝或取消。
5. 批准后执行，结果脱敏；否则终止操作。

## 4. 风险分析与工具执行

低风险示例：

- 列目录、读取普通代码文件、搜索代码。
- 查看 Git 状态、版本号和只读检查结果。

需要审批或禁止的操作：

- 写入、覆盖、删除文件。
- `git reset`、`git checkout`、`git clean`。
- `rm`、`del`、`Remove-Item`。
- 安装依赖、启动/停止服务、构建和部署。
- 未知或无法识别的命令。

命令执行必须：

- 固定工作目录为项目工作区。
- 限制环境变量，不能暴露服务端密钥。
- 设置超时和最大输出长度。
- 记录退出码、标准输出、标准错误和耗时。
- 兼容 Bash、PowerShell 和 cmd 的基础风险识别。

`write_file` 使用临时文件写入后原子替换，避免写入中断导致文件损坏。

## 5. 审批与审计数据

新增 `approvals` 表，字段包括：

- `id`、`run_id`、`session_id`
- `tool_name`、`arguments`
- `risk_level`、`risk_reason`
- `target_path`
- `status`
- `requested_at`、`resolved_at`
- `resolution_reason`

审批状态：

```text
PENDING / APPROVED / REJECTED / CANCELLED / EXPIRED
```

新增 `audit_logs` 表，记录：

- 项目、会话、Run
- 事件类型和工具名称
- 脱敏参数摘要
- 执行结果摘要
- 错误信息和时间

## 6. API 与前端

新增 API：

```text
GET  /api/runs/{run_id}/approvals
GET  /api/approvals/{approval_id}
POST /api/approvals/{approval_id}/approve
POST /api/approvals/{approval_id}/reject
POST /api/approvals/{approval_id}/cancel
GET  /api/runs/{run_id}/audit-events
```

前端审批弹窗展示：

- 工具名称
- 风险等级和原因
- 目标路径
- 命令或参数摘要
- 批准、拒绝、取消按钮

敏感参数只显示脱敏结果。页面需要展示等待审批、执行中、成功、失败和取消状态。

## 7. 测试与验收

### 单元测试

- 工作区内路径通过。
- 路径穿越、绝对路径、符号链接逃逸拒绝。
- 敏感路径识别。
- 文件写入原子性。
- 命令风险分类、超时和输出限制。
- 未注册工具拒绝执行。

### 集成测试

- 创建、查询、批准、拒绝和取消审批。
- 重复审批和跨项目访问拒绝。
- 审计日志完整且敏感信息脱敏。
- 批准后文件写入或命令执行。
- 拒绝后不发生实际修改。

### Docker 端到端测试

1. 创建项目和会话。
2. Agent 请求写文件。
3. 前端收到审批弹窗。
4. 批准后文件写入工作区。
5. 危险命令触发审批。
6. 拒绝后验证系统和文件未被修改。
7. 查询审计日志确认结果完整。

## 8. 默认假设

- 命令运行在后端 Docker 容器内，不直接运行宿主机命令。
- 工作区是唯一允许访问的文件根目录。
- 不引入登录、多租户和复杂权限系统。
- 审批人默认是当前前端操作者。
