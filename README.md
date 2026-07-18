# Huai-Coder

Huai-Coder 是面向智能编码工作流的 Python Web 项目。

## 启动方式

进入项目目录，复制 `.env.example` 为 `.env`，填写本地数据库密码：

```powershell
cd Huai-Coder
docker compose up -d --build
```

访问地址：

- 前端：<http://localhost>
- API 健康检查：<http://localhost:8000/health>

## 阶段成果

- **第一阶段：基础设施与前后端骨架**：完成 FastAPI、React/Vite、PostgreSQL、Docker Compose、项目/会话/消息 API、Alembic 数据库迁移和敏感信息提交防护。
- **第二阶段：LangGraph ReAct Agent**：完成 LangGraph Agent、OpenAI-compatible 模型适配、文件工具、SSE 实时事件流、Agent 运行记录、PostgreSQL Checkpointer 和 React 聊天工作区。

## Git 协作约定

每个阶段或功能都必须创建独立分支，完成校对和测试后提交 Pull Request，审核通过后合并到 `main`。每次阶段性更新都必须同步更新本 README。真实密码、API Key、Token 和私钥不得提交到仓库。
