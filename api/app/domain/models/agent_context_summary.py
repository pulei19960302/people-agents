import uuid

from datetime import datetime, timezone

from pydantic import BaseModel, Field



class AgentContextSummary(BaseModel):
    """Session级的Agent上下文摘要。"""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    # 摘要归属于Session，不再归属于某个Run。
    session_id: str

    # Planner和ReAct维护各自的摘要。
    agent_name: str

    # 最后一次参与摘要的Run，仅用于追踪来源。
    last_run_id: str | None = None

    summary: str = ""

    # 摘要已经覆盖到哪条AgentMessage。
    summarized_through_sequence: int = 0

    version: int = 1

    token_count: int = 0

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )