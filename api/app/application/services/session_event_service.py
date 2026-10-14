from dataclasses import dataclass
import base64
import json
import logging
from typing import List, Callable, Type

from datetime import datetime
from app.application.errors.exceptions import NotFoundError, ServerRequestsError, BadRequestError
from app.domain.models.event import BaseEvent, ToolEvent
from app.domain.models.session_event import SessionEvent
from app.domain.repositories.uow import IUnitOfWork

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SessionEventPage:
    """会话事件分页结果。"""

    events: list[SessionEvent]
    next_cursor: str | None
    has_more: bool


class SessionEventService:
    """session event service"""

    def __init__(self,  uow_factory: Callable[[], IUnitOfWork]) -> None:
        self._uow_factory = uow_factory


    @staticmethod
    def _encode_cursor(
        created_at: datetime,
    ) -> str:
        """将事件排序字段编码成不透明游标。"""

        cursor_data = {
            "created_at": created_at.isoformat(),
        }

        cursor_json = json.dumps(
            cursor_data,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        return base64.urlsafe_b64encode(
            cursor_json.encode("utf-8")
        ).decode("ascii")

    @staticmethod
    def _decode_cursor(
            cursor: str,
    ) -> datetime:
        """解析分页游标。"""

        try:
            cursor_json = base64.urlsafe_b64decode(
                cursor.encode("ascii")
            ).decode("utf-8")

            cursor_data = json.loads(cursor_json)

            created_at = datetime.fromisoformat(
                cursor_data["created_at"]
            )

            return created_at

        except (
                ValueError,
                TypeError,
                KeyError,
                json.JSONDecodeError,
        ) as exc:
            raise BadRequestError("无效的会话事件分页游标") from exc



    async def append_event(self, session_id: str, event: BaseEvent, run_id: str | None) -> SessionEvent:
        """向指定会话追加一条事件。"""

        # 1.将现有业务事件转换为会话事件。
        session_event = SessionEvent(
            event_id=event.id,
            session_id=session_id,
            run_id=run_id,
            event_type=event.type,
            tool_call_id=(
                event.tool_call_id
                if isinstance(event, ToolEvent)
                else None
            ),
            payload=event.model_dump(
                mode="json",
                exclude={
                    "id",
                    "type",
                    "created_at",
                },
            ),
            occurred_at=event.created_at,
        )

        # 2.检查会话是否存在，并在同一个事务中保存事件。
        async with self._uow_factory() as uow:
            session = await uow.session.get_by_id(session_id)
            if session is None:
                raise NotFoundError(
                    f"会话[{session_id}]不存在，无法添加事件"
                )

            await uow.session_event.add(session_event)

        logger.debug(
            "会话事件新增成功: session_id=%s, event_id=%s, event_type=%s",
            session_id,
            session_event.event_id,
            session_event.event_type,
        )

        return session_event


    async def get_page_by_session_id(
            self,
            session_id: str,
            limit: int,
            before: str | None = None,
    ) -> SessionEventPage:
        """分页查询会话事件。

                第一次查询不传before，返回最新的limit条事件。
                加载更早事件时，传递上一次返回的next_cursor。
                """

        if limit < 1 or limit > 100:
            raise BadRequestError("limit必须在1到100之间")

        before_created_at: datetime | None = None

        if before:
            before_created_at = self._decode_cursor(
                before
            )

        async with self._uow_factory() as uow:
            session_exists = await uow.session.get_session_exists(
                session_id
            )

            if not session_exists:
                raise NotFoundError(
                    f"会话[{session_id}]不存在，无法查询事件"
                )

            events, has_more = (
                await uow.session_event.get_by_session_id(
                    session_id=session_id,
                    limit=limit,
                    before_created_at=before_created_at,
                )
            )

        next_cursor: str | None = None

        # Repository返回的events已经是“旧 -> 新”。
        # events[0]是本页最早的记录，下一页需要查询它之前的数据。
        if events and has_more:
            oldest_event = events[0]

            next_cursor = self._encode_cursor(
                created_at=oldest_event.created_at,
            )

        logger.debug(
            "查询会话事件分页成功: "
            "session_id=%s, count=%s, has_more=%s",
            session_id,
            len(events),
            has_more,
        )

        return SessionEventPage(
            events=events,
            next_cursor=next_cursor,
            has_more=has_more,
        )