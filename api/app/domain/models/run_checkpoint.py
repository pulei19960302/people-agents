from enum import Enum

from pydantic import BaseModel, Field

from app.domain.models.plan import Step


class RunCheckpointPhase(str, Enum):
    """PlannerReActFlow可以恢复的持久化阶段。"""

    PLANNING = "planning" # 正在根据用户问题创建 Plan
    EXECUTING = "executing" # 表示 ReAct 正在执行 Plan 中某一个 Step
    UPDATING = "updating" # 当前 Step 已经执行完成或失败，接下来应该交给 Planner 更新 Plan
    SUMMARIZING = "summarizing" # 表示所有 Step 都已经执行完成，ReAct 正在生成最终回答
    WAITING = "waiting" # 等待用户回答
    COMPLETED = "completed" # 完成


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
