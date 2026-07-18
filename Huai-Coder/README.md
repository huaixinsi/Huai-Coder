# Huai-Coder

第一阶段基础设施：FastAPI、PostgreSQL 与健康检查 API。

## Docker 启动

复制 `.env.example` 为 `.env`，仅在本机填写 `POSTGRES_PASSWORD`，然后执行：

```bash
docker compose up -d --build
```

API 健康检查地址：`http://localhost:8000/health`。`.env` 已被 Git 忽略，真实凭据不得提交到仓库。

## 阶段成果

- **第一阶段：基础设施与前后端骨架**：完成 FastAPI、React/Vite、PostgreSQL 和 Docker Compose 基础环境，提供 API 健康检查入口，并加入环境变量与敏感信息提交防护。
