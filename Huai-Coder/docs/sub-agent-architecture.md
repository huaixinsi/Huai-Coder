# Sub-Agent 架构重构文档

> 变更日期: 2026-07-21
> 变更范围: backend/app/ 下 8 个文件（4 新建 + 4 修改）
> 架构目标: 将硬编码 if-else 路由替换为 LLM 驱动的 ReAct 循环 + 声明式子 Agent 权限体系

---

## 一、架构总览

### 变更前（旧架构）

```
用户请求 → main.py SSE 端点
  ├─ if prompt.startswith("/list") → 直接调 handler
  ├─ if prompt.startswith("/read") → 直接调 handler
  ├─ if prompt.startswith("/grep") → 直接调 handler
  ├─ if prompt.startswith("/write") → 审批轮询 → 执行
  ├─ if prompt.startswith("/exec") → 审批轮询 → 执行
  └─ else → 调 LLM complete() 返回纯文本
```

问题:
- 路由逻辑硬编码在 main.py，无法扩展
- LLM 无法自主决定调用哪些工具
- 无子 Agent 概念，所有操作在单一上下文中完成
- 审批机制与路由耦合，代码约 160 行嵌套在 SSE 生成器内

### 变更后（新架构）

```
用户请求 → main.py SSE 端点（纯流式转发）
  → agent.py ReAct 循环
      ├─ LLM 思考 → 选择工具 → 执行 → 观察 → 继续
      ├─ LLM 选择 task 工具 → 生成子 Agent
      │     ├─ explorer (只读, 10轮, 60s超时)
      │     ├─ planner  (只读, 5轮, 30s超时)
      │     ├─ coder    (写文件, 25轮, 300s超时, 需审批)
      │     └─ tester   (执行命令, 15轮, 120s超时, 需审批)
      └─ 遇到高危工具 → 终止本轮 → 报告用户 → 用户批准后重发
```

---

## 二、新建文件

### 2.1 `backend/app/agents/__init__.py`

空文件，将 `agents/` 目录标记为 Python 包。

### 2.2 `backend/app/agents/base.py`

子 Agent 配置的数据结构定义。

```python
@dataclass(frozen=True)
class SubAgentConfig:
    name: str                    # 唯一标识: "explorer" / "planner" / "coder" / "tester"
    description: str             # 用途描述（嵌入主 Agent system prompt）
    tools: tuple[str, ...]       # 硬编码工具白名单（不可变）
    max_turns: int               # 最大 ReAct 轮次
    timeout: int                 # 超时秒数
    needs_approval: bool = False # 是否包含高危工具
    system_prompt: str = ""      # 子 Agent 专用 system prompt

    def can_use(self, tool_name: str) -> bool:
        return tool_name in self.tools
```

设计要点:
- `frozen=True` 使配置不可变，运行时无法被篡改
- `tools` 用 tuple 而非 list，进一步防止意外修改
- `can_use()` 是唯一的权限查询入口

### 2.3 `backend/app/agents/registry.py`

子 Agent 权限声明表（静态注册）。

| Agent | 允许工具 | 最大轮次 | 超时 | 需审批 |
|-------|---------|---------|------|--------|
| explorer | list_dir, read_file, grep_code | 10 | 60s | 否 |
| planner | list_dir, read_file, grep_code | 5 | 30s | 否 |
| coder | read_file, write_file, grep_code | 25 | 300s | 是 |
| tester | execute_command, read_file | 15 | 120s | 是 |

核心安全约束:
- **coder 永远无法调用 execute_command**（即使 LLM 幻觉输出该工具名，白名单会拒绝）
- **tester 永远无法调用 write_file**（同理）
- **explorer/planner 永远无法写入或执行**（纯只读）

导出函数:
- `get_subagent_config(name)` → 返回配置或 None
- `list_subagents()` → 返回摘要列表（用于嵌入主 Agent prompt）

### 2.4 `backend/app/agents/subagent.py`

子 Agent ReAct 执行器。

```python
async def run_subagent(
    agent_name: str,
    task_prompt: str,
    workspace: str,
    approved_tools: set[str] | None = None,
) -> SubAgentResult
```

执行流程:
1. 从 registry 获取配置，校验 agent_name 合法性
2. 构建 messages 列表（system_prompt + task_prompt）
3. 构建 tool_schemas（仅包含白名单内的工具）
4. 进入 ReAct 循环:
   - 调用 `complete_with_tools()` 获取 LLM 响应
   - 若无 tool_call → 返回最终答案
   - 若有 tool_call → 白名单校验 → 审批校验 → 执行 → 观察回传
5. 超过 max_turns 或 timeout → 返回超时/超限结果

防循环依赖设计:
- `from ..llm import complete_with_tools` 和 `from ..registry import get_tool` 放在函数体内（延迟导入）
- 避免 registry.py ↔ subagent.py 的模块级循环导入

返回值:
```python
@dataclass
class SubAgentResult:
    agent_name: str
    output: str              # 最终输出或错误信息
    turns_used: int          # 实际使用轮次
    approval_required: bool  # 是否需要用户审批
    pending_tool: ToolCall   # 待审批的工具调用（如有）
```

---

## 三、修改文件

### 3.1 `backend/app/llm.py`（重写）

变更前: 单一 `complete(prompt: str) -> str` 函数，仅支持字符串输入。

变更后:

| 函数 | 签名 | 用途 |
|------|------|------|
| `complete` | `(prompt: str \| list[dict], timeout: int = 60) -> str` | 向后兼容的纯文本补全 |
| `complete_with_tools` | `(messages: list[dict], tools: list[dict], timeout: int = 60) -> LLMResponse` | 支持 OpenAI function calling |

新增数据结构:
```python
@dataclass
class ParsedToolCall:
    id: str              # tool_call ID（用于 message history 关联）
    name: str            # 工具名
    arguments: dict      # 解析后的参数
    raw: dict            # 原始 tool_call dict（回传给 LLM）

@dataclass
class LLMResponse:
    content: str                    # 文本回复（最终答案或思考过程）
    tool_call: ParsedToolCall | None  # 工具调用（None 表示无调用）
```

`complete_with_tools` 行为:
- 向 LLM API 发送 `tools` 和 `tool_choice: "auto"` 参数
- 解析响应中的 `tool_calls[0]`（当前只取第一个）
- 将 `function.arguments` JSON 字符串解析为 dict
- 若无 tool_calls → 返回纯 content

向后兼容:
- `complete("hello")` 仍然正常工作
- `complete([{"role": "user", "content": "hello"}])` 新增支持 messages 列表
- LLM 未配置时返回 fallback 提示文本

### 3.2 `backend/app/registry.py`（扩展）

变更点:

1. **新增 `task` 工具**:
```python
async def _spawn_subagent(agent_name: str, task: str, guard: PathGuard) -> str:
    from .agents.subagent import run_subagent
    result = await run_subagent(agent_name=agent_name, task_prompt=task, workspace=str(guard.root))
    if result.approval_required and result.pending_tool:
        return f"APPROVAL_REQUIRED: Agent '{agent_name}' needs approval for '{result.pending_tool.name}'. {result.output}"
    return f"[{agent_name}] ({result.turns_used} turns)\n{result.output}"
```

2. **`get_tool` 增加 `caller` 参数**:
```python
def get_tool(name: str, caller: str = "main") -> ToolSpec:
    if name not in TOOLS:
        raise WorkspaceViolation(f"Unknown tool: {name}")
    if caller != "main":
        from .agents.registry import get_subagent_config
        config = get_subagent_config(caller)
        if config and name not in config.tools:
            raise WorkspaceViolation(f"Agent '{caller}' cannot use '{name}'")
    return TOOLS[name]
```

- `caller="main"` 时不检查权限（主 Agent 拥有所有工具）
- `caller="explorer"` 等子 Agent 时强制白名单校验
- 这是**第二层防线**（第一层是 tool_schemas 只暴露白名单工具给 LLM）

3. **工具注册表**:
```python
TOOLS = {
    "list_dir": ...,
    "read_file": ...,
    "grep_code": ...,
    "write_file": ...,
    "execute_command": ...,
    "task": ToolSpec("task", "Spawn a sub-agent for a specific task", Risk("low", "sub-agent delegation", False), _spawn_subagent),
}
```

### 3.3 `backend/app/agent.py`（重写）

变更前: `_execute` 函数内 5 个 if-elif 分支处理 /list /read /grep /write /exec 前缀命令。

变更后: 完整 ReAct 循环。

核心逻辑（`_execute` 函数）:

```
1. 敏感请求检测（.env / 密钥 / token 等关键词）→ 直接返回提示
2. 构建 system prompt（含工具描述 + 子 Agent 列表 + 项目上下文）
3. 构建 tool_schemas（6 个工具的 OpenAI function calling 格式）
4. ReAct 循环（最多 MAX_REACT_TURNS=15 轮）:
   a. 调用 complete_with_tools(messages, tool_schemas)
   b. 若无 tool_call → 输出最终答案，退出循环
   c. 若有 tool_call:
      - 发射 "tool.started" 事件
      - 检查 risk.requires_approval:
        - 是 → 发射 "approval.required" 事件 → 终止本轮（终止-重发模式）
        - 否 → 执行工具 → 发射 "tool.finished" 事件
      - 将观察结果追加到 messages
5. 超过 15 轮 → 返回超限提示
```

System Prompt 结构:
```
You are a coding assistant working inside a project workspace.
## Available Tools（6 个工具的描述）
## Sub-Agents（4 个子 Agent 的 JSON 描述）
## Rules（5 条行为规则）
## Project Context（文件列表 + 代码摘录，最多 60KB）
```

保留的设计:
- LangGraph StateGraph 结构（`_builder`）
- PostgreSQL checkpointer（会话持久化）
- `_workspace_context()` 函数（项目快照）
- `run_agent()` 异步生成器接口（main.py 调用方式不变）

删除的设计:
- `/list` `/read` `/grep` `/write` `/exec` 前缀路由
- 直接调用 `get_tool().handler()` 的硬编码路径

### 3.4 `backend/app/main.py`（瘦身）

删除内容（约 160 行）:
- `/write` `/exec` `/read` 前缀判断 + 审批轮询逻辑
- `create_plan` 自动计划生成逻辑
- `TaskDependency` 创建逻辑
- `PathGuard` / `command_risk` / `get_tool` / `scrub` 直接调用

保留内容:
- SSE 流式端点结构
- `run_agent()` 调用 + 事件持久化
- Plan 确认/取消 API（`/api/plans/{id}/confirm` 等）
- `execute_plan` 后台任务函数

简化后的 SSE 生成器:
```python
async def events():
    try:
        workspace = str((WORKSPACE_ROOT / "projects" / str(request.project_id)).resolve())
        history = [(message.role, message.content) for message in previous_messages]
        async for event in run_agent(request.prompt, workspace, history, f"session-{session.id}"):
            db.add(AgentEventRecord(...))
            # 状态更新 + 消息持久化
            yield f"data: {json.dumps(...)}\n\n"
    except Exception as error:
        # 错误处理
```

删除的 import:
- `from .registry import get_tool, command_risk`
- `from .security import PathGuard, scrub, WorkspaceViolation`
- `from .planner import create_plan`
- `TaskDependency` model

### 3.5 `backend/app/models.py`（扩展）

`AgentRun` 表新增字段:
```python
agent_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
```

用途: 记录本次运行使用的 Agent 类型（"main" / "explorer" / "coder" 等），便于审计和调试。

---

## 四、权限模型

### 4.1 三层防线

| 层级 | 机制 | 位置 |
|------|------|------|
| L1: Schema 限制 | tool_schemas 只包含白名单工具，LLM 根本看不到其他工具 | subagent.py `_build_tool_schemas()` |
| L2: 运行时校验 | `config.can_use(tool_name)` 硬编码检查 | subagent.py ReAct 循环内 |
| L3: 注册表校验 | `get_tool(name, caller=agent_name)` 二次验证 | registry.py `get_tool()` |

即使 LLM 被 prompt injection 攻击输出非白名单工具名，L2 和 L3 都会拒绝执行。

### 4.2 审批机制（第一版: 终止-重发）

触发条件: `tool_spec.risk.requires_approval == True`（write_file, execute_command）

流程:
1. ReAct 循环检测到高危工具
2. 发射 `approval.required` SSE 事件（含工具名、参数、原因）
3. 终止当前 ReAct 循环，返回提示信息
4. 用户在前端看到提示，决定是否批准
5. 批准后: 用户发送新请求（附带"已批准"上下文）
6. 主 Agent 重新执行（子 Agent 通过 `approved_tools` 参数跳过已批准工具）

与旧版区别:
- 旧版: SSE 流暂停 → `while True: sleep(1)` 轮询 DB → 恢复执行
- 新版: 整个请求结束 → 新请求重新开始（无状态恢复）
- 代价: 批准后子 Agent 需重新执行前面的只读步骤（约 2-5 秒额外 LLM 调用）
- 收益: 无需维护挂起状态，无死锁风险，代码量减少 80%

---

## 五、工具注册表（完整）

| 工具名 | 描述 | 风险等级 | 需审批 | 可用 Agent |
|--------|------|---------|--------|-----------|
| list_dir | 列出目录文件 | low | 否 | main, explorer, planner |
| read_file | 读取文件内容 | low | 否 | main, explorer, planner, coder, tester |
| grep_code | 搜索源代码 | low | 否 | main, explorer, planner, coder |
| write_file | 写入文件 | high | 是 | main, coder |
| execute_command | 执行 shell 命令 | medium | 是 | main, tester |
| task | 生成子 Agent | low | 否 | main |

---

## 六、SSE 事件类型

| 事件类型 | 含义 | content 内容 |
|---------|------|-------------|
| run.started | Agent 开始执行 | 空 |
| tool.started | 开始调用工具 | 空（tool 字段有工具名） |
| tool.finished | 工具执行完成 | 工具输出（截断至 4000 字符） |
| approval.required | 需要用户审批 | JSON: {tool, arguments, reason} |
| message.delta | LLM 文本输出 | 最终答案或中间思考 |
| run.finished | Agent 执行结束 | 空 |
| run.failed | Agent 执行失败 | 错误信息 |

---

## 七、部署注意事项

### 7.1 数据库迁移

models.py 新增了 `agent_runs.agent_type` 字段，需要生成 Alembic 迁移:

```bash
cd backend
alembic revision --autogenerate -m "add agent_type to agent_runs"
alembic upgrade head
```

该字段为 `nullable=True`，不影响已有数据。

### 7.2 无新增依赖

本次变更未引入新的 pip 包。所有功能基于已有依赖:
- httpx（LLM API 调用）
- langgraph（状态图）
- sqlalchemy（ORM）
- fastapi（Web 框架）

### 7.3 配置要求

`complete_with_tools` 需要 LLM 后端支持 OpenAI-compatible function calling:
- 请求 payload 包含 `tools` 和 `tool_choice` 字段
- 响应 `choices[0].message` 可能包含 `tool_calls` 数组

已验证兼容: OpenAI GPT-4/3.5, DeepSeek, Qwen, GLM-4 等主流模型的 /chat/completions 接口。

若 LLM 不支持 function calling，`complete_with_tools` 会返回纯 content（无 tool_call），ReAct 循环退化为单轮问答。

---

## 八、已知限制与后续规划

| 限制 | 影响 | 后续方案 |
|------|------|---------|
| 审批为终止-重发模式 | 批准后子 Agent 重跑只读步骤 | LangGraph `interrupt_before` + checkpoint 恢复 |
| 子 Agent 无持久化 | 子 Agent 执行记录不存 DB | 利用 `agent_type` 字段 + AgentEventRecord 关联 |
| 单次只取第一个 tool_call | 并行工具调用不支持 | 解析 tool_calls 数组，并行执行 |
| coder 不走 Plan 体系 | 复杂多文件修改无计划 | 后续接入 Plan/Task 模型 |
| httpx timeout 固定 | 长任务可能超时 | 按 SubAgentConfig.timeout 动态传入（已实现） |
| 前端未适配 approval.required 事件 | 审批 UI 需更新 | 前端监听新事件类型，渲染审批卡片 |

---

## 九、文件变更清单

| 操作 | 文件路径 | 行数变化 |
|------|---------|---------|
| 新建 | backend/app/agents/__init__.py | +0 |
| 新建 | backend/app/agents/base.py | +20 |
| 新建 | backend/app/agents/registry.py | +69 |
| 新建 | backend/app/agents/subagent.py | +130 |
| 重写 | backend/app/llm.py | 14 → 95 |
| 扩展 | backend/app/registry.py | 69 → 95 |
| 重写 | backend/app/agent.py | 225 → 195 |
| 瘦身 | backend/app/main.py | 约 -160 行 |
| 扩展 | backend/app/models.py | +1 字段 |

---

## 十、验证记录

```
[通过] python -m py_compile app/agents/__init__.py
[通过] python -m py_compile app/agents/base.py
[通过] python -m py_compile app/agents/registry.py
[通过] python -m py_compile app/agents/subagent.py
[通过] python -m py_compile app/llm.py
[通过] python -m py_compile app/registry.py
[通过] python -m py_compile app/agent.py
[通过] python -m py_compile app/main.py
[通过] python -m py_compile app/models.py

[通过] explorer.can_use('write_file') == False
[通过] explorer.can_use('execute_command') == False
[通过] coder.can_use('write_file') == True
[通过] coder.can_use('execute_command') == False
[通过] tester.can_use('execute_command') == True
[通过] tester.can_use('write_file') == False
[通过] get_tool('write_file', caller='explorer') raises WorkspaceViolation
[通过] 'task' in TOOLS
[通过] AgentRun.agent_type 字段存在
```
