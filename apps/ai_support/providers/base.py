from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class SupportImage:
    content: bytes
    mime_type: str


@dataclass(frozen=True)
class SupportTurn:
    role: str
    text: str


@dataclass(frozen=True)
class SupportRequest:
    user_text: str
    system_instruction: str
    knowledge_chunks: tuple[str, ...]
    route_context: dict[str, str]
    user_role: str
    public_base_url: str
    max_output_tokens: int
    history: tuple[SupportTurn, ...] = ()
    image: SupportImage | None = None


@dataclass(frozen=True)
class SupportResult:
    text: str
    provider: str
    model: str
    status: str
    latency_ms: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    error_code: str = ""
    request_id: str = ""


class AIProvider(Protocol):
    def generate(self, request: SupportRequest) -> SupportResult: ...
