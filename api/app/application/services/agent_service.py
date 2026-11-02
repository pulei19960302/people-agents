#!/usr/bin/env python
# -*- coding: utf-8 -*-
import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncGenerator, Optional, List, Type, Callable

from pydantic import TypeAdapter

from app.domain.external.file_storage import FileStorage
from app.domain.external.json_parser import JSONParser
from app.domain.external.llm import LLM
from app.domain.external.sandbox import Sandbox
from app.domain.external.search import SearchEngine
from app.domain.external.task import Task as StreamTask
from app.domain.models.app_config import AgentConfig, MCPConfig, A2AConfig
from app.domain.models.event import BaseEvent, ErrorEvent, MessageEvent, Event, DoneEvent, WaitEvent
from app.domain.models.file import File
from app.domain.models.session import Session
from app.domain.models.session_event import SessionEvent
from app.domain.models.session_run import SessionRun, SessionRunStatus
from app.domain.repositories.uow import IUnitOfWork
from app.domain.services.agent_task_runner import AgentTaskRunner

logger = logging.getLogger(__name__)

# redis-py 8.x默认socket超时为5秒。XREAD阻塞时间必须小于该值，
# 让空闲Run定期回到读取循环检查Task是否已经结束，避免被当作Redis超时。
OUTPUT_STREAM_BLOCK_MS = 3_000

EVENT_ADAPTER = TypeAdapter(Event)


class RunClaimConflictError(RuntimeError):
    """当前Run已经被另一个请求绑定，失败请求不能修改Run状态。"""


@dataclass(frozen=True, slots=True)
class _PreparedChat:
    """第一阶段任务编排成功后，第二阶段消费输出所需的数据。"""

    run: SessionRun
    stream_task: StreamTask
    message_event: MessageEvent | None = None


class AgentService:
    """协调Session、SessionRun、进程内Stream Task以及SSE事件传输。"""

    def __init__(
            self,
            uow_factory: Callable[[], IUnitOfWork],
            llm: LLM,
            agent_config: AgentConfig,
            mcp_config: MCPConfig,
            a2a_config: A2AConfig,
            sandbox_cls: Type[Sandbox],
            stream_task_cls: Type[StreamTask],
            json_parser: JSONParser,
            search_engine: SearchEngine,
            file_storage: FileStorage,
    ) -> None:
        """保存任务编排所需的仓储工厂、模型配置和外部服务依赖。"""
        self._uow_factory = uow_factory
        self._llm = llm
        self._agent_config = agent_config
        self._mcp_config = mcp_config
        self._a2a_config = a2a_config
        self._sandbox_cls = sandbox_cls
        self._stream_task_cls = stream_task_cls
        self._json_parser = json_parser
        self._search_engine = search_engine
        self._file_storage = file_storage
        logger.info(f"AgentService初始化成功")

    def _get_stream_task(self, run: SessionRun) -> Optional[StreamTask]:
        """根据Run记录的active_task_id查询当前进程中的Task实例。"""

        if run.active_task_id is None:
            return None

        # Task注册表只存在于当前API进程；应用重启后即使数据库保留task_id也会查询不到。
        return self._stream_task_cls.get(run.active_task_id)

    async def _create_stream_task(
            self,
            session: Session,
            run: SessionRun,
    ) -> StreamTask:
        """准备沙箱和Runner，并以原子方式为Run绑定新的进程内Task。"""

        # bind_task会把Run更新为RUNNING，因此必须提前记录用于断点恢复的原状态。
        # 新任务通常是PENDING；恢复任务可能是WAITING、INTERRUPTED或FAILED。
        resume_from_status = run.status

        # 优先复用Session已经绑定的沙箱。
        sandbox = None
        sandbox_id = session.sandbox_id
        if sandbox_id:
            sandbox = await self._sandbox_cls.get(sandbox_id)

        # 沙箱未创建或已经被释放时，创建新沙箱并回写Session。
        if not sandbox:
            sandbox = await self._sandbox_cls.create()
            session.sandbox_id = sandbox.id
            async with self._uow_factory() as uow:
                await uow.session.save(session)

        # AgentTaskRunner要求沙箱浏览器已经就绪。
        browser = await sandbox.get_browser()
        if not browser:
            logger.error(f"获取沙箱[{sandbox.id}]中的浏览器实例失败")
            raise RuntimeError(f"获取沙箱[{sandbox.id}]中的浏览器实例失败")

        # Runner负责执行Agent流程、持久化业务事件并发布到Redis输出流。
        task_runner = AgentTaskRunner(
            uow_factory=self._uow_factory,
            llm=self._llm,
            agent_config=self._agent_config,
            mcp_config=self._mcp_config,
            a2a_config=self._a2a_config,
            session_id=session.id,
            run_id=run.id,
            resume_from_status=resume_from_status,
            file_storage=self._file_storage,
            json_parser=self._json_parser,
            browser=browser,
            search_engine=self._search_engine,
            sandbox=sandbox,
        )

        # Stream Task负责后台调度Runner，并提供输入、输出两个Redis Stream。
        stream_task = self._stream_task_cls.create(task_runner=task_runner)

        try:
            async with self._uow_factory() as uow:
                await uow.session_run.bind_task(
                    run_id=run.id,
                    task_id=stream_task.id,
                )
        except RuntimeError as exc:
            # _resolve_run持有的Session行锁在返回时已经释放，另一个恢复请求
            # 可能抢先绑定同一个Run。这里只清理本请求创建的进程内Task，
            # 不能把赢家正在执行的Run标记为FAILED。
            stream_task.cancel()
            raise RunClaimConflictError(
                f"运行[{run.id}]已经由其他请求启动"
            ) from exc

        run.active_task_id = stream_task.id
        run.status = SessionRunStatus.RUNNING
        return stream_task


    async def _resolve_run(
            self,
            session_id: str,
            resume_run_id: str | None = None
    ) -> SessionRun:

        """在Session行锁保护下决定创建新Run，还是恢复指定的最新Run。"""
        uow = self._uow_factory()

        async with uow:
            # 同一Session的Run决策必须串行执行。该行锁会一直持有到
            # 当前UoW退出，确保“读取最新Run”和“创建新Run”是一个原子过程。
            session_locked = await uow.session.lock_for_run_resolution(session_id)
            if not session_locked:
                raise RuntimeError(f"会话[{session_id}]不存在")

            # 获取锁后再读取，确保拿到前一个并发请求提交后的最新状态。
            latest_run = (
                await uow.session_run.get_latest_by_session_id(session_id)
            )

            # 明确恢复某个Run
            if resume_run_id is not None:
                run = await uow.session_run.get_by_id(resume_run_id)
                # 判断
                if run is None:
                    raise RuntimeError(
                        f"运行[{resume_run_id}]不存在"
                    )

                if run.session_id != session_id:
                    raise RuntimeError(
                        f"运行[{resume_run_id}]不属于会话[{session_id}]"
                    )

                # Session级摘要会持续合并后续Run的历史。如果允许恢复旧Run，
                # 旧任务将读取到在它之后产生的上下文，因此只能恢复最新Run。
                # 要求恢复旧任务。但是现在最新的已经是其他的了
                if latest_run is None or latest_run.id != run.id:
                    raise RuntimeError(
                        f"运行[{resume_run_id}]不是会话[{session_id}]的"
                        "最新运行，Session级上下文模式下只能恢复最新运行"
                    )

                if not run.status.can_resume:
                    raise RuntimeError(
                        f"运行[{resume_run_id}]当前状态"
                        f"[{run.status.value}]不能恢复"
                    )

                return run

            # 没有历史Run，创建第一轮任务。
            if latest_run is None:
                run = SessionRun(session_id=session_id)
                await uow.session_run.add(run)
                return run

            # 普通/chat始终表示新请求。回答WAITING问题或恢复旧任务
            # 必须显式使用/runs/{run_id}/resume，避免新话题误入旧计划。
            if latest_run.status == SessionRunStatus.WAITING:
                await uow.session_run.update_status(
                    latest_run.id,
                    SessionRunStatus.CANCELLED,
                )

            # 当前任务还未启动完成或正在执行，拒绝创建第二个Run。
            if latest_run.status in {
                SessionRunStatus.PENDING,
                SessionRunStatus.RUNNING,
            }:
                raise RuntimeError(
                    f"会话[{session_id}]当前任务正在运行，"
                    "请等待任务完成后再发送新消息"
                )

            # WAITING先放弃旧任务；COMPLETED、FAILED、INTERRUPTED、CANCELLED后，
            # 普通聊天消息表示开始新一轮任务。
            run = SessionRun(session_id=session_id)
            await uow.session_run.add(run)
            return run



    async def _safe_update_unread_count(self, session_id: str) -> None:
        """在独立的后台任务中安全地更新未读消息计数

        该方法通过asyncio.create_task()调用，运行在一个全新的asyncio Task中，
        因此不受sse_starlette的anyio cancel scope影响，数据库操作可以正常完成。
        使用uow_factory创建全新的UoW实例，避免与被取消的上下文共享数据库连接。
        """
        try:
            uow = self._uow_factory()
            async with uow:
                await uow.session.update_unread_message_count(session_id, 0)
        except Exception as e:
            logger.warning(f"会话[{session_id}]后台更新未读消息计数失败: {e}")

    async def _get_session(self, session_id: str) -> Session:
        """查询会话，不存在时统一抛出业务错误。"""
        async with self._uow_factory() as uow:
            session = await uow.session.get_by_id(session_id)

        if session is None:
            logger.error(f"尝试与不存在的任务会话[{session_id}]对话")
            raise RuntimeError("任务会话不存在, 请核实后重试")

        return session

    # 重新连接
    async def _get_reconnect_run(
            self,
            session_id: str,
            resume_run_id: str | None,
    ) -> SessionRun:
        """SSE重新连接只查询已有Run，绝不隐式创建新Run。"""
        async with self._uow_factory() as uow:
            if resume_run_id is not None:
                run = await uow.session_run.get_by_id(resume_run_id)
            else:
                run = await uow.session_run.get_latest_by_session_id(
                    session_id
                )

        if run is None or run.session_id != session_id:
            raise RuntimeError("当前会话没有可连接的任务运行")

        return run

    async def _load_attachments(
            self,
            attachment_ids: list[str],
    ) -> list[File]:
        """一次查询加载用户消息中的全部附件。"""
        async with self._uow_factory() as uow:
            return await uow.file.get_by_ids(attachment_ids)

    async def _persist_user_message(
            self,
            session_id: str,
            run: SessionRun,
            message: str,
            attachments: list[str],
            timestamp: datetime | None,
    ) -> MessageEvent:
        """创建并在同一事务中保存用户消息及Session摘要字段。"""
        db_attachments = await self._load_attachments(attachments)
        message_event = MessageEvent(
            run_id=run.id,
            role="user",
            message=message,
            attachments=db_attachments,
        )

        session_event = SessionEvent.from_event(
            session_id=session_id,
            event=message_event,
            run_id=run.id,
        )

        async with self._uow_factory() as uow:
            await uow.session_event.add(session_event)
            await uow.session.update_latest_message(
                session_id=session_id,
                message=message,
                timestamp=timestamp or datetime.now(),
            )

        return message_event

    async def _clear_stale_task_binding(self, run: SessionRun) -> None:
        """清除数据库中已经找不到进程内Task的旧绑定。"""
        if run.active_task_id is None:
            return

        async with self._uow_factory() as uow:
            await uow.session_run.clear_task(
                run_id=run.id,
                expected_task_id=run.active_task_id,
            )
        run.active_task_id = None

    @staticmethod
    def _normalize_stream_cursor(stream_cursor: str | None) -> str | None:
        """只允许Redis Stream原生游标，避免把业务事件UUID传给XREAD。"""
        if stream_cursor is None:
            return None
        if re.fullmatch(r"\d+-\d+", stream_cursor):
            return stream_cursor

        logger.warning("忽略无效的Redis Stream游标: %s", stream_cursor)
        return None

    @staticmethod
    async def _cleanup_completed_stream_task(
            stream_task: StreamTask,
            session_id: str,
    ) -> None:
        """DoneEvent已经交给SSE上层后，删除成功任务的输入、输出Stream。"""
        try:
            await stream_task.input_stream.clear()
            await stream_task.output_stream.clear()
            logger.info(f"会话[{session_id}]任务执行成功，Redis Stream已删除")
        except Exception as e:
            # Runner已经为成功任务设置TTL，立即删除失败时仍会自动过期。
            logger.warning(f"会话[{session_id}]删除Redis Stream失败: {e}")

    async def _read_stream_task_events(
            self,
            session_id: str,
            run: SessionRun,
            stream_task: StreamTask,
            stream_cursor: str | None,
    ) -> AsyncGenerator[BaseEvent, None]:
        """持续读取Runner输出，并把Redis游标附加到实时事件。"""
        latest_stream_cursor = self._normalize_stream_cursor(stream_cursor)

        while True:
            # 使用上次Redis Stream ID续读；None表示从当前Stream的起点开始。
            message_cursor, event_str = await stream_task.output_stream.get(
                start_id=latest_stream_cursor,
                block_ms=OUTPUT_STREAM_BLOCK_MS,
            )

            if event_str is None:
                if stream_task.done:
                    break
                logger.debug(f"在会话[{session_id}]输出队列中未发现事件内容")
                continue

            latest_stream_cursor = message_cursor

            # Redis Stream游标和业务事件ID用途不同：前者负责断线续读，
            # 后者继续用于session_events以及前端事件合并去重。
            event = EVENT_ADAPTER.validate_json(event_str)
            event.stream_cursor = message_cursor

            logger.debug(
                "从会话[%s]运行[%s]读取事件: %s",
                session_id,
                run.id,
                type(event).__name__,
            )

            yield event

            # 等待、失败和完成都会结束本次SSE响应
            if isinstance(event, (DoneEvent, ErrorEvent, WaitEvent)):
                break


    async def _persist_orchestration_error(
            self,
            session_id: str,
            run: SessionRun,
            error_event: ErrorEvent,
            stream_task_id: str | None = None,
    ) -> None:
        """持久化任务启动前发生的编排错误。"""

        session_event = SessionEvent.from_event(
            session_id=session_id,
            event=error_event,
            run_id=run.id,
        )


        async with self._uow_factory() as uow:
            await uow.session_event.add(session_event)
            await uow.session_run.update_status(
                run_id=run.id,
                status=SessionRunStatus.FAILED,
                error=error_event.error,
            )
            if stream_task_id is not None:
                await uow.session_run.clear_task(
                    run_id=run.id,
                    expected_task_id=stream_task_id,
                )


    # 如果task丢失了。但是run 是running
    async def _interrupt_missing_task(
            self,
            session_id: str,
            run: SessionRun,
    ) -> ErrorEvent:
        """将找不到进程内Task的RUNNING状态Run标记为中断。"""
        async with self._uow_factory() as uow:
            # 状态和active_task_id必须在同一个事务中更新。
            await uow.session_run.update_status(
                run.id,
                SessionRunStatus.INTERRUPTED,
                error="任务执行实例已经丢失",
            )
            if run.active_task_id is not None:
                await uow.session_run.clear_task(
                    run_id=run.id,
                    expected_task_id=run.active_task_id,
                )

        # 同步当前请求中的领域对象，避免后续逻辑继续使用旧状态。
        run.status = SessionRunStatus.INTERRUPTED
        run.active_task_id = None

        logger.warning(
            "会话[%s]运行[%s]的执行实例已经丢失，已标记为中断",
            session_id,
            run.id,
        )

        return ErrorEvent(
            run_id=run.id,
            error="任务执行实例已经丢失，已标记为中断，请通过恢复任务接口继续",
        )

    async def _prepare_chat(
            self,
            session_id: str,
            message: Optional[str],
            attachments: Optional[List[str]],
            timestamp: Optional[datetime],
            resume_run_id: str | None,
    ) -> _PreparedChat | ErrorEvent:
        """完成任务编排，成功时返回第二阶段需要的运行上下文。"""

        run: SessionRun | None = None
        stream_task: StreamTask | None = None
        message_event: MessageEvent | None = None
        is_message_request = bool(message)
        stream_task_created_by_request = False

        # ------------------------------------------------------------
        # 第一阶段：任务编排
        # 负责：
        # 1. 查询Session；
        # 2. 创建或恢复SessionRun；
        # 3. 创建RedisStreamTask；
        # 4. 保存用户消息；
        # 5. 写入Redis输入流；
        # 6. 启动后台Runner。
        #
        # 这里失败表示任务尚未被Runner完整接管。携带消息请求的普通编排错误会
        # 保存ErrorEvent并把对应Run标记为FAILED；并发抢占失败不能修改赢家Run。
        # ------------------------------------------------------------
        try:
            # 所有入口都必须先确认Session存在。
            session = await self._get_session(session_id)

            # 普通/chat发起新任务；显式resume接口才恢复旧Run。也就是有resume_run_id
            if is_message_request:
                run = await self._resolve_run(session_id=session_id, resume_run_id=resume_run_id)
            else:
                # 没有消息表示SSE重新连接，不能创建新Run
                run = await self._get_reconnect_run(
                    session_id=session_id,
                    resume_run_id=resume_run_id,
                )

            # 根据Run中的active_task_id查找进程内Task
            stream_task = self._get_stream_task(run)

            # 只有携带消息的请求才会创建/恢复Task并写入输入Stream。
            if is_message_request and run is not None:
                # 断言
                assert message is not None

                # RUNNING但查询不到Task，说明API进程重启或执行实例意外丢失。
                if run.status == SessionRunStatus.RUNNING and stream_task is None:
                    return await self._interrupt_missing_task(
                        session_id=session_id,
                        run=run,
                    )

                # WAITING、INTERRUPTED、FAILED可能保留失效task_id。失效的none
                if stream_task is None and run.active_task_id is not None:
                    await self._clear_stale_task_binding(run)

                # 不存在执行实例时，为当前Run创建新的Stream Task。
                if stream_task is None:
                    stream_task = await self._create_stream_task(
                        session=session,
                        run=run,
                    )
                    stream_task_created_by_request = True

                # 用户消息先持久化，再发布到Task输入Stream。
                message_event = await self._persist_user_message(
                    session_id=session_id,
                    run=run,
                    message=message,
                    attachments=attachments or [],
                    timestamp=timestamp,
                )

                logger.debug(
                    "用户消息事件保存成功: "
                    "session_id=%s, event_id=%s",
                    session_id,
                    message_event.id,
                )

                # Redis输入Stream负责把用户消息交给后台Runner。
                stream_id = await stream_task.input_stream.put(
                    message_event.model_dump_json()
                )

                logger.debug(
                    "用户消息写入成功: session_id=%s, run_id=%s, "
                    "stream_task_id=%s, event_id=%s, stream_id=%s",
                    session_id,
                    run.id,
                    stream_task.id,
                    message_event.id,
                    stream_id,
                )
                # invoke只启动后台执行，不在当前SSE请求内等待Agent完成。
                await stream_task.invoke()

            elif stream_task is None:
                raise RuntimeError(
                    "当前任务执行实例已经结束或丢失，请读取历史事件"
                )

            if run is None or stream_task is None:
                # 这是程序状态异常，正常情况下不会进入这里。
                return ErrorEvent(
                    run_id=run.id if run is not None else None,
                    error="任务编排完成，但没有可用的Run或Task",
                )

            return _PreparedChat(
                run=run,
                stream_task=stream_task,
                message_event=message_event,
            )

        except asyncio.CancelledError:
            # 客户端在任务编排阶段断开连接，
            # 不应该因此把Run标记成FAILED。
            logger.info(
                "会话[%s]在任务编排阶段取消SSE连接",
                session_id,
            )
            raise

        except RunClaimConflictError as exc:
            # 另一个请求已经成功领取该Run。当前请求不能写ErrorEvent，
            # 更不能把赢家正在运行的Run更新成FAILED。
            logger.warning(
                "会话[%s]恢复任务发生并发冲突: %s",
                session_id,
                exc,
            )
            return ErrorEvent(
                run_id=run.id if run is not None else None,
                error=str(exc),
            )

        except Exception as exc:
            error_message = str(exc) or type(exc).__name__

            logger.exception(
                "会话[%s]任务编排失败: %s",
                session_id,
                error_message,
            )

            error_event = ErrorEvent(
                run_id=run.id if run is not None else None,
                error=error_message,
            )

            # Session不存在或Run尚未创建时，没有可以关联的Run，
            # 此时只能向当前客户端返回错误。
            if run is not None and is_message_request:
                try:
                    failed_stream_task_id = (
                        stream_task.id
                        if stream_task is not None and stream_task_created_by_request
                        else None
                    )
                    if failed_stream_task_id is not None:
                        # 编排尚未完整交给Runner，本请求创建的Stream Task不能留在
                        # 进程注册表或继续占用active_task_id。
                        stream_task.cancel()
                    await self._persist_orchestration_error(
                        session_id=session_id,
                        run=run,
                        error_event=error_event,
                        stream_task_id=failed_stream_task_id,
                    )
                except Exception as persist_exc:
                    logger.exception(
                        "会话[%s]运行[%s]编排错误持久化失败: %s",
                        session_id,
                        run.id,
                        persist_exc,
                    )

            return error_event

    async def _stream_chat_events(
            self,
            session_id: str,
            prepared: _PreparedChat,
            stream_cursor: Optional[str],
    ) -> AsyncGenerator[BaseEvent, None]:
        """消费Runner输出并转换为当前SSE连接的事件流。"""
        # ------------------------------------------------------------
        # 第二阶段：消费Runner输出并发送SSE
        #
        # 这里发生错误，只表示当前客户端没有成功接收事件。
        # 后台Runner可能仍在执行，因此：
        # 1.不保存ErrorEvent；
        # 2.不把SessionRun改成FAILED；
        # 3.只通知当前SSE客户端。
        # ------------------------------------------------------------
        run = prepared.run
        stream_task = prepared.stream_task
        delivered_event = False
        unread_reset_scheduled = False
        try:
            # 记录日志展示会话已启动
            logger.info(f"会话[{session_id}]已启动")
            logger.info(f"会话[{session_id}]Stream Task实例: {stream_task}")

            # 持续读取Redis输出流
            async for event in self._read_stream_task_events(
                    session_id=session_id,
                    run=run,
                    stream_task=stream_task,
                    stream_cursor=stream_cursor,
            ):
                yield event

                # 生成器在yield之后恢复，说明上层已经请求下一条事件。
                # 未读数量只重置一次，避免Plan、Step和Tool事件逐条更新数据库。
                delivered_event = True
                if not unread_reset_scheduled:
                    try:
                        asyncio.create_task(
                            self._safe_update_unread_count(session_id)
                        )
                        unread_reset_scheduled = True
                    except RuntimeError:
                        # 事件循环已关闭时由finally保留最后一次尝试机会。
                        logger.warning(
                            f"会话[{session_id}]无法创建后台任务更新未读消息计数"
                        )

                # 成功结束事件已经交给上层后再删除Redis数据；失败和等待状态保留数据。
                # 如果客户端恰好断开，Runner设置的TTL仍会负责最终清理。
                if isinstance(event, DoneEvent):
                    await self._cleanup_completed_stream_task(
                        stream_task,
                        session_id,
                    )

            # 读取循环退出表示遇到终止事件，或Task结束后已无剩余输出。
            logger.info(
                "会话[%s]运行[%s]本次输出结束",
                session_id,
                run.id,
            )

        except asyncio.CancelledError:
            # SSE客户端断开不等于Agent执行失败
            logger.info(
                "会话[%s]的SSE连接被客户端取消",
                session_id,
            )
            raise

        except Exception as exc:
            # Runner产生的业务错误已经由Runner持久化。
            # 这里属于输出流消费错误，只返回前端，不写入session_events。

            error_message = str(exc) or type(exc).__name__

            logger.exception(
                "会话[%s]运行[%s]消费输出流失败: %s",
                session_id,
                run.id,
                error_message,
            )

            # 这是临时的传输层错误，只返回给当前客户端。
            # 不保存到session_events，也不修改SessionRun状态。
            yield ErrorEvent(
                run_id=run.id,
                error=(
                    "任务输出连接异常，请重新连接查看任务状态："
                    f"{error_message}"
                ),
            )
        finally:
            # 当前连接至少成功消费过一个事件后，才将未读消息数重置为0。
            # 注意：当SSE客户端断开连接时，sse_starlette使用anyio cancel scope取消当前Task中
            # 所有的await操作（asyncio.shield也无法对抗anyio的cancel scope）。
            # 如果在finally块中直接执行数据库操作，该操作会被立即取消，并且SQLAlchemy在尝试
            # 终止被中断的连接时也会被取消，从而产生ERROR日志并可能污染连接池。
            # 解决方案：将数据库更新操作放到独立的asyncio Task中执行，新Task不受当前
            # cancel scope的影响，可以正常完成数据库操作。
            if delivered_event and not unread_reset_scheduled:
                try:
                    asyncio.create_task(
                        self._safe_update_unread_count(session_id)
                    )
                except RuntimeError:
                    # 事件循环已关闭（如应用正在关闭），无法创建后台任务
                    logger.warning(f"会话[{session_id}]无法创建后台任务更新未读消息计数")

    # 对外入口只串联“任务编排”和“SSE输出”两个阶段。
    async def chat(
            self,
            session_id: str,
            message: Optional[str] = None,
            attachments: Optional[List[str]] = None,
            stream_cursor: Optional[str] = None,
            timestamp: Optional[datetime] = None,
            resume_run_id: str | None = None
    ) -> AsyncGenerator[BaseEvent, None]:
        """发送消息、恢复任务或重新连接当前任务的输出流。"""
        prepared = await self._prepare_chat(
            session_id=session_id,
            message=message,
            attachments=attachments,
            timestamp=timestamp,
            resume_run_id=resume_run_id,
        )

        if isinstance(prepared, ErrorEvent):
            yield prepared
            return

        # 第一阶段成功后，用户消息可以返回前端。
        if prepared.message_event is not None:
            yield prepared.message_event

        async for event in self._stream_chat_events(
                session_id=session_id,
                prepared=prepared,
                stream_cursor=stream_cursor,
        ):
            yield event

    async def interrupt_orphaned_runs(self) -> int:
        """中断服务启动前遗留且已失去执行实例的Run。"""
        uow = self._uow_factory()
        async with uow:
            return await uow.session_run.interrupt_orphaned_runs()

    async def stop_session(self, session_id: str) -> None:
        """停止会话的最新Run，并清除其进程内Stream Task绑定。"""
        # Session与最新Run在同一个查询事务中读取，离开上下文后不继续持有连接。
        async with self._uow_factory() as read_uow:
            session = await read_uow.session.get_by_id(session_id)
            run = await read_uow.session_run.get_latest_by_session_id(
                session_id
            )

        if session is None:
            raise RuntimeError("任务会话不存在")

        if run is None:
            return

        stream_task = self._get_stream_task(run)
        if stream_task is not None:
            stream_task.cancel()

        uow = self._uow_factory()
        async with uow:
            await uow.session_run.update_status(
                run.id,
                SessionRunStatus.INTERRUPTED,
            )
            if run.active_task_id:
                await uow.session_run.clear_task(
                    run.id,
                    expected_task_id=run.active_task_id,
                )

    async def shutdown(self) -> None:
        """关闭Agent服务"""
        logger.info("正在清除所有会话任务资源并释放")
        await self._stream_task_cls.destroy()
        logger.info("所有会话任务资源清除成功")
