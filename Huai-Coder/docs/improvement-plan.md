# Huai-Coder 架构改进计划

> 核心原则：主 Agent 自由调度，子 Agent 权限锁死。自由在调度层，约束在工具层。

---

## 一、现状分析

### 当前架构问题

| 问题 | 位置 | 影响 |
|------|------|------|
| 路由逻辑硬编码在 HTTP 层 | main.py L142-202 | 无法复用，新增模式必须改 main.py |
| Plan 执行只调 LLM 不操作文件 | main.py L283 | 计划确认后无法真正修改代码 |
| Agent 只有单节点状态图 | agent.py L73-76 | 无法多轮工具调用 |
| 工具调用无权限分级 | registry.py | 所有工具同等暴露，无隔离 |
| 无子 Agent 机制 | 全局 | 无法并行处理、无法按角色分工 |
| 无轮次/超时限制 | 全局 | Agent 可能无限循环 |

### 现有可复用资产

| 资产 | 文件 | 复用方式 |
|------|------|---------|
| 工具注册表 | registry.py | 不变，作为共享工具池 |
| 路径安全守卫 | security.py | 不变，工具执行层统一检查 |
| 计划生成与校验 | planner.py | 成为 planner 子 Agent 核心逻辑 |
| 任务调度执行 | executor.py | 成为 coder 子 Agent 的调度器 |
| 审批机制 | main.py Approval 逻辑 | 抽取为独立模块 |
| 审计日志 | models.py AuditLog | 不变，所有子 Agent 操作写入 |
| LangGraph checkpoint | agent.py | 主 Agent 继续使用 |

---

## 二、目标架构

```
用户输入
    │
    ▼
┌─────────────────────────────────────────────┐
│  主 Agent（改造后的 agent.py）                │
│  - 读取项目上下文（CLAUDE.md 模式）           │
│  - 带完整工具列表 + 子 Agent 清单             │
│  - LLM 自主决策：直接调工具 or spawn 子 Agent │
└──────────────────┬──────────────────────────┘
                   │
    ┌──────────────┼──────────────┬──────────────┐
    ▼              ▼              ▼              ▼
┌────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
│explorer│  │ planner  │  │  coder   │  │  tester  │
│只读    │  │只读      │  │可写      │  │可执行    │
│10轮    │  │5轮       │  │25轮      │  │15轮      │
│60s     │  │30s       │  │300s      │  │120s      │
└────────┘  └──────────┘  └──────────┘  └──────────┘
    │              │              │              │
    └──────────────┴──────────────┴──────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│  约束层（四层枷锁）                           │
│  1. 工具权限隔离（声明时锁死）                 │
│  2. 审批插桩（写/执行操作需确认）              │
│  3. 审计追踪（所有操作不可篡改记录）           │
│  4. 超时与轮次限制（防失控循环）               │
└─────────────────────────────────────────────┘
```

---

## 三、分阶段实施计划

### Phase 1：基础设施（预计 1 天）

**目标**：建立子 Agent 声明和调度的基础协议。

#### 1.1 新建 `backend/app/agents/__init__.py`

空文件，标记为 Python 包。

#### 1.2 新建 `backend/app/agents/base.py`

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator

@dataclass
class SubAgentConfig:
    """子 Agent 声明式配置"""
    name: str
    description: str
    tools: list[str]           # 允许的工具白名单（硬约束）
    system_prompt: str
    max_turns: int = 10        # 最大推理轮次
    timeout: int = 60          # 超时秒数
    risk_level: str = "low"    # low / medium / high
    needs_approval: bool = False  # 工具调用是否需要审批

@dataclass
class AgentEvent:
    """Agent 事件（SSE 推送）"""
    type: str                  # agent.started / tool.called / agent.finished / error
    content: str = ""
    tool: str | None = None
    agent_name: str | None = None

class BaseSubAgent(ABC):
    """子 Agent 基类"""

    def __init__(self, config: SubAgentConfig):
        self.config = config
        self.turn_count = 0

    @abstractmethod
    async def run(self, task: str, workspace: str,
                  context: str) -> AsyncIterator[AgentEvent]:
        """执行任务，产出事件流"""
        ...

    def can_use_tool(self, tool_name: str) -> bool:
        """权限检查：该子 Agent 是否有权使用此工具"""
        return tool_name in self.config.tools

    def check_limits(self) -> None:
        """轮次限制检查"""
        self.turn_count += 1
        if self.turn_count > self.config.max_turns:
            raise RuntimeError(
                f"Agent {self.config.name} exceeded max turns ({self.config.max_turns})"
            )
```

#### 1.3 新建 `backend/app/agents/registry.py`（子 Agent 注册表）

```python
from .base import SubAgentConfig

# 子 Agent 声明（权限在此锁死）
SUBAGENT_CONFIGS: dict[str, SubAgentConfig] = {
    "explorer": SubAgentConfig(
        name="explorer",
        description="代码探索专家，只读分析项目结构和代码内容",
        tools=["list_dir", "read_file", "grep_code"],
        system_prompt="你是代码探索专家。只负责读取和分析代码，不做任何修改。输出简洁的分析结果。",
        max_turns=10,
        timeout=60,
        risk_level="low",
        needs_approval=False,
    ),
    "planner": SubAgentConfig(
        name="planner",
        description="计划制定专家，分析任务并输出结构化执行计划",
        tools=["list_dir", "read_file", "grep_code"],
        system_prompt="你是计划制定专家。分析用户任务和项目代码，输出 JSON 格式的执行计划。",
        max_turns=5,
        timeout=30,
        risk_level="low",
        needs_approval=False,
    ),
    "coder": SubAgentConfig(
        name="coder",
        description="开发专家，负责编写和修改代码文件",
        tools=["read_file", "write_file", "grep_code"],
        system_prompt="你是开发专家。根据计划执行代码编写和修改任务。",
        max_turns=25,
        timeout=300,
        risk_level="medium",
        needs_approval=True,  # 写文件需要审批
    ),
    "tester": SubAgentConfig(
        name="tester",
        description="测试专家，负责运行测试和验证结果",
        tools=["execute_command", "read_file"],
        system_prompt="你是测试专家。运行测试命令，分析输出，报告通过/失败及原因。",
        max_turns=15,
        timeout=120,
        risk_level="high",
        needs_approval=True,  # 执行命令需要审批
    ),
}

def get_subagent_config(name: str) -> SubAgentConfig | None:
    return SUBAGENT_CONFIGS.get(name)

def list_subagents() -> list[dict]:
    """返回子 Agent 清单（供主 Agent system prompt 使用）"""
    return [
        {"name": c.name, "description": c.description, "tools": c.tools}
        for c in SUBAGENT_CONFIGS.values()
    ]
```

#### 1.4 验证

- [ ] `python -c "from app.agents.base import BaseSubAgent, SubAgentConfig"` 无报错
- [ ] `python -c "from app.agents.registry import SUBAGENT_CONFIGS; print(len(SUBAGENT_CONFIGS))"` 输出 4

---

### Phase 2：子 Agent 实现（预计 2 天）

**目标**：实现四个子 Agent 的执行逻辑。

#### 2.1 新建 `backend/app/agents/subagent.py`（通用子 Agent 执行器）

```python
import asyncio
import json
from typing import AsyncIterator
from .base import BaseSubAgent, SubAgentConfig, AgentEvent
from ..llm import complete
from ..registry import get_tool
from ..security import PathGuard

class SubAgent(BaseSubAgent):
    """通用子 Agent：LLM 驱动 + 工具调用循环"""

    async def run(self, task: str, workspace: str,
                  context: str) -> AsyncIterator[AgentEvent]:
        guard = PathGuard(workspace)
        messages = [
            {"role": "system", "content": self.config.system_prompt},
            {"role": "user", "content": f"项目上下文:\n{context}\n\n任务:\n{task}"},
        ]

        yield AgentEvent("agent.started", agent_name=self.config.name)

        try:
            result = await asyncio.wait_for(
                self._execute_loop(messages, guard),
                timeout=self.config.timeout,
            )
            yield AgentEvent("agent.finished", result, agent_name=self.config.name)
        except asyncio.TimeoutError:
            yield AgentEvent("error", f"Agent {self.config.name} timed out after {self.config.timeout}s",
                           agent_name=self.config.name)
        except RuntimeError as e:
            yield AgentEvent("error", str(e), agent_name=self.config.name)

    async def _execute_loop(self, messages: list[dict],
                           guard: PathGuard) -> str:
        """ReAct 循环：思考 -> 调工具 -> 观察 -> 重复"""
        for _ in range(self.config.max_turns):
            self.check_limits()

            # 1. LLM 思考
            response = await complete(
                json.dumps(messages, ensure_ascii=False)
            )

            # 2. 解析工具调用（简化版：检查是否包含工具调用标记）
            tool_call = self._parse_tool_call(response)
            if tool_call is None:
                # 无工具调用，返回最终结果
                return response

            tool_name, tool_args = tool_call

            # 3. 权限检查（硬约束）
            if not self.can_use_tool(tool_name):
                messages.append({
                    "role": "assistant",
                    "content": f"错误：无权使用工具 {tool_name}"
                })
                continue

            # 4. 执行工具
            tool = get_tool(tool_name)
            try:
                if tool_name == "execute_command":
                    result = await tool.handler(tool_args.get("command", ""), guard)
                elif tool_name == "write_file":
                    result = tool.handler(
                        tool_args.get("path", ""),
                        tool_args.get("content", ""),
                        guard
                    )
                else:
                    result = tool.handler(tool_args.get("path", "."), guard)
            except Exception as e:
                result = f"工具执行失败: {e}"

            # 5. 观察结果，加入上下文
            messages.append({"role": "assistant", "content": response})
            messages.append({"role": "user", "content": f"工具结果:\n{result}"})

        return "达到最大轮次限制，任务未完成"

    def _parse_tool_call(self, response: str) -> tuple[str, dict] | None:
        """从 LLM 输出中解析工具调用。
        
        支持的格式（按优先级）：
        1. JSON 块: {"tool": "read_file", "args": {"path": "config.py"}}
        2. 嵌套 JSON: ```json\n{"tool": "...", "args": {...}}\n```
        3. 无工具调用标记 -> 返回 None（视为最终回答）
        
        实现说明：
        - 先尝试匹配 ```json 代码块中的 JSON
        - 再尝试匹配裸 {"tool" 开头的 JSON
        - 解析失败时返回 None，不中断流程
        - 如果后续接入 OpenAI function calling 格式，
          在此方法中增加对 tool_calls 数组的解析即可
        """
        import re
        # 优先匹配 ```json 代码块
        code_block = re.search(r'```json\s*(\{.*?\})\s*```', response, re.DOTALL)
        if code_block:
            try:
                data = json.loads(code_block.group(1))
                if "tool" in data:
                    return data["tool"], data.get("args", {})
            except json.JSONDecodeError:
                pass
        # 匹配裸 JSON
        start = response.find('{"tool"')
        if start < 0:
            return None
        # 找到匹配的闭合大括号（处理嵌套）
        depth = 0
        for i in range(start, len(response)):
            if response[i] == '{':
                depth += 1
            elif response[i] == '}':
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(response[start:i+1])
                        return data["tool"], data.get("args", {})
                    except json.JSONDecodeError:
                        return None
        return None
```

#### 2.2 验证

- [ ] 单元测试：explorer 子 Agent 只能调 read_file/list_dir/grep_code
- [ ] 单元测试：explorer 尝试调 write_file 被拒绝
- [ ] 单元测试：超过 max_turns 抛出 RuntimeError
- [ ] 单元测试：超过 timeout 返回超时事件

---

### Phase 3：主 Agent 改造（预计 2 天）

**目标**：改造 agent.py，让主 Agent 能自主决策并 spawn 子 Agent。

#### 3.1 改造 `backend/app/agent.py`

核心改动：
1. system prompt 中加入子 Agent 清单和调度指令
2. 新增 `task` 工具，主 Agent 通过它 spawn 子 Agent
3. 保留 LangGraph 状态图，但 execute 节点改为多轮 ReAct 循环

```python
# agent.py 改造要点（伪代码）

# 1. 构建增强版 system prompt
def build_system_prompt(workspace: str) -> str:
    from .agents.registry import list_subagents
    subagents = list_subagents()
    return f"""你是 Huai-Coder，一个项目级代码 Agent。

可用工具（直接调用）：
- list_dir: 列出目录
- read_file: 读取文件
- grep_code: 搜索代码
- write_file: 写入文件（需审批）
- execute_command: 执行命令（需审批）

可用子 Agent（通过 task 工具调度）：
{json.dumps(subagents, ensure_ascii=False, indent=2)}

调度原则：
- 简单查询直接用工具，不要 spawn 子 Agent
- 需要多步骤分析的任务，spawn explorer 先探索
- 需要制定计划的任务，spawn planner
- 需要修改代码的任务，spawn coder
- 需要运行测试的任务，spawn tester
- 可以并行 spawn 多个子 Agent

安全规则：
- 不要输出任何密钥、密码、Token
- 不要访问 .env 等敏感文件
- 写文件和执行命令会自动触发审批
"""

# 2. 注册 task 工具到 registry.py
# "task": ToolSpec("task", "Spawn a sub-agent", Risk("low", ...), _spawn_subagent)

# 3. _spawn_subagent 实现
async def _spawn_subagent(agent_name: str, task: str,
                         workspace: str, context: str) -> str:
    from .agents.subagent import SubAgent
    from .agents.registry import get_subagent_config
    config = get_subagent_config(agent_name)
    if config is None:
        return f"Unknown sub-agent: {agent_name}"
    agent = SubAgent(config)
    results = []
    async for event in agent.run(task, workspace, context):
        if event.type == "agent.finished":
            results.append(event.content)
        elif event.type == "error":
            results.append(f"ERROR: {event.content}")
    return "\n".join(results) or "No output"
```

#### 3.2 改造 `_execute` 节点为多轮循环

当前 agent.py 的 `_execute` 是单次的：收到 prompt -> 调一次 LLM -> 返回。

改为 ReAct 循环：

```
while turn < max_turns:
    response = LLM(messages)
    if response 包含工具调用:
        执行工具，结果加入 messages
    elif response 包含 task 调用:
        spawn 子 Agent，结果加入 messages
    else:
        返回最终回答
```

#### 3.3 验证

- [ ] 主 Agent 收到"帮我看看项目结构" -> 直接调 list_dir，不 spawn 子 Agent
- [ ] 主 Agent 收到"分析这个项目并制定重构计划" -> spawn explorer + planner
- [ ] 主 Agent 收到"修改 config.py 的端口号" -> spawn coder -> 触发审批
- [ ] 主 Agent 尝试让 explorer 写文件 -> 被权限层拒绝

---

### Phase 4：约束层完善（预计 1 天）

**目标**：加固四层约束机制。

#### 4.1 工具权限隔离（已在 Phase 1 的 SubAgentConfig.tools 实现）

补充：在 `registry.py` 的 `get_tool` 中增加调用者校验：

```python
def get_tool(name: str, caller: str = "main") -> ToolSpec:
    """获取工具，校验调用者权限"""
    if name not in TOOLS:
        raise WorkspaceViolation(f"Unknown tool: {name}")
    # 子 Agent 调用时校验权限
    if caller != "main":
        from .agents.registry import get_subagent_config
        config = get_subagent_config(caller)
        if config and name not in config.tools:
            raise WorkspaceViolation(
                f"Agent '{caller}' is not allowed to use tool '{name}'"
            )
    return TOOLS[name]
```

#### 4.2 审批插桩

抽取 main.py 中的审批逻辑为独立模块 `backend/app/approval.py`：

```python
async def request_approval(db, run_id, session_id, project_id,
                          tool_name, arguments, risk) -> Approval:
    """创建审批记录"""
    approval = Approval(
        run_id=run_id, session_id=session_id,
        tool_name=tool_name,
        arguments=json.dumps(scrub(arguments)),
        risk_level=risk.level,
        risk_reason=risk.reason,
    )
    db.add(approval)
    db.add(AuditLog(
        project_id=project_id, session_id=session_id,
        run_id=run_id, event_type="approval.requested",
        tool_name=tool_name, details=scrub(arguments),
    ))
    await db.commit()
    return approval
```

#### 4.3 审计追踪

在子 Agent 的每次工具调用后写入审计日志：

```python
# subagent.py 中，工具执行后
db.add(AuditLog(
    project_id=project_id,
    session_id=session_id,
    run_id=run_id,
    event_type="subagent.tool_executed",
    tool_name=tool_name,
    details=f"agent={self.config.name}, result={scrub(result)}",
))
```

#### 4.4 超时与轮次限制（已在 Phase 1 的 BaseSubAgent 实现）

补充主 Agent 的限制：

```python
# agent.py
MAIN_AGENT_MAX_TURNS = 30   # 主 Agent 最多 30 轮
MAIN_AGENT_TIMEOUT = 600    # 主 Agent 最多 10 分钟
```

#### 4.5 验证

- [ ] explorer 调 write_file -> 抛出 WorkspaceViolation
- [ ] coder 调 execute_command -> 抛出 WorkspaceViolation
- [ ] 所有子 Agent 工具调用都有 AuditLog 记录
- [ ] 超过 max_turns 的子 Agent 自动停止
- [ ] 超过 timeout 的子 Agent 返回超时事件

---

### Phase 5：main.py 瘦身（预计 1 天）

**目标**：删除 main.py 中的路由逻辑和审批等待逻辑，改为调用主 Agent。

#### 5.1 删除的代码

- L142-202：prompt 前缀判断 + 直接工具调用 + 审批轮询（约 60 行）
- L274-287：execute_plan 函数（移入 planner 子 Agent）

#### 5.2 改造后的 `/api/runs` 端点

```python
@app.post("/api/runs")
async def create_run(request: RunRequest, db: AsyncSession = Depends(get_db)):
    # ... 校验逻辑不变 ...
    run = AgentRun(prompt=request.prompt, status="running", agent_type="main")
    db.add(run)
    db.add(Message(session_id=session.id, role="user", content=request.prompt))
    await db.commit()
    await db.refresh(run)

    async def events():
        try:
            workspace = str((WORKSPACE_ROOT / "projects" / str(request.project_id)).resolve())
            history = [(m.role, m.content) for m in previous_messages]

            async for event in run_agent(request.prompt, workspace, history,
                                        f"session-{session.id}"):
                # 持久化
                db.add(AgentEventRecord(run_id=run.id, event_type=event.type,
                                       content=event.content, tool=event.tool))
                if event.type == "message.delta":
                    db.add(Message(session_id=session.id, role="assistant",
                                 content=event.content))
                if event.type in ("run.finished", "run.failed"):
                    run.status = "completed" if event.type == "run.finished" else "failed"
                await db.commit()

                yield f"data: {json.dumps({'run_id': run.id, 'type': event.type, 'content': event.content, 'tool': event.tool}, ensure_ascii=False)}\n\n"

        except Exception as error:
            run.status = "failed"
            await db.commit()
            yield f"data: {json.dumps({'type': 'run.failed', 'content': str(error)})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
```

#### 5.3 保留的端点

以下端点不变，继续保留：
- `/api/plans/{plan_id}/confirm` -> 调用 planner 子 Agent 的确认执行逻辑
- `/api/approvals/{id}/approve|reject|cancel` -> 审批决策
- 所有 CRUD 端点（projects/sessions/messages）

#### 5.4 验证

- [ ] main.py 行数从 316 行降到 ~200 行
- [ ] 所有现有 API 端点仍然可用
- [ ] 前端无需修改即可正常工作（SSE 事件格式兼容）

---

### Phase 6：前端适配（预计 1 天）

**目标**：前端展示子 Agent 调度过程。

#### 6.1 新增 SSE 事件处理

```typescript
// 新增事件类型
interface AgentEvent {
  type:
    | "agent.started"      // 子 Agent 开始
    | "agent.finished"     // 子 Agent 完成
    | "tool.called"        // 工具调用
    | "approval.required"  // 需要审批
    | "message.delta"      // 主 Agent 输出
    | "run.finished"       // 全部完成
    | "run.failed"         // 失败
    | "error";             // 错误
  content: string;
  tool?: string;
  agent_name?: string;     // 新增：哪个子 Agent
}
```

#### 6.2 UI 展示

- 子 Agent 执行时显示进度条：`[explorer] 正在分析项目结构...`
- 工具调用显示折叠面板：`read_file: config.py`
- 审批弹窗保持不变

---

## 四、数据模型变更

### 4.1 AgentRun 表新增字段

```python
class AgentRun(Base):
    # ... 现有字段 ...
    agent_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # 值: "main" / "explorer" / "planner" / "coder" / "tester"
```

### 4.2 新增 Alembic 迁移

文件：`migrations/versions/0004_agent_type.py`

```python
def upgrade():
    op.add_column('agent_runs',
        sa.Column('agent_type', sa.String(30), nullable=True))

def downgrade():
    op.drop_column('agent_runs', 'agent_type')
```

---

## 五、新增文件清单

```
backend/app/agents/
├── __init__.py          # 包标记
├── base.py              # BaseSubAgent + SubAgentConfig + AgentEvent
├── registry.py          # 子 Agent 声明式配置（权限锁死）
└── subagent.py          # 通用子 Agent 执行器（ReAct 循环）

backend/app/approval.py  # 审批逻辑抽取（从 main.py 移出）
```

## 六、修改文件清单

| 文件 | 改动 | 风险 |
|------|------|------|
| agent.py | 重写 _execute 为多轮 ReAct + 加入 task 工具 | 高 |
| registry.py | get_tool 增加 caller 权限校验 + 注册 task 工具 | 中 |
| main.py | 删除路由 if-else，简化 /api/runs | 高 |
| models.py | AgentRun 增加 agent_type 字段 | 低 |
| llm.py | complete() 支持 messages 列表输入 | 中 |

---

## 七、实施顺序与依赖关系

```
Phase 1 (基础设施)
    │
    ▼
Phase 2 (子 Agent 实现) ──→ Phase 4 (约束层)
    │                              │
    ▼                              ▼
Phase 3 (主 Agent 改造) ──→ Phase 5 (main.py 瘦身)
                                   │
                                   ▼
                            Phase 6 (前端适配)
```

- Phase 1 -> 2 -> 3 是串行依赖
- Phase 4 可以和 Phase 3 并行
- Phase 5 依赖 Phase 3 完成
- Phase 6 依赖 Phase 5 完成

---

## 八、验证策略

### 每个 Phase 完成后的验证

| Phase | 验证方式 |
|-------|---------|
| 1 | import 测试 + 配置加载测试 |
| 2 | 单元测试：权限隔离、轮次限制、超时 |
| 3 | 集成测试：主 Agent 调度子 Agent 全流程 |
| 4 | 安全测试：越权调用被拒绝、审计日志完整 |
| 5 | API 测试：所有端点正常、SSE 事件格式正确 |
| 6 | 手动测试：前端展示子 Agent 进度 |

### 回归测试

每个 Phase 完成后运行：

```powershell
cd backend
python -m pytest tests/ -v
```

---

## 九、风险与回退

| 风险 | 概率 | 影响 | 回退方案 |
|------|------|------|---------|
| LLM 不遵循 system prompt 调度指令 | 中 | 主 Agent 不调度子 Agent | 在 prompt 中加 few-shot 示例 |
| 工具调用解析失败 | 中 | 子 Agent 无法执行工具 | 回退到纯 LLM 回答模式 |
| 子 Agent 超时频繁 | 低 | 任务中断 | 增大 timeout 或减少 max_turns |
| main.py 改造引入 bug | 中 | API 不可用 | git 分支开发，保留旧版 |
| 前端事件格式不兼容 | 低 | 前端显示异常 | 保持旧事件格式兼容 |

### 回退策略

- 每个 Phase 在独立 git 分支开发
- Phase 5（main.py 瘦身）是最高风险步骤，保留旧版 main.py 备份
- 如果 Phase 3 的主 Agent 改造效果不好，可以回退到 Phase 2 的子 Agent + 简单规则路由

---

## 十、不在本次改进范围内

以下功能留待后续迭代：

- [ ] 多模型路由（不同子 Agent 用不同价格的模型）
- [ ] 子 Agent 并行执行（当前为串行）
- [ ] CLAUDE.md 持久化记忆文件
- [ ] 上下文压缩（Context Compaction）
- [ ] MCP 协议集成
- [ ] Worktree 隔离（子 Agent 独立文件系统）
- [ ] 前端可视化 Agent 调度图
