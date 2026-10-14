from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


EventType = Literal[
    "message",
    "title",
    "plan",
    "step",
    "tool",
    "wait",
    "error",
    "done",
]


class SessionEventResponse(BaseModel):
    """单条会话事件响应。"""

    # session_events 表的主键
    id: str

    # Agent业务事件ID
    event_id: str

    session_id: str

    # 本次Agent任务运行ID
    run_id: str | None = None

    event_type: EventType

    # 仅工具事件存在
    tool_call_id: str | None = None

    # 不同事件自己的业务数据
    payload: dict[str, Any] = Field(default_factory=dict)

    # 业务事件实际发生时间
    occurred_at: datetime

    # 数据库记录创建时间
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ListSessionEventsResponse(BaseModel):
    """分页查询会话事件响应。"""

    events: list[SessionEventResponse] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False