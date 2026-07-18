# 阶段一：基础设施与前后端骨架

## 目标

建立 Python 后端、React 前端和 PostgreSQL 基础设施，形成可以创建项目、创建会话和保存消息的最小 Web 应用。

## 技术栈

- FastAPI
- Python 3.12+
- React + TypeScript + Vite
- SQLAlchemy 2.x
- Alembic
- asyncpg
- PostgreSQL
- pytest
- Vitest

## 后端任务

1. 创建 `backend/` 和 `pyproject.toml`。
2. 配置 FastAPI、CORS、健康检查和统一异常处理。
3. 配置 PostgreSQL 连接池和异步 Session。
4. 创建 `projects`、`sessions`、`messages` 基础表。
5. 编写项目、会话、消息 API。
6. 增加数据库迁移。
7. 增加 API 和 Repository 测试。

## 前端任务

1. 创建 Vite React TypeScript 应用。
2. 创建工作区布局。
3. 实现项目列表和项目切换。
4. 实现会话列表。
5. 实现聊天输入区域和消息列表占位。
6. 配置 API Client、错误提示和加载状态。

## 验收

- `GET /health` 返回成功。
- 可以创建项目和会话。
- 刷新页面后数据仍在 PostgreSQL 中。
- 前端可以完成项目切换和会话切换。
- 测试数据库与开发数据库隔离。
