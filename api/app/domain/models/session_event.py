from datetime import datetime

import uuid

from typing import Optional, Dict, Any

from pydantic import BaseModel, Field

from app.domain.models.event import BaseEvent, ToolEvent


class SessionEvent(BaseModel):
    """事件领域模型"""

    # session_events表自身的记录ID。
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    # 原始业务事件ID。
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    # 会话id
    session_id: str

    # 所属的一次Agent任务运行ID。
    run_id: Optional[str] = None

    # event type
    event_type: str

    # 仅工具事件具有该字段。
    tool_call_id: Optional[str] = None

    # 原始事件除公共字段外的业务数据。
    payload: Dict[str, Any] = Field(default_factory=dict)

    # 原始事件发生时间。
    occurred_at: datetime = Field(default_factory=datetime.now)

    # 创建事件
    created_at: datetime = Field(default_factory=datetime.now)


    @classmethod
    def from_event(
        cls,
        session_id: str,
        event: BaseEvent,
        run_id: str | None = None
    ) -> 'SessionEvent':
        """将Agent业务事件转换为会话持久化事件。"""
        # 仅ToolEvent具有tool_call_id。
        tool_call_id = (
            event.tool_call_id
            if isinstance(event, ToolEvent)
            else None
        )

        # payload只保存不同事件自己的业务字段。
        # id、type和created_at已经映射到独立数据库列，不重复保存。
        payload = event.model_dump(
            mode="json",
            exclude={
                "id",
                "type",
                "created_at",
            },
        )

        return cls(
            event_id=event.id,
            session_id=session_id,
            run_id=run_id,
            event_type=event.type,
            tool_call_id=tool_call_id,
            payload=payload,
            occurred_at=event.created_at,
        )
