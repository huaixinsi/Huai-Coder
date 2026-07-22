# 本地 Runner 与自动依赖安装

浏览器无法直接启动本机进程，因此 Huai-Coder 使用一个运行在用户电脑上的 Local Runner 执行命令。Agent 仍然通过 ReAct 决定工具调用，前端把 `execute_command` 请求转发给 Runner，Runner 返回命令输出后，Agent 继续下一轮推理。

## 启动

在项目后端目录安装依赖后，绑定哪个项目目录，就把哪个目录传给 Runner：

```powershell
cd backend
python -m app.runner_server --workspace "D:\projects\my-project"
```

默认监听 `http://127.0.0.1:8765`。前端通过 `VITE_RUNNER_URL` 覆盖地址：

```powershell
$env:VITE_RUNNER_URL = "http://127.0.0.1:8765"
```

浏览器中选择的文件夹必须与 Runner 的 `--workspace` 相同。浏览器不能把真实文件系统路径传给网页，这是浏览器安全模型的限制。

## 自动准备的依赖

Runner 会识别以下清单，并在第一次执行命令前安装依赖：

- Python：`requirements.txt`、`pyproject.toml`，使用项目内 `.huai-coder-venv`。
- Node：`package.json`，根据 lock 文件选择 npm、pnpm 或 yarn。
- Java：`pom.xml`、`build.gradle`。
- Go：`go.mod`。
- Rust：`Cargo.toml`。
- Ruby：`Gemfile`。
- PHP：`composer.json`。

安装失败会按安装器提供的备用命令最多重试 3 次；命令因缺少依赖失败时，会重新准备依赖后最多重跑 3 次。删除磁盘、重置 Git、控制系统服务等命令会被本地安全策略拒绝。

## HTTP 接口

```text
GET  /health
POST /v1/prepare
POST /v1/execute
```

`/v1/execute` 请求示例：

```json
{
  "command": "python -c \"import colorama; print(colorama.__version__)\"",
  "auto_prepare": true,
  "timeout_seconds": 120
}
```

响应包含 `dependency_steps`、`attempts`、`result`、`exit_code` 和 `error_type`，前端会把安装和执行结果显示在工具调用详情中。
