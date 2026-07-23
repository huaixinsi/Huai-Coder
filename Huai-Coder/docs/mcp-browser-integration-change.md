# MCP 接入与浏览器自动化详细变更文档

## 1. 文档信息

| 项目 | 内容 |
| --- | --- |
| 文档名称 | MCP 接入与浏览器自动化详细变更文档 |
| 适用项目 | Huai-Coder |
| 文档状态 | 非 GitHub 范围已实施并完成本地验收；GitHub 真实联调延期 |
| 目标版本 | MCP Browser Automation Phase 1 |
| 编写日期 | 2026-07-23 |
| 关联能力 | ReAct、Plan-and-Execute、Local Runner、工具审批、上下文压缩 |

## 2. 变更背景

当前项目已经具备以下能力：

- 基于 ReAct 循环调用内置工具；
- 通过 Plan-and-Execute 拆分复杂任务；
- 通过浏览器文件系统权限读写本地绑定工作区；
- 通过 Local Runner 在宿主机执行脚本和安装依赖；
- 通过工具事件向前端展示执行过程；
- 通过长期记忆和上下文压缩减少重复上下文；
- 通过工具审批控制高风险操作。

当前的主要限制是：

1. 工具主要写死在 `backend/app/registry.py` 中，不能动态接入外部工具服务。
2. Agent 只能调用项目内置工具，不能按需发现 MCP Server 提供的工具。
3. 尚未提供标准化的 MCP Server 生命周期管理。
4. 尚未实现浏览器页面快照、点击、输入、等待和页面状态校验。
5. Docker 后端无法直接操作用户 Windows 桌面上的登录浏览器。
6. 外部工具的连接状态、工具列表、调用结果和权限状态还没有统一展示。

本次变更的目标，是让 Huai-Coder 具备类似 Codex、Claude 的外部工具接入能力，并优先接入 Playwright MCP，使 Agent 能够完成如下任务：

> 打开网页，等待页面加载，查找输入框，输入内容，点击按钮，等待结果出现，读取结果；如果页面状态发生变化或工具失败，重新获取页面结构并继续执行。

## 3. 设计目标

### 3.1 必须实现

- 支持 MCP Client 能力；
- 支持本地 `stdio` MCP Server；
- 预留 Streamable HTTP MCP Server；
- 支持动态发现 MCP 工具；
- 支持将 MCP 工具转换为当前 LLM 的工具定义；
- 支持把 MCP 工具调用结果重新放回 ReAct 循环；
- 接入 Playwright MCP；
- 支持浏览器导航、快照、点击、输入和等待；
- 支持浏览器会话在同一次 Agent Run 内保持；
- 支持工具错误反馈和自动重新思考；
- 支持高风险浏览器动作的人工确认；
- 支持前端实时展示 MCP 连接和工具调用过程；
- 支持 Docker 后端与宿主机浏览器分离运行。

### 3.2 暂不实现

以下能力不纳入第一期：

- MCP Server 市场或一键安装中心；
- 任意远程 MCP Server 的自动信任；
- 浏览器截图识别作为主要定位手段；
- 多个 Agent 同时控制同一个浏览器 Profile；
- 自动获取或保存用户 Cookie、密码、Token；
- 让模型直接执行任意浏览器 DevTools Protocol 指令；
- 将浏览器 MCP Server 放入后端 Docker 后控制宿主机桌面浏览器。

## 4. 参考设计原则

### 4.1 参考 Codex

Codex 的核心思想是将 Agent 循环、工具执行、审批、安全边界和状态展示拆开。Huai-Coder 应保持以下边界：

```text
Agent：判断下一步做什么
MCP Gateway：管理外部 MCP Server 和工具
MCP Server：执行具体能力
前端：展示进度、请求审批、提供取消入口
安全层：判断工作区、网络、凭据和副作用风险
```

不要将浏览器控制逻辑直接写入 `agent.py`，否则后续接入 GitHub、数据库或搜索服务时会持续增加 Agent 内部耦合。

Codex 的 App Server 设计也强调双向会话、过程事件和持续状态，而不仅是一次性调用。参考：[OpenAI Codex App Server](https://openai.com/index/unlocking-the-codex-harness/)。

### 4.2 参考 Claude

Claude 将 MCP 作为外部能力的统一接入方式。模型只需要看到标准化工具名称、描述和 JSON Schema，不需要理解每个外部服务的实现细节。

因此，本项目应让 Agent 使用统一的工具调用格式：

```text
内置工具：read_file、write_file、execute_command
MCP 工具：mcp__playwright__browser_click
          mcp__github__create_pull_request
          mcp__database__query
```

参考：[Anthropic MCP 文档](https://docs.anthropic.com/en/docs/mcp)。

### 4.3 参考 MCP 官方规范

MCP 工具需要通过 `tools/list` 发现，通过 `tools/call` 调用，并使用 JSON Schema 描述参数。工具失败时应将错误作为工具结果返回，让模型能够重新判断，而不是直接终止整个任务。

参考：[MCP Tools 规范](https://modelcontextprotocol.io/specification/2025-03-26/server/tools)。

第一期优先使用 `stdio`，因为它适合本机启动 MCP 子进程；远程服务再使用 Streamable HTTP。参考：[MCP Transport 规范](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports)。

## 5. 总体架构

```text
┌──────────────────────────────┐
│          Web Frontend        │
│  对话、审批、MCP 面板、事件流  │
└──────────────┬───────────────┘
               │ SSE / HTTP
┌──────────────▼───────────────┐
│      FastAPI + Agent          │
│ ReAct、Plan、记忆、上下文压缩  │
└──────────────┬───────────────┘
               │ HTTP / JSON
┌──────────────▼───────────────┐
│         MCP Gateway           │
│ Server 注册、连接、工具路由    │
└───────┬──────────────────────┘
        │
        ├── stdio ── Playwright MCP ── 本机浏览器
        │
        ├── stdio ── GitHub MCP
        │
        └── HTTP ─── 远程 MCP Server

┌──────────────────────────────┐
│       Local Runner            │
│ 本机文件、脚本、依赖安装执行    │
└──────────────────────────────┘
```

### 5.1 Docker 与宿主机边界

当前项目的后端 Docker 负责 API、Agent、数据库和会话状态，但不应该直接控制用户 Windows 桌面浏览器。

建议采用以下方式：

```text
Docker Backend
    ↓
宿主机 MCP Gateway
    ↓
宿主机 Playwright MCP
    ↓
用户浏览器
```

原因：

- Docker 容器通常无法直接访问宿主机已打开的 Chrome 标签页；
- 用户登录态、浏览器 Profile 和桌面显示都在宿主机；
- Windows 浏览器权限和 Docker Linux 环境不同；
- 将浏览器放入 Docker 会变成容器内的隔离浏览器，而不是用户看到的浏览器。

如果只需要自动化测试，可以让 Playwright MCP 在容器内启动无头浏览器；如果要模拟用户点击当前浏览器，则必须使用宿主机模式或浏览器扩展连接模式。

## 6. 模块拆分

建议增加以下目录：

```text
backend/app/mcp/
├── __init__.py
├── models.py              # MCP Server、Tool、Session 数据模型
├── config.py              # MCP 配置读取和校验
├── transport.py           # stdio、Streamable HTTP 传输层
├── client.py              # initialize、tools/list、tools/call
├── registry.py             # MCP Server 注册和工具缓存
├── adapter.py              # MCP 工具转换为 LLM 工具格式
├── router.py               # 工具名称路由和白名单
├── permissions.py          # MCP 工具风险等级和审批判断
├── session.py              # MCP 会话生命周期
└── events.py               # MCP 过程事件标准化

mcp-gateway/
├── package.json
├── src/
│   ├── server.ts           # Gateway HTTP 服务
│   ├── mcp-client.ts       # MCP Client 封装
│   ├── process-manager.ts  # MCP 子进程管理
│   └── session-manager.ts  # 浏览器会话管理
└── config/
    └── mcp.example.json
```

如果后续确定完全使用 Python MCP SDK，可以保留 `mcp-gateway` 的逻辑并迁移到 Python；第一期推荐独立 Node Gateway，原因是 Playwright MCP 及其生态主要围绕 Node 运行，减少 Python 与 Node 进程管理的耦合。

## 7. MCP Server 配置

### 7.1 配置文件示例

```json
{
  "mcpServers": {
    "playwright": {
      "enabled": true,
      "transport": "stdio",
      "command": "npx.cmd",
      "args": [
        "-y",
        "@playwright/mcp@latest",
        "--user-data-dir",
        "C:\\Users\\27982\\.huai-coder\\browser-profile"
      ],
      "scope": "user",
      "connectTimeoutMs": 30000,
      "callTimeoutMs": 120000,
      "approval": {
        "default": "auto",
        "browser_snapshot": "auto",
        "browser_wait_for": "auto",
        "browser_click": "auto",
        "browser_type": "auto",
        "browser_submit": "confirm",
        "browser_publish": "confirm"
      },
      "allowedTools": [
        "browser_navigate",
        "browser_tabs",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_wait_for"
      ]
    }
  }
}
```

说明：

- `scope=user` 表示用户级配置；后续可增加 `global`、`project` 和 `session`；
- `allowedTools` 防止 Server 暴露的所有工具一次性进入模型上下文；
- 第一版应允许固定工具白名单；
- 稳定环境应固定 Playwright MCP 版本，不建议长期使用 `@latest`；
- 浏览器 Profile 应该是项目或会话隔离的，避免多个任务互相污染登录状态。

### 7.2 配置作用域

```text
全局配置
  ↓ 覆盖
用户配置
  ↓ 覆盖
项目配置
  ↓ 覆盖
会话临时配置
```

推荐规则：

- 全局配置：默认 MCP Server 和系统禁用列表；
- 用户配置：用户自己的 GitHub、浏览器和数据库连接；
- 项目配置：项目允许使用哪些 MCP Server 和工具；
- 会话配置：只对当前任务临时开启的服务。

## 8. MCP Client 生命周期

每一个 MCP Server 都应经过以下状态：

```text
disabled
   ↓
configured
   ↓ connect
starting
   ↓ initialize
ready
   ↓ tools/list
available
   ↓ call error
degraded
   ↓ reconnect
ready
   ↓ stop
stopped
```

### 8.1 连接流程

```text
1. 读取 Server 配置
2. 校验 command、args、transport 和权限
3. 启动 stdio 子进程或建立 HTTP 连接
4. 发送 initialize
5. 校验协议版本和能力
6. 调用 tools/list
7. 分页拉取全部工具
8. 过滤 disabled 和不在白名单内的工具
9. 为工具增加命名空间
10. 缓存工具描述
11. 向前端发送 mcp.server.ready
```

### 8.2 工具调用流程

```text
模型产生 tool_call
        ↓
检查 mcp Server 是否 ready
        ↓
校验工具是否在 allowlist
        ↓
检查参数 JSON Schema
        ↓
判断风险等级
        ├── auto：直接调用
        └── confirm：发送审批事件并等待
        ↓
MCP tools/call
        ↓
标准化 content、structuredContent 和 isError
        ↓
返回 Agent ReAct observation
        ↓
向前端发送 completed / failed 事件
```

## 9. 工具转换设计

MCP 工具定义示例：

```json
{
  "name": "browser_click",
  "description": "Click an element on the current page",
  "inputSchema": {
    "type": "object",
    "properties": {
      "element": { "type": "string" },
      "ref": { "type": "string" }
    },
    "required": ["element", "ref"]
  }
}
```

转换后的内部工具定义：

```json
{
  "name": "mcp__playwright__browser_click",
  "description": "[MCP: playwright] Click an element on the current page",
  "parameters": {
    "type": "object",
    "properties": {
      "element": { "type": "string" },
      "ref": { "type": "string" }
    },
    "required": ["element", "ref"]
  },
  "metadata": {
    "server": "playwright",
    "originalName": "browser_click",
    "risk": "reversible",
    "transport": "stdio"
  }
}
```

工具名称必须保存 `server` 和 `originalName`，否则同名工具无法路由回正确的 MCP Server。

## 10. ReAct 循环改造

当前 Agent 已经存在静态工具调用和工具结果回填逻辑，本次不应重写 ReAct 循环，而是增加动态工具层。

改造后的逻辑：

```python
static_tools = registry.list_static_tools(context)
mcp_tools = await mcp_registry.list_tools_for_run(run_context)
available_tools = static_tools + mcp_adapter.to_model_tools(mcp_tools)

response = await complete_with_tools(
    messages=messages,
    tools=available_tools,
)

for tool_call in response.tool_calls:
    if is_static_tool(tool_call.name):
        result = await execute_static_tool(tool_call)
    elif is_mcp_tool(tool_call.name):
        result = await mcp_router.call(tool_call)
    else:
        result = tool_not_found(tool_call.name)

    messages.append(to_tool_observation(tool_call, result))
```

### 10.1 MCP 错误重新思考

当 MCP 返回错误时，不能直接把任务标记为失败。错误应该进入 ReAct observation：

```text
工具调用失败
    ↓
记录错误类型、工具名和参数摘要
    ↓
返回 isError=true 的工具结果
    ↓
Agent 判断是否需要：
  - 重新获取页面快照
  - 修正元素引用
  - 等待更长时间
  - 更换操作路径
  - 请求用户确认
    ↓
继续下一轮 ReAct
```

建议错误分类：

| 错误类型 | 处理方式 |
| --- | --- |
| stale_reference | 重新 snapshot，禁止复用旧引用 |
| element_not_found | 重新 snapshot，尝试语义匹配 |
| page_not_ready | 使用 browser_wait_for，限制最大等待时间 |
| navigation_timeout | 有限重试一次，仍失败则请求用户处理 |
| permission_denied | 立即停止当前工具并记录审批结果 |
| server_unavailable | 重连一次，失败后结束 MCP 子任务 |
| invalid_arguments | 把 Schema 校验错误回传给模型修正 |
| external_side_effect_denied | 告知用户未执行，不自动绕过确认 |

## 11. 浏览器自动化设计

### 11.1 浏览器工具职责

第一期只开放以下能力：

| 工具 | 作用 | 默认风险 |
| --- | --- | --- |
| `browser_navigate` | 打开 URL | 可逆 |
| `browser_tabs` | 查看和切换标签页 | 只读 |
| `browser_snapshot` | 获取页面可访问性结构 | 只读 |
| `browser_click` | 点击页面元素 | 可逆 |
| `browser_type` | 输入文本 | 可逆 |
| `browser_wait_for` | 等待时间或页面文本状态 | 只读 |

后续再开放：

- 文件上传；
- 下载文件；
- 表单提交；
- 发送消息；
- 发布内容；
- 删除内容；
- GitHub 创建 PR。

### 11.2 标准浏览器操作循环

Agent 的系统提示词应增加以下约束：

```text
浏览器操作规则：
1. 操作前先获取当前页面快照。
2. 只能使用最新快照中的元素引用。
3. 点击或输入后必须等待页面状态变化。
4. 优先等待目标文本出现或加载文本消失。
5. 工具失败后必须重新获取页面快照。
6. 不得对同一个旧元素引用无限重试。
7. 提交、发送、删除、支付和发布前必须请求确认。
8. 任务结束前必须验证目标状态确实发生。
```

### 11.3 浏览器等待策略

等待优先级如下：

```text
文本出现 / 文本消失
        ↓
页面结构中目标元素出现
        ↓
标签页或 URL 状态变化
        ↓
固定时间等待
```

固定时间等待必须设置上限，例如单次不超过 30 秒，整个浏览器子任务不超过 5 分钟。这样可以避免 Agent 因为页面没有响应而一直卡在“进行中”。

### 11.4 浏览器会话模型

```text
Agent Run
  └── Browser Session
       ├── MCP Process
       ├── Browser Context
       ├── Active Page
       ├── Last Snapshot
       └── Cancellation Token
```

同一个 Agent Run 内需要复用同一个 Browser Session；不同 Run 默认使用不同 Session，避免任务之间串页或串登录状态。

会话必须支持：

- 创建；
- 获取当前标签页；
- 切换标签页；
- 停止；
- 超时关闭；
- 宿主机进程异常后的重连；
- 用户主动取消。

## 12. 权限与审批

### 12.1 风险等级

| 风险等级 | 典型操作 | 策略 |
| --- | --- | --- |
| readonly | snapshot、tabs、读取页面文本 | 自动执行 |
| reversible | navigate、click、type、wait | 默认自动执行 |
| external_effect | submit、send、publish、delete、pay | 必须确认 |
| credential_access | 读取 Cookie、Token、密码 | 禁止模型直接访问 |

“点击”本身可以自动执行，但点击“提交订单”“发布 PR”“发送消息”等会造成外部副作用的按钮前，必须根据页面语义或工具元数据请求确认。

### 12.2 审批状态机

```text
pending
  ├── approved → executing → completed
  ├── rejected → rejected
  └── expired  → expired
```

审批请求至少包含：

```json
{
  "approvalId": "approval-uuid",
  "runId": "run-uuid",
  "server": "playwright",
  "tool": "browser_click",
  "risk": "external_effect",
  "summary": "即将点击“提交订单”按钮",
  "argumentsPreview": {
    "element": "提交订单按钮"
  },
  "expiresAt": "2026-07-23T12:00:00Z"
}
```

敏感参数必须脱敏，不能在审批弹窗中展示密码、Cookie、Token 或完整信用卡信息。

### 12.3 凭据管理

- MCP 配置中不能直接提交 API Key；
- 凭据应保存在操作系统密钥环或本地密钥存储；
- 环境变量只允许按 MCP Server 白名单注入；
- Agent 消息、工具日志和长期记忆不得保存密码、Cookie、Token；
- 页面快照中的敏感字段应在写入日志和上下文前脱敏。

## 13. 后端 API 设计

以下接口为建议接口，第一期可以先实现基础接口，后续再补充更新和删除。

### 13.1 MCP Server 管理

```text
GET    /api/mcp/servers
POST   /api/mcp/servers
GET    /api/mcp/servers/{server_id}
PATCH  /api/mcp/servers/{server_id}
DELETE /api/mcp/servers/{server_id}
POST   /api/mcp/servers/{server_id}/connect
POST   /api/mcp/servers/{server_id}/disconnect
POST   /api/mcp/servers/{server_id}/reconnect
```

### 13.2 工具发现

```text
GET /api/mcp/servers/{server_id}/tools
GET /api/mcp/tools?scope=project&session_id={session_id}
```

返回示例：

```json
{
  "server": "playwright",
  "status": "ready",
  "tools": [
    {
      "name": "mcp__playwright__browser_click",
      "originalName": "browser_click",
      "description": "Click an element on the current page",
      "risk": "reversible",
      "enabled": true
    }
  ]
}
```

### 13.3 审批

```text
GET  /api/mcp/approvals/{approval_id}
POST /api/mcp/approvals/{approval_id}/approve
POST /api/mcp/approvals/{approval_id}/reject
```

### 13.4 浏览器会话

```text
GET  /api/browser/sessions
POST /api/browser/sessions
GET  /api/browser/sessions/{session_id}
POST /api/browser/sessions/{session_id}/stop
POST /api/browser/sessions/{session_id}/reset
```

浏览器会话不能只放在前端内存中，否则刷新页面或 Docker 重启后，后端无法判断 MCP 子进程是否仍然存在。会话状态应至少保存：

- `session_id`；
- `run_id`；
- `server_id`；
- `process_id` 或 Gateway 会话 ID；
- 当前状态；
- 创建时间；
- 最近心跳时间；
- 当前页面 URL 的脱敏版本；
- 是否使用持久化 Profile。

## 14. 事件协议

为了避免再次出现“工具已经完成但前端仍显示进行中”，所有 MCP 事件都必须使用明确的开始和结束事件。

### 14.1 事件类型

```text
mcp.server.starting
mcp.server.ready
mcp.server.failed
mcp.server.stopped
mcp.tools.discovered
mcp.tool.started
mcp.tool.waiting_approval
mcp.tool.approved
mcp.tool.rejected
mcp.tool.progress
mcp.tool.completed
mcp.tool.failed
mcp.session.created
mcp.session.closed
```

### 14.2 工具事件示例

```json
{
  "type": "mcp.tool.completed",
  "eventId": "event-uuid",
  "runId": "run-uuid",
  "toolCallId": "call-uuid",
  "server": "playwright",
  "tool": "browser_wait_for",
  "status": "completed",
  "durationMs": 1450,
  "isError": false,
  "resultPreview": "目标文本已出现",
  "createdAt": "2026-07-23T12:00:00Z"
}
```

### 14.3 前端状态要求

前端收到以下任一事件后都必须结束当前工具的 loading 状态：

- `mcp.tool.completed`；
- `mcp.tool.failed`；
- `mcp.tool.rejected`；
- `mcp.server.failed`；
- `mcp.session.closed`；
- Agent Run 被取消或超时。

不能只依赖最后一条文本消息判断工具是否结束。

## 15. 超时、取消和重试

### 15.1 超时层级

```text
工具调用超时：120 秒
浏览器等待超时：30 秒
单个浏览器子任务：5 分钟
MCP Server 启动超时：30 秒
整个 Agent Run：由任务预算决定
```

所有超时都必须转化为明确的终态事件，不能让任务永久停留在 `running`。

### 15.2 重试策略

只对可恢复错误重试：

- MCP Server 尚未 ready：等待并重试一次；
- 网络临时错误：指数退避重试一次；
- 页面尚未加载：重新等待一次；
- 页面元素引用过期：重新 snapshot 后重试一次。

不应自动重试：

- 用户拒绝审批；
- 参数校验失败且模型无法修正；
- 删除、发送、发布等副作用动作；
- 明确的权限不足；
- 目标页面要求用户输入密码或验证码。

### 15.3 取消传播

取消链路必须完整：

```text
用户点击取消
  ↓
前端发送 run.cancel
  ↓
FastAPI 设置 cancellation token
  ↓
MCP Gateway 停止等待
  ↓
Playwright MCP 中止当前调用
  ↓
关闭或复位 Browser Session
  ↓
发送 mcp.session.closed
```

## 16. 上下文压缩与 MCP 工具

MCP 工具定义和浏览器快照都可能占用大量 Token，因此需要遵循以下规则：

### 16.1 工具按需加载

不要把所有 MCP Server 的全部工具一次性传给模型。建议按照以下优先级筛选：

```text
当前任务明确需要的工具
    ↓
当前项目允许的工具
    ↓
当前 Agent 类型允许的工具
    ↓
工具白名单
    ↓
Token 预算限制
```

### 16.2 页面快照压缩

页面快照进入上下文前可以：

- 删除无关的装饰节点；
- 保留可交互元素和父级语义；
- 限制最大字符数；
- 对重复节点去重；
- 只保留当前标签页；
- 将旧快照替换成摘要，而不是无限累积。

### 16.3 工具结果压缩

工具结果应同时保存两份：

```text
原始结果：用于审计和调试
摘要结果：用于继续放入模型上下文
```

原始结果不能直接无限追加到下一轮上下文。摘要至少包含：

- 是否成功；
- 工具名称；
- 关键状态变化；
- 页面 URL；
- 目标文本是否出现；
- 错误类型；
- 是否需要下一步操作。

## 17. 与现有模块的对应关系

| 现有模块 | 本次变更 |
| --- | --- |
| `backend/app/agent.py` | 合并静态工具和动态 MCP 工具，路由 MCP 调用 |
| `backend/app/registry.py` | 保留内置工具，增加 MCP 工具适配入口 |
| `backend/app/main.py` | 增加 MCP Server、工具、审批和浏览器会话接口 |
| `backend/app/local_runner.py` | 可用于宿主机启动 MCP Gateway，仍负责本地脚本执行 |
| `backend/app/runner_server.py` | 保留本地 Runner 健康检查和进程托管能力 |
| `backend/app/context.py` | 增加 MCP 工具定义和页面快照压缩策略 |
| `backend/app/security.py` | 增加 MCP Server、网络和凭据边界检查 |
| `frontend/src/main.tsx` | 增加 MCP 服务面板、浏览器会话和审批状态 |
| `docs/agent-web/phase-07-mcp-memory.md` | 保留阶段概要，链接到本文档 |
| `docker-compose.yml` | 后端保持容器化，MCP Gateway 默认由宿主机运行 |

## 18. 分阶段实施计划

### Phase 1：MCP 基础设施

目标：先打通 MCP Server 的连接、发现和调用。

后端：

1. 增加 MCP 配置模型；
2. 增加 stdio 连接；
3. 实现 `initialize`；
4. 实现 `tools/list`；
5. 实现 `tools/call`；
6. 增加工具命名空间；
7. 增加调用超时和错误回填；
8. 增加 MCP 事件。

前端：

1. 展示 Server 列表；
2. 展示连接状态；
3. 展示工具列表；
4. 展示工具调用过程。

### Phase 2：Playwright MCP

目标：完成浏览器点击、输入和等待。

1. 宿主机启动 Playwright MCP；
2. 创建浏览器会话；
3. 接入 `browser_navigate`；
4. 接入 `browser_tabs`；
5. 接入 `browser_snapshot`；
6. 接入 `browser_click`；
7. 接入 `browser_type`；
8. 接入 `browser_wait_for`；
9. 增加页面状态校验；
10. 增加 stale reference 自动恢复。

### Phase 3：可靠性和审批

1. 增加高风险动作识别；
2. 增加审批弹窗；
3. 增加取消和超时；
4. 增加 MCP Server 重连；
5. 增加浏览器 Profile 隔离；
6. 增加敏感信息脱敏；
7. 增加调用审计记录。

### Phase 4：远程 MCP 与扩展生态

1. 增加 Streamable HTTP；
2. 增加 HTTP 认证和 Origin 校验；
3. 增加 GitHub MCP；
4. 增加 PR、Issue、仓库查询流程；
5. 增加项目级 MCP 配置界面；
6. 增加 MCP Server 安装和版本管理。

## 19. 测试方案

### 19.1 MCP Client 单元测试

- 正常完成 initialize；
- initialize 版本不兼容；
- tools/list 分页；
- 工具 Schema 缺失；
- tools/call 返回文本；
- tools/call 返回结构化数据；
- tools/call 返回 `isError=true`；
- MCP 子进程提前退出；
- 调用超时；
- 取消正在进行的调用。

### 19.2 Playwright 浏览器测试

1. 打开 TodoMVC 测试页面；
2. 等待页面标题出现；
3. 获取页面快照；
4. 输入一条 Todo；
5. 点击添加；
6. 等待 Todo 出现在列表；
7. 重新获取页面快照；
8. 读取最终列表内容。

### 19.3 错误恢复测试

- 使用过期元素引用；
- 页面加载延迟；
- 元素不存在；
- 页面导航超时；
- MCP Server 被关闭；
- 用户拒绝敏感操作；
- Agent Run 被主动取消；
- 浏览器窗口被用户关闭。

### 19.4 Docker 与宿主机测试

- Docker 后端正常启动；
- 宿主机 MCP Gateway 正常启动；
- 后端可以发现宿主机 MCP 工具；
- Playwright MCP 可以打开宿主机浏览器；
- Docker 重启不会导致配置丢失；
- Browser Session 断开后能显示明确错误；
- 不把用户浏览器 Profile 写入 Docker 容器。

## 20. 验收用例

### 用例一：基础网页操作

用户输入：

> 打开 `https://demo.playwright.dev/todomvc`，等待页面加载完成，添加一条“测试 MCP”，点击添加，等待它出现在列表中，最后告诉我列表里的内容。

期望结果：

```text
1. MCP Server 状态变为 ready
2. Agent 发现 Playwright 工具
3. 页面成功打开
4. Agent 获取页面快照
5. 找到输入框
6. 输入“测试 MCP”
7. 点击添加
8. 等待目标文本出现
9. 再次获取页面快照
10. 返回最终列表内容
```

### 用例二：错误自纠正

人为让 Agent 使用过期引用。

期望结果：

```text
browser_click 返回 stale_reference
  ↓
Agent 读取错误
  ↓
重新 browser_snapshot
  ↓
找到新引用
  ↓
再次点击
  ↓
完成任务
```

### 用例三：敏感操作审批

用户要求：

> 登录后台后发布这篇文章。

期望结果：

```text
打开后台、输入内容、预览：自动执行
点击“发布”：暂停并请求确认
用户拒绝：不执行发布，任务正常结束
```

### 用例四：工具调用超时

让页面持续处于加载状态。

期望结果：

- 页面等待在限定时间内结束；
- 前端显示“等待超时”；
- 工具状态变为 `failed` 或 `timeout`；
- Agent 可以给出下一步建议；
- 不出现永远停留在“进行中”的卡片。

## 21. 风险与应对

| 风险 | 影响 | 应对方案 |
| --- | --- | --- |
| MCP Server 版本变化 | 工具名称或参数变化 | 固定版本并启动时校验 Schema |
| 页面 DOM 变化 | 元素引用失效 | 操作前 snapshot，失败后重新 snapshot |
| 浏览器 Profile 冲突 | 会话串扰或启动失败 | 每个 Run 使用独立 Profile 或串行锁 |
| 远程 MCP 不可信 | 数据泄露或恶意工具 | 白名单、凭据隔离、人工确认 |
| 快照过大 | 上下文超限 | 页面快照裁剪和压缩 |
| 工具一直等待 | Agent 卡死 | 分层超时、取消传播、终态事件 |
| Docker 无法访问桌面 | 浏览器自动化失败 | 宿主机 MCP Gateway |
| 页面包含密码或 Token | 敏感信息泄露 | 快照和日志脱敏 |
| 重复点击副作用按钮 | 重复提交 | 幂等检查、动作前确认、成功状态验证 |

## 22. 完成标准

本次 MCP 浏览器自动化变更完成，需要同时满足：

- MCP Server 可以通过配置启动和停止；
- Agent 能动态发现 MCP 工具；
- MCP 工具能进入现有 ReAct 循环；
- MCP 错误能作为 observation 触发重新思考；
- Playwright MCP 能完成打开、快照、点击、输入和等待；
- 浏览器会话不会因为单次工具调用结束而立即丢失；
- 前端能展示完整的开始、等待、完成、失败和审批状态；
- 工具超时后不会无限显示进行中；
- 高风险操作有审批；
- 敏感信息不会写入上下文、长期记忆和普通日志；
- Docker 重启不影响 MCP 配置；
- 宿主机浏览器和 Docker 后端边界清晰；
- 单元测试、集成测试和浏览器验收用例全部通过。

## 23. 推荐的第一条测试提示词

```text
请使用浏览器工具完成以下任务：

1. 打开 https://demo.playwright.dev/todomvc。
2. 等待页面出现 Todo 输入框。
3. 输入“测试 MCP 浏览器自动化”。
4. 点击添加按钮。
5. 等待“测试 MCP 浏览器自动化”出现在 Todo 列表中。
6. 再次读取页面结构，确认任务确实添加成功。
7. 如果元素引用失效，请重新获取页面快照后再操作。
8. 不要执行删除、提交、发布或其他外部副作用操作。
```

## 24. 参考资料

- [Model Context Protocol：Server Concepts](https://modelcontextprotocol.io/docs/learn/server-concepts)
- [Model Context Protocol：Tools](https://modelcontextprotocol.io/specification/2025-03-26/server/tools)
- [Model Context Protocol：Transports](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports)
- [Anthropic MCP 文档](https://docs.anthropic.com/en/docs/mcp)
- [Microsoft Playwright MCP](https://github.com/microsoft/playwright-mcp)
- [OpenAI：Unlocking the Codex Harness](https://openai.com/index/unlocking-the-codex-harness/)
- [OpenAI：Running Codex safely](https://openai.com/index/running-codex-safely/)

## 25. 本次实际实现与验证记录

本节记录本次代码变更已经落地的内容，和仍然需要外部环境才能完成的部分，避免把设计目标误认为已经部署完成。

### 25.1 已落地的代码

| 模块 | 实际变更 |
| --- | --- |
| `backend/app/mcp/models.py` | 增加 MCP Server 配置、环境变量占位符解析、工具命名空间、风险级别、审批标记和安全公开字段。 |
| `backend/app/mcp/client.py` | 实现 stdio、Streamable HTTP 和兼容旧版 SSE 的 JSON-RPC Client；支持 initialize、tools/list 分页、tools/call、超时、错误和进程关闭。Windows 下 stdio 使用线程包装阻塞进程管道，兼容项目当前 SelectorEventLoop。 |
| `backend/app/agent.py` | 在每次 ReAct 执行前动态发现 MCP 工具，把 Schema 注入模型；MCP 调用结果作为 observation 回到模型；输出 `mcp.tool.started/completed/failed` 和审批事件。 |
| `backend/app/main.py` | 增加 Server 列表、连接、断开、刷新和工具列表 API；审批执行路径可调用 MCP 工具；拒绝审批后 Run 正确进入 stopped 状态。 |
| `frontend/src/main.tsx` | 增加 MCP 状态面板，展示 Server 状态、工具数量、实际工具列表、风险级别、审批标记、连接/断开/重连/刷新和错误；长期记忆支持查询、编辑、删除和审计；MCP 失败事件会结束对应的运行状态。 |
| `frontend/src/style.css` | 增加 MCP 面板、工具列表和响应式样式，同时保留运行中、完成、失败和停止状态图标。 |
| `backend/mcp.example.json` | 提供 Playwright stdio、Playwright 宿主机 SSE 和 GitHub Remote MCP Streamable HTTP 配置示例；GitHub 与宿主机模式默认关闭。 |
| `docker-compose.yml` | 为 backend 增加 `host.docker.internal` 网关映射和 GitHub Token 的显式环境变量入口。 |
| `.env.example` | 增加 MCP 开关、配置路径、审批和超时配置。 |

### 25.2 已完成的自动化验证

已执行并通过：

```text
前端类型检查：npm.cmd run typecheck
前端生产构建：npm.cmd run build
后端语法检查：python -m compileall -q backend/app backend/migrations
MCP 定向测试：包括 stdio、SSE、工具命名空间、错误结果、ReAct observation 和审批边界
后端完整测试：backend/tests 全量测试，59 passed
```

官方 Playwright MCP 真实实验已验证：

```text
1. npx 启动官方 @playwright/mcp
2. 自动发现 browser_navigate、browser_snapshot、browser_type、browser_click、browser_wait_for 等工具
3. 打开 Selenium Web Form 公开测试页
4. 获取页面快照并读取 target/ref
5. 向 Text input 输入 Huai Coder MCP
6. 点击 Submit
7. 页面跳转到 submitted-form
8. 再次快照确认 Form submitted / Received!
```

实验中还故意使用了错误的 `element/ref` 参数组合，得到官方 MCP 的参数错误；随后读取实际 Schema，改用快照返回的 `target` ref 后成功。这证明适配层能把 MCP 错误作为 observation 返回，而不是把任务永久卡在进行中。

### 25.2.1 本轮补齐的运行管理接口

在第一次实现基础连接后，本轮又补齐了文档中定义的生命周期接口：

```text
POST   /api/mcp/servers
GET    /api/mcp/servers/{server_id}
PATCH  /api/mcp/servers/{server_id}
DELETE /api/mcp/servers/{server_id}
POST   /api/mcp/servers/{server_id}/reconnect
GET    /api/mcp/servers/{server_id}/tools
GET    /api/mcp/approvals/{approval_id}
POST   /api/mcp/approvals/{approval_id}/approve
POST   /api/mcp/approvals/{approval_id}/reject
GET    /api/browser/sessions
POST   /api/browser/sessions
GET    /api/browser/sessions/{session_id}
POST   /api/browser/sessions/{session_id}/stop
POST   /api/browser/sessions/{session_id}/reset
POST   /api/runs/{run_id}/cancel
```

浏览器会话不再只依赖前端内存，`browser_sessions` 表会保留 Server、Run、进程/Gateway 会话标识、状态、脱敏 URL、Profile 模式、心跳和关闭时间。取消 Run 时会设置取消事件，ReAct 在下一安全检查点发出 `run.cancelled`；MCP 读取/等待类工具遇到连接断开或超时最多自动重试一次，创建 PR、发送、发布等副作用工具不会自动重放。

配置管理 API 默认只读，只有 `MCP_CONFIG_WRITE_ENABLED=true` 时才允许新增、修改或删除 Server；即使开启写入，也只允许环境变量使用 `${NAME}` 占位符，禁止把明文 Token 通过 API 写入 JSON。配置文件仍然可以由用户在本地安全编辑。Server、工具、浏览器会话和审批状态均返回明确终态，前端不会只依赖最后一条文本消息判断工具是否结束。

### 25.2.2 阶段七补齐：SDK、SubAgent 与资源配额

- `backend/pyproject.toml` 增加官方 `mcp` Python SDK 依赖。Server 配置设置 `client=auto` 或 `client=python_sdk` 时启用 SDK；默认使用已经过 Windows/Docker 双环境验证的内置 JSON-RPC 传输适配器，避免宿主机事件循环差异导致 stdio 卡住。两条路径仍复用同一套白名单、风险审批、超时、重试和审计层。Windows 宿主机已使用官方 SDK 适配器完成 fake MCP Server 的启动、`tools/list`、工具调用和结果回传验证。
- 子 Agent 通过 LangGraph 子图边界启动，父 Agent 的消息不会共享给子 Agent；explorer、planner、coder、tester 继续使用不可变的工具白名单。
- `SUBAGENT_MAX_PARALLEL` 限制进程级并发，`SUBAGENT_MAX_PER_RUN` 限制单次 Run 的子 Agent 数量，`SUBAGENT_QUEUE_TIMEOUT_SECONDS` 限制排队时间。超过限制时返回 `SUBAGENT_RESOURCE_LIMIT`，不会无限等待。
- 主 Agent 会持久化 `agent.started`、`agent.finished`、`agent.failed` 事件，前端执行面板可以看到子 Agent 名称、输入任务和最终结果。

本轮本地验收结果：后端 `59 passed`，MCP 专项 `13 passed`，SubAgent 专项 `5 passed`，记忆与审计专项 `11 passed`，前端 typecheck/build、Python compileall、路由检查和 Compose 配置检查均通过。新增了“已连接浏览器 MCP 时必须调用浏览器工具、未连接时明确提示配置缺失”的回归测试。官方 Playwright MCP 真实实验已完成初始化、工具发现、导航、快照、按 ref 输入、点击、等待和结果快照确认；GitHub MCP 使用本地协议仿真 Server 验证了工具发现、命名空间和只读仓库文件读取路径。

前端 MCP 面板现可展开每个已连接 Server 的实际工具列表，显示工具原名、风险级别、是否需要审批和工具描述；工具列表通过 `/api/mcp/servers/{server_id}/tools` 动态读取，不再只显示工具数量。

### 25.3 Docker 场景的边界

Docker backend 可以连接 MCP 的 HTTP/SSE endpoint，但不能直接执行 Windows 宿主机的 `npx.cmd`。因此有两种可用部署方式：

1. 后端在宿主机运行：使用 Playwright stdio，后端直接管理 `npx.cmd` 子进程。
2. 后端在 Docker 运行：宿主机单独启动 Playwright MCP `--port 8931 --host 0.0.0.0 --allowed-hosts "*"`，backend 使用 `playwright-host` 的 SSE URL 连接；Docker Compose 已加入 `host.docker.internal` 映射。由于 Docker 请求的 Host 头可能包含端口，使用通配符可避免误判为 `localhost:8931`；该服务只应在本机受控网络中使用。

GitHub Remote MCP 需要用户自己的 GitHub Token 和网络访问，不依赖 backend 容器内的 Docker Engine；如果选择本地 GitHub MCP stdio，才需要宿主机 Docker 或预构建二进制。代码不会生成、保存或回显 Token；没有 Token 或远程服务不可达时，Server 会显示 failed/disabled 状态，并把原因反馈给前端。

### 25.4 Docker 运行验收

此前在权限可用时已对正在运行的 Docker Compose 环境完成运行时验收；本轮源码改动继续通过宿主机测试和 Compose 配置检查，但由于当前执行策略拒绝再次执行 Docker Engine 命令，本轮不宣称已用最新源码重建镜像：

```powershell
docker compose config
docker compose up -d --build
docker compose exec -T backend python -m compileall -q /app/app /app/migrations
docker compose exec -T backend pytest -q /workspace/backend/tests
Invoke-RestMethod http://localhost:8000/health
```

结果：此前 Docker Engine 可访问，`backend`、`db`、`frontend` 容器均处于运行状态；容器内编译和测试 `55 passed`，健康检查返回 `status=ok`。本轮 `docker compose config --quiet` 通过，最新源码的宿主机全量测试为 `59 passed`。本次复验没有执行 GitHub 远程联调，也没有在本轮重复构建镜像。

GitHub MCP 的真实远程调用也没有使用虚构 Token 进行冒险测试；配置、密钥隐藏、读取工具白名单和高风险工具审批边界已覆盖，真实仓库查询/创建 PR 需要用户在本地注入 Token 后再执行。
### 25.5 本轮补齐：记忆审计与 SubAgent 拓扑

为满足阶段七的“记忆查询、删除和审计”以及前端 SubAgent 运行拓扑要求，本轮新增：

```text
GET /api/memories/{memory_id}/audit
GET /api/projects/{project_id}/memories/audit?include_session=true
GET /api/subagents
```

记忆删除仍然是软删除，`memory_audits` 会记录创建、更新和删除前后的内容、原因、来源 Run 与时间；前端按“当前会话 / 当前项目 / 用户记忆”分层展示，并可查看单条记忆的审计记录。`/api/subagents` 返回 LangGraph 根 Agent 到子图的边界、四个角色、工具白名单和审批要求；对话执行流继续展示每个子 Agent 的开始、完成或失败详情。
### 25.6 现场验收脚本

新增 `backend/app/mcp_smoke.py` 作为部署后的只读验收探针：

```powershell
docker compose exec -T backend python -m app.mcp_smoke --config /workspace/backend/mcp.json --github
docker compose exec -T backend python -m app.mcp_smoke --config /workspace/backend/mcp.json --browser
```

脚本只从已发现工具中选择 `get_me`、`search_repositories`、`get_file_contents`、`browser_tabs` 或 `browser_snapshot` 等低风险工具；发现高风险工具时不会调用，失败时返回非零退出码。这样可以把“Server 已启动、工具已发现、调用结果可返回、安全审批未被绕过”一次性作为部署验收条件。
### 25.7 项目适配层真实 Playwright 验证

在宿主机使用官方 `@playwright/mcp` stdio Server，并通过项目自身 `McpManager`（不是直接调用官方进程）完成了以下验证：

```text
发现工具：browser_navigate、browser_snapshot、browser_tabs、browser_wait_for
browser_tabs(action=list)：成功，返回 about:blank 当前标签页
browser_navigate：成功打开 Selenium Web Form
browser_snapshot：成功返回页面快照，包含 Text input 和 ref=e11
```

这证明真实 MCP Server → 项目传输适配器 → 命名空间工具 → 工具结果回传的链路已经打通。GitHub 仍需在用户环境注入 Token 并启动 Docker Server 后，使用 25.6 的只读探针完成同样的远程验证。

### 25.9 权限恢复后的完整浏览器复验

在获得 Docker 和宿主机浏览器缓存访问权限后，使用项目自己的 `McpManager` 完成了完整浏览器操作闭环：

```text
发现工具：browser_type、browser_navigate、browser_snapshot、browser_click、browser_tabs、browser_wait_for
打开页面：https://www.selenium.dev/selenium/web/web-form.html
快照定位：输入框 ref=e11，提交按钮 ref=e40
输入：Huai Coder MCP
点击：提交按钮
等待：Form submitted
最终校验：页面包含 Form submitted / Received!
```

该结果证明浏览器 MCP 不只是能够发现工具，还能在同一 MCP 会话中持续完成导航、快照、输入、点击、等待和最终状态验证。
### 25.8 GitHub Remote MCP 与 Docker 边界修正

审计 Compose 部署时发现，backend 容器不能可靠地通过 `docker run` 启动 GitHub MCP：这会要求容器内部额外安装 Docker CLI 并访问宿主 Docker Socket。示例配置已改为 GitHub 官方 Remote MCP 的 Streamable HTTP 入口：

```json
{
  "enabled": true,
  "transport": "streamable_http",
  "url": "https://api.githubcopilot.com/mcp/",
  "headers": {
    "Authorization": "Bearer ${GITHUB_PERSONAL_ACCESS_TOKEN}"
  }
}
```

MCP Client 现在支持配置 Header，并在公开状态中只返回 Header 名称，不返回值；API 写入配置时，Authorization、Token、Secret 和 API-Key Header 必须使用环境变量占位符。这样 Docker backend 不需要 Docker Socket，GitHub 工具仍然复用同一套白名单、风险分类、审批和审计逻辑。
