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
        run_started_at: datetime,
        run_id: str
    ) -> str:
        """将事件排序字段编码成不透明游标。"""

        cursor_data = {
            "run_started_at": run_started_at.isoformat(),
            "run_id": run_id,
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
    ) -> tuple[datetime, str]:
        """解析分页游标。"""

        try:
            cursor_json = base64.urlsafe_b64decode(
                cursor.encode("ascii")
            ).decode("utf-8")

            cursor_data = json.loads(cursor_json)

            return (
                datetime.fromisoformat(cursor_data["run_started_at"]),
                str(cursor_data["run_id"]),
            )


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
            run_id=run_id if run_id is not None else event.run_id,
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
                    "run_id",
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
            run_limit: int,
            before: str | None = None,
    ) -> SessionEventPage:

        if run_limit < 1 or run_limit > 10:
            raise BadRequestError(
                "run_limit必须在1到10之间"
            )


        before_started_at: datetime | None = None
        before_run_id: str | None = None

        if before:
            before_started_at, before_run_id = self._decode_cursor(before)



        async with self._uow_factory() as uow:
            # 5.检查目标会话是否存在。
            session_exists = (
                await uow.session.get_session_exists(session_id)
            )

            if not session_exists:
                raise NotFoundError(
                    f"会话[{session_id}]不存在，无法查询事件"
                )

            # 查询run_limit个完整运行的全部事件。
            #
            # Repository负责：
            # - 查询当前页有哪些run_id；
            # - 查询这些run_id的全部事件；
            # - 判断是否还有更早的run；
            # - 返回本页最老run的分页边界。
            (
                events,
                has_more,
                oldest_run_started_at,
                oldest_run_id,
            ) = await (
                uow.session_event.get_run_page_by_session_id(
                    session_id=session_id,
                    run_limit=run_limit,
                    before_started_at=before_started_at,
                    before_run_id=before_run_id,
                )
            )

        # 7.默认没有下一页游标。
        next_cursor: str | None = None

        # 8.存在更早的运行时，使用本页最老run生成下一页游标。
        if has_more:
            if (
                oldest_run_started_at is None
                or oldest_run_id is None
            ):
                # 这是Repository返回结果违反约定，属于服务端错误，
                # 不能向客户端返回一个无法继续分页的结果。
                raise RuntimeError(
                    "会话事件仓库未返回运行分页边界"
                )

            next_cursor = self._encode_cursor(
                run_started_at=oldest_run_started_at,
                run_id=oldest_run_id,
            )

        # 9.返回应用层分页结果。
        return SessionEventPage(
            events=events,
            next_cursor=next_cursor,
            has_more=has_more,
        )