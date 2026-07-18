# Huai-Coder

Huai-Coder 是一个面向智能编码工作流的 Python Web 项目。

## 当前阶段

- **第一阶段：基础设施与前后端骨架**：完成 FastAPI、React/Vite、PostgreSQL 和 Docker Compose 基础环境，提供 API 健康检查入口，并加入环境变量与敏感信息提交防护。

## 启动方式

进入项目目录后，复制 `.env.example` 为 `.env`，填写本地数据库密码：

```bash
cd Huai-Coder
docker compose --env-file .env -f deploy/docker-compose.yml up --build
```

API 健康检查地址：<http://localhost:8000/health>。

详细说明见 [`Huai-Coder/README.md`](Huai-Coder/README.md)。

## Git 约定

每个功能独立创建分支，完成校对和验证后提交 Pull Request，审核通过后再合并到主分支。真实凭据不得提交到仓库。
111
