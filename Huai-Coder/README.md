# Huai-Coder

Huai-Coder 第一、二阶段基础版本。

## Docker 启动

在当前目录创建 `.env`，设置 `POSTGRES_PASSWORD`，然后执行：

```powershell
docker compose up -d --build
```

API 健康检查：<http://localhost:8000/health>

## 第一阶段成果

- FastAPI 后端与 PostgreSQL 数据库
- 项目、会话、消息基础 API
- React/Vite 前端工作区
- Docker Compose 启动环境
- Alembic 数据库迁移
- `.env` 和敏感信息提交防护

## 第二阶段成果

- LangGraph ReAct Agent 基础图
- OpenAI-compatible 模型接口配置
- `list_dir`、`read_file`、`grep_code` 文件工具
- 工作区路径越界保护
- SSE Agent 事件流
- Agent Run/Event 持久化
- PostgreSQL LangGraph Checkpointer
- React 项目列表、聊天时间线和运行状态展示

当前阶段仍未实现登录认证，后续阶段继续完善。
