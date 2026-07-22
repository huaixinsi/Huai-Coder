from .base import SubAgentConfig

# Hard-coded permission table. These are NOT suggestions.
# explorer/planner: read-only, can never write or execute.
# coder: can write, can never execute commands.
# tester: can execute, can never write files.
SUBAGENT_CONFIGS: dict[str, SubAgentConfig] = {
    "explorer": SubAgentConfig(
        name="explorer",
        description="Read-only project exploration: list files, read source, search code.",
        tools=("list_dir", "read_file", "grep_code"),
        max_turns=10,
        timeout=60,
        needs_approval=False,
        system_prompt=(
            "You are a code explorer. Use the available tools to inspect the project "
            "and answer the user's question. Only report what you find; do not modify anything."
        ),
    ),
    "planner": SubAgentConfig(
        name="planner",
        description="Analyze project structure and produce a step-by-step plan.",
        tools=("list_dir", "read_file", "grep_code"),
        max_turns=5,
        timeout=30,
        needs_approval=False,
        system_prompt=(
            "You are a planning agent. Inspect the project and produce a concise, "
            "ordered plan to achieve the user's goal. Do not modify any files."
        ),
    ),
    "coder": SubAgentConfig(
        name="coder",
        description="Write or modify project files to implement changes.",
        tools=("read_file", "write_file", "grep_code"),
        max_turns=25,
        timeout=300,
        needs_approval=True,
        system_prompt=(
            "You are a coding agent. Read existing code, then write or modify files to "
            "implement the requested change. You cannot execute commands."
        ),
    ),
    "tester": SubAgentConfig(
        name="tester",
        description="Run tests and commands to verify changes.",
        tools=("execute_command", "read_file"),
        max_turns=15,
        timeout=120,
        needs_approval=True,
        system_prompt=(
            "You are a testing agent. Run commands to verify that the project works "
            "correctly. You cannot write or modify files."
        ),
    ),
}


def get_subagent_config(name: str) -> SubAgentConfig | None:
    return SUBAGENT_CONFIGS.get(name)


def list_subagents() -> list[dict]:
    """Return a summary suitable for embedding in a system prompt."""
    return [
        {
            "name": cfg.name,
            "description": cfg.description,
            "tools": list(cfg.tools),
            "needs_approval": cfg.needs_approval,
        }
        for cfg in SUBAGENT_CONFIGS.values()
    ]
