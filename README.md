# Huai-Coder

Huai-Coder 是一个面向项目级代码分析与自动化修改的 Python Web Agent，使用 React/Vite、FastAPI、PostgreSQL、LangGraph 和 Docker Compose 构建。

## 当前能力

- 项目管理与多会话聊天
- 文件夹递归上传和单文件上传
- 项目文件上下文读取与代码分析
- LangGraph Agent、SSE 实时事件和 PostgreSQL Checkpointer
- `list_dir`、`read_file`、`grep_code`、`write_file`、`execute_command` 工具
- 工作区路径隔离、敏感文件保护和命令风险分析
- 文件写入与命令执行的人工审批（HITL）
- Plan-and-Execute：结构化计划、计划确认、任务依赖和串行执行
- 计划暂停、继续、取消、失败重试和审计日志基础能力

## 启动

在项目目录创建 `.env`，至少设置 `POSTGRES_PASSWORD`，然后执行：

```powershell
cd Huai-Coder
docker compose up -d --build
```

访问：

- 前端：http://localhost
- API 健康检查：http://localhost:8000/health

## 开发验证

```powershell
cd Huai-Coder/frontend
npm.cmd run build

cd ..
docker compose up -d --build
```

## 阶段进展

- 阶段一：完成项目、会话、消息、PostgreSQL 和 React 基础设施。
- 阶段二：完成 LangGraph ReAct Agent、文件工具、SSE 和运行记录。
- 阶段三：完成安全工具、工作区隔离、人工审批和审计日志。
- 阶段四：完成 Plan-and-Execute 基础闭环和任务状态管理。

## 安全说明

Agent 只能访问当前项目工作区。路径穿越、工作区外访问和未授权命令会被拦截。`.env`、API Key、Token、密码和证书等敏感内容不会自动输出；敏感读写操作需要用户明确审批。

请勿将真实密码、API Key、Token 或私钥提交到 Git。
