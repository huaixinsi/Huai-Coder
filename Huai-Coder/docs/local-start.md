# 一键启动本地开发环境

`scripts/start-local.cmd` 会统一启动：

1. Windows 宿主机 Local Runner（8765）；
2. Windows 宿主机 Playwright MCP（8931）；
3. Docker Compose 的 backend、database 和 frontend；
4. MCP 配置生成、健康检查和工具刷新。

首次运行会通过 `npx` 下载 Playwright MCP，缓存保存在项目的 `.huai-coder-runtime/npm-cache`，后续启动会复用缓存；Docker Desktop 需要保持运行。

启动脚本会自动清理 Huai-Coder 专用的 `8765` 和 `8931` 端口上的旧进程，因此之前手动启动过 Runner 或 MCP 也可以直接重新执行；之后只使用一键脚本即可。

## 使用

双击：

```text
scripts/start-local.cmd
```

脚本会提示输入绑定工作区的绝对路径，例如：

```text
F:\Dirty work
```

也可以在 PowerShell 中直接指定：

```powershell
.\scripts\start-local.cmd "F:\Dirty work"
```

默认启动可见浏览器；如果只需要后台浏览器：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-local.ps1 -Workspace "F:\Dirty work" -Headless
```

脚本会把日志和进程状态写入被 Git 忽略的 `.huai-coder-runtime`。停止宿主机 Runner 和 Playwright MCP：

```powershell
.\scripts\stop-local.cmd
```

连 Docker 一起停止：

```powershell
.\scripts\stop-local.cmd -StopDocker
```

## 启动成功标准

脚本最后应显示：

```text
Browser MCP connected. Found 6 tools.
```

页面 MCP 面板应显示 `playwright-host · ready`。如果只看到服务启动但没有 `ready`，优先查看：

```text
.huai-coder-runtime/logs/playwright-mcp.stderr.log
.huai-coder-runtime/logs/runner.stderr.log
```
