# Huai-Coder

第一阶段基础设施：FastAPI、PostgreSQL 与健康检查 API。

## Docker 启动

复制 `.env.example` 为 `.env`，仅在本机填写 `POSTGRES_PASSWORD`，然后执行：

```bash
docker compose --env-file .env -f deploy/docker-compose.yml up --build
```

API 健康检查地址：`http://localhost:8000/health`。`.env` 已被 Git 忽略，真实凭据不得提交到仓库。
