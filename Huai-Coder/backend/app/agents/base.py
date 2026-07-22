from dataclasses import dataclass, field


@dataclass(frozen=True)
class SubAgentConfig:
    """Immutable permission declaration for a sub-agent."""

    name: str
    description: str
    tools: tuple[str, ...]
    max_turns: int
    timeout: int  # seconds
    needs_approval: bool = False
    system_prompt: str = ""

    def can_use(self, tool_name: str) -> bool:
        return tool_name in self.tools
