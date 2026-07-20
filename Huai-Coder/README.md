# Huai-Coder

Huai-Coder 是一个支持项目级代码分析、文件修改和计划执行的 Web Agent。

## Docker 启动

在当前目录准备 `.env`：

```env
POSTGRES_PASSWORD=your-password
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=your-api-key
LLM_MODEL=deepseek-chat
```

启动服务：

```powershell
docker compose up -d --build
```

服务地址：

- 前端：http://localhost
- API：http://localhost:8000
- 健康检查：http://localhost:8000/health

## 使用流程

1. 创建或选择项目。
2. 创建或选择会话。
3. 使用“选择完整文件夹”上传项目，也可以单独选择多个文件。
4. 输入问题，Agent 会先生成执行计划。
5. 查看计划后点击“确认计划”。
6. 涉及写文件、执行命令或敏感配置时，在审批弹窗中批准、拒绝或取消。

## 已实现功能

### 项目与会话

- 一个项目支持多个会话。
- 支持会话切换、创建和删除。
- 会话消息持久化到 PostgreSQL。

### 文件与 Agent

- 支持完整文件夹递归上传。
- 支持单文件和多文件上传。
- 保留目录结构和相对路径。
- 自动生成项目文件清单和代码上下文。
- 支持 `list_dir`、`read_file` 和 `grep_code`。

### 安全与审批

- 只能访问当前项目工作区。
- 拒绝路径穿越、绝对路径和工作区外访问。
- `.env`、API Key、Token、密码、SSH 配置和证书等敏感内容默认不回显。
- `write_file` 和 `execute_command` 等高风险操作需要人工确认。
- 审批结果和工具执行结果写入审计日志。

### Plan-and-Execute

- Planner 输出结构化 JSON 计划。
- Plan Validator 检查任务字段、依赖和循环依赖。
- 计划确认后才会执行任务。
- 支持任务状态、依赖关系、串行调度和基础重试。
- 支持计划暂停、继续和取消。

## 主要 API

```text
GET  /api/projects
POST /api/projects
GET  /api/projects/{project_id}/sessions
POST /api/sessions
POST /api/runs
GET  /api/plans/{plan_id}
GET  /api/plans/{plan_id}/tasks
POST /api/plans/{plan_id}/confirm
POST /api/plans/{plan_id}/pause
POST /api/plans/{plan_id}/resume
POST /api/plans/{plan_id}/cancel
GET  /api/runs/{run_id}/approvals
POST /api/approvals/{approval_id}/approve
POST /api/approvals/{approval_id}/reject
```

## 验证

```powershell
cd frontend
npm.cmd run build

docker compose up -d --build
curl http://localhost:8000/health
```

## 开发约定

- 每个阶段使用独立分支开发。
- 修改后运行前端构建、后端测试和 Docker 验证。
- 阶段完成后提交 Pull Request。
- 不提交真实密钥、密码、Token 或私钥。
