from enum import Enum

from pydantic import BaseModel, Field

from app.domain.models.plan import Step


class RunCheckpointPhase(str, Enum):
    """PlannerReActFlow可以恢复的持久化阶段。"""

    PLANNING = "planning"
    EXECUTING = "executing"
    UPDATING = "updating"
    SUMMARIZING = "summarizing"
    WAITING = "waiting"
    COMPLETED = "completed"


class PendingToolCall(BaseModel):
    """中断时可能尚未取得可靠结果的工具调用。"""

    tool_call_id: str
    tool_name: str
    function_name: str
    waiting_for_user: bool = False


class RunCheckpoint(BaseModel):
    """SessionRun的最小可恢复执行快照。

    Plan和消息的完整历史仍分别保存在session_events和agent_messages中。
    checkpoint只保存恢复流程阶段所需的指针和当前步骤快照。
    """

    version: int = Field(default=1, ge=1)
    flow_status: RunCheckpointPhase = RunCheckpointPhase.PLANNING
    plan_event_id: str | None = None
    current_step: Step | None = None
    pending_tool_call: PendingToolCall | None = None
    last_event_id: str | None = None

    @property
    def current_step_id(self) -> str | None:
        return self.current_step.id if self.current_step is not None else None
