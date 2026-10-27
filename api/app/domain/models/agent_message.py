import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field
from typing import Literal, Any


class AgentMessage(BaseModel):

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    session_id: str

    run_id: str

    agent_name: str

    sequence: int | None = None

    role: Literal["system", "developer", "user", "assistant", "tool"]

    message: dict[str, Any]

    tool_call_id: str | None = None

    token_count: int

    compacted_at: datetime | None = None

    excluded_at: datetime | None = None

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

