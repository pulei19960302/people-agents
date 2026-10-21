#!/usr/bin/env python
# -*- coding: utf-8 -*-
import asyncio
import logging
from datetime import datetime
from typing import AsyncGenerator, Optional, List, Type, Callable

from pydantic import TypeAdapter

from app.domain.external.file_storage import FileStorage
from app.domain.external.json_parser import JSONParser
from app.domain.external.llm import LLM
from app.domain.external.sandbox import Sandbox
from app.domain.external.search import SearchEngine
from app.domain.external.task import Task
from app.domain.models.app_config import AgentConfig, MCPConfig, A2AConfig
from app.domain.models.event import BaseEvent, ErrorEvent, MessageEvent, Event, DoneEvent, WaitEvent
from app.domain.models.session import Session
from app.domain.models.session_event import SessionEvent
from app.domain.models.session_run import SessionRun, SessionRunStatus
from app.domain.repositories.uow import IUnitOfWork
from app.domain.services.agent_task_runner import AgentTaskRunner

logger = logging.getLogger(__name__)

# redis-py 8.x defaults to a 5-second socket timeout. Keep XREAD below it so
# an idle agent run returns control to this loop instead of triggering retries.
OUTPUT_STREAM_BLOCK_MS = 3_000


class AgentService:
    """Manus智能体服务"""

    def __init__(
            self,
            uow_factory: Callable[[], IUnitOfWork],
            llm: LLM,
            agent_config: AgentConfig,
            mcp_config: MCPConfig,
            a2a_config: A2AConfig,
            sandbox_cls: Type[Sandbox],
            task_cls: Type[Task],
            json_parser: JSONParser,
            search_engine: SearchEngine,
            file_storage: FileStorage,
    ) -> None:
        """构造函数，完成Agent服务初始化"""
        self._uow_factory = uow_factory
        self._uow = uow_factory()
        self._llm = llm
        self._agent_config = agent_config
        self._mcp_config = mcp_config
        self._a2a_config = a2a_config
        self._sandbox_cls = sandbox_cls
        self._task_cls = task_cls
        self._json_parser = json_parser
        self._search_engine = search_engine
        self._file_storage = file_storage
        logger.info(f"AgentService初始化成功")

    async def _get_task(self, run: SessionRun | None) -> Optional[Task]:
        """根据传递的任务会话获取任务实例"""

        if run is None or run.active_task_id is None:
            return None

        # 2.调用人物类的get方法获取对应的任务实例
        return self._task_cls.get(run.active_task_id)

    # 创建一个任务
    async def _create_task(self, session: Session, run: SessionRun) -> Task:
        """根据传递的会话创建一个新任务"""

        # 必须在bind_task之前保存。
        # 可能是PENDING、WAITING、INTERRUPTED或FAILED。
        resume_from_status = run.status
        # 1.获取沙箱实例
        sandbox = None
        sandbox_id = session.sandbox_id
        if sandbox_id:
            sandbox = await self._sandbox_cls.get(sandbox_id)

        # 2.判断是否能获取到沙箱(如果没有则创建)
        if not sandbox:
            # 3.沙箱不存在则创建一个新的(有可能被释放了)
            sandbox = await self._sandbox_cls.create()
            session.sandbox_id = sandbox.id
            async with self._uow:
                await self._uow.session.save(session)

        # 4.从沙箱中获取浏览器实例
        browser = await sandbox.get_browser()
        if not browser:
            logger.error(f"获取沙箱[{sandbox.id}]中的浏览器实例失败")
            raise RuntimeError(f"获取沙箱[{sandbox.id}]中的浏览器实例失败")

        # 5.创建AgentTaskRunner
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

        # 6.创建任务Task并更新会话中的信息
        task = self._task_cls.create(task_runner=task_runner)

        async with self._uow:
            await self._uow.session_run.bind_task(
                run_id=run.id,
                task_id=task.id,
            )

        run.active_task_id = task.id
        run.status = SessionRunStatus.RUNNING
        return task


    async def _resolve_run(
            self,
            session_id: str,
            resume_run_id: str | None = None
    ) -> SessionRun:

        """决定创建新Run，还是恢复已有Run。"""
        uow = self._uow_factory()

        async with uow:

            # 根据session id
            latest_run = (
                await uow.session_run.get_latest_by_session_id(session_id)
            )

            # 明确恢复某个Run
            if resume_run_id is not None:
                run = await uow.session_run.get_by_id(resume_run_id)
                if run is None:
                    raise RuntimeError(
                        f"运行[{resume_run_id}]不存在"
                    )

                if run.session_id != session_id:
                    raise RuntimeError(
                        f"运行[{resume_run_id}]不属于会话[{session_id}]"
                    )

                if not run.status.can_resume:
                    raise RuntimeError(
                        f"运行[{resume_run_id}]当前状态"
                        f"[{run.status.value}]不能恢复"
                    )

                # 当前Session已经有另一个活动Run时，不允许恢复旧Run。
                if (
                        latest_run is not None
                        and latest_run.id != run.id
                        and latest_run.status.is_active
                ):
                    raise RuntimeError(
                        f"会话[{session_id}]当前已有运行中的任务"
                        f"[{latest_run.id}]，不能同时恢复运行[{run.id}]"
                    )

                return run

            # 没有历史Run，创建第一轮任务。
            if latest_run is None:
                run = SessionRun(session_id=session_id)
                await uow.session_run.add(run)
                return run

            # Agent等待用户回答时，这条消息必须继续进入原Run。
            if latest_run.status == SessionRunStatus.WAITING:
                return latest_run

            # 当前任务还未启动完成或正在执行，拒绝创建第二个Run。
            if latest_run.status in {
                SessionRunStatus.PENDING,
                SessionRunStatus.RUNNING,
            }:
                raise RuntimeError(
                    f"会话[{session_id}]当前任务正在运行，"
                    "请等待任务完成后再发送新消息"
                )

            # COMPLETED、FAILED、INTERRUPTED、CANCELLED后，
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

    @staticmethod
    async def _cleanup_completed_task_streams(task: Task, session_id: str) -> None:
        """成功任务的终止事件已读取后，删除对应的Redis Stream。"""
        try:
            await task.input_stream.clear()
            await task.output_stream.clear()
            logger.info(f"会话[{session_id}]任务执行成功，Redis Stream已删除")
        except Exception as e:
            # Runner已经为成功任务设置TTL，立即删除失败时仍会自动过期。
            logger.warning(f"会话[{session_id}]删除Redis Stream失败: {e}")


    async def _persist_orchestration_error(
            self,
            session_id: str,
            run: SessionRun,
            error_event: ErrorEvent,
    ) -> None:
        """持久化任务启动前发生的编排错误。"""

        session_event = SessionEvent.from_event(
            session_id=session_id,
            event=error_event,
            run_id=run.id,
        )


        async with self._uow:
            await self._uow.session_event.add(session_event)
            await self._uow.session_run.update_status(
                run_id=run.id,
                status=SessionRunStatus.FAILED,
                error=error_event.error,
            )


    # 这个函数拆分成2个大部分。
    async def chat(
            self,
            session_id: str,
            message: Optional[str] = None,
            attachments: Optional[List[str]] = None,
            latest_event_id: Optional[str] = None,
            timestamp: Optional[datetime] = None,
            resume_run_id: str | None = None
    ) -> AsyncGenerator[BaseEvent, None]:
        """发送消息、恢复任务或重新连接当前任务的输出流。"""

        run: SessionRun | None = None
        task: Task | None = None
        message_event: MessageEvent | None = None

        # ------------------------------------------------------------
        # 第一阶段：任务编排
        #
        # 负责：
        # 1. 查询Session；
        # 2. 创建或恢复SessionRun；
        # 3. 创建RedisStreamTask；
        # 4. 保存用户消息；
        # 5. 写入Redis输入流；
        # 6. 启动后台Runner。
        #
        # 这里失败，说明任务尚未被Runner完整接管。
        # 如果已经有run，需要保存ErrorEvent并将run标记为FAILED。
        # ------------------------------------------------------------
        try:
            # 检查会话是否存在
            async with self._uow:
                session = await self._uow.session.get_by_id(session_id)
            if not session:
                logger.error(f"尝试与不存在的任务会话[{session_id}]对话")
                raise RuntimeError("任务会话不存在, 请核实后重试")

            # 获取run的一些方法
            # 有消息表示发起新任务、回答WAITING任务或恢复指定Run
            if message:
                run = await self._resolve_run(session_id=session_id, resume_run_id=resume_run_id)
            else:
                # 没有消息表示SSE重新连接，不能创建新Run
                async with self._uow:
                    if resume_run_id is not None:
                        run = await self._uow.session_run.get_by_id(
                            resume_run_id
                        )
                    else:
                        run = (
                            await self._uow.session_run
                            .get_latest_by_session_id(session_id)
                        )

                if run is None or run.session_id != session_id:
                    raise RuntimeError("当前会话没有可连接的任务运行")

            # 根据Run中的active_task_id查找进程内Task
            task = await self._get_task(run)

            # 处理用户消息
            if message and run is not None:

                # 如果run的状态是运行，并且task查询不到, 说明应用重启或Task意外丢失。
                if run.status == SessionRunStatus.RUNNING and task is None:
                    async with self._uow:
                        # 设置状态
                        await self._uow.session_run.update_status(
                            run.id,
                            SessionRunStatus.INTERRUPTED,
                            error="任务执行实例已经丢失",
                        )
                        # 清空task
                        if run.active_task_id is not None:
                            await self._uow.session_run.clear_task(
                                run_id=run.id,
                                expected_task_id=run.active_task_id,
                            )

                    # 同步内存对象，避免后续代码误判。
                    run.status = SessionRunStatus.INTERRUPTED
                    run.active_task_id = None

                    logger.warning(
                        "会话[%s]运行[%s]的执行实例已经丢失，已标记为中断",
                        session_id,
                        run.id,
                    )

                    yield ErrorEvent(
                        run_id=run.id,
                        error="任务执行实例已经丢失，已标记为中断，请通过恢复任务接口继续",
                    )
                    return

                # WAITING、INTERRUPTED、FAILED可能保留失效task_id。
                if task is None and run.active_task_id is not None:
                    async with self._uow:
                        await self._uow.session_run.clear_task(
                            run_id=run.id,
                            expected_task_id=run.active_task_id,
                        )
                    run.active_task_id = None


                # 不存在执行实例时，为当前Run创建新Task
                if task is None:
                    task = await self._create_task(
                        session=session,
                        run=run,
                    )

                attachment_ids = attachments or []
                db_attachments = []

                async with self._uow:
                    for file_id in attachment_ids:
                        attachment = await self._uow.file.get_by_id(file_id)
                        if attachment is not None:
                            db_attachments.append(attachment)


                # 创建一个人类消息事件 并且保存起来
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

                async with self._uow:
                    await self._uow.session_event.add(session_event)
                    await self._uow.session.update_latest_message(
                        session_id=session_id,
                        message=message,
                        timestamp=timestamp or datetime.now(),
                    )

                logger.debug(
                    "用户消息事件保存成功: "
                    "session_id=%s, event_id=%s, stream_id=%s",
                    session_id,
                    message_event.id,
                )

                # 写入当前消息的Redis输入流
                stream_id = await task.input_stream.put(
                    message_event.model_dump_json()
                )

                logger.debug(
                    "用户消息写入成功: session_id=%s, run_id=%s, "
                    "task_id=%s, event_id=%s, stream_id=%s",
                    session_id,
                    run.id,
                    task.id,
                    message_event.id,
                    stream_id,
                )
                # 启动执行任务
                await task.invoke()

            elif task is None:
                raise RuntimeError(
                    "当前任务执行实例已经结束或丢失，请读取历史事件"
                )
        except asyncio.CancelledError:
            # 客户端在任务编排阶段断开连接，
            # 不应该因此把Run标记成FAILED。
            logger.info(
                "会话[%s]在任务编排阶段取消SSE连接",
                session_id,
            )
            raise

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
            if run is not None:
                try:
                    await self._persist_orchestration_error(
                        session_id=session_id,
                        run=run,
                        error_event=error_event,
                    )
                except Exception as persist_exc:
                    logger.exception(
                        "会话[%s]运行[%s]编排错误持久化失败: %s",
                        session_id,
                        run.id,
                        persist_exc,
                    )

            yield error_event
            return

        # 第一阶段成功后，用户消息可以返回前端。
        if message_event is not None:
            yield message_event

        if run is None or task is None:
            # 这是程序状态异常，正常情况下不会进入这里。
            yield ErrorEvent(
                run_id=run.id if run is not None else None,
                error="任务编排完成，但没有可用的Run或Task",
            )
            return


        # ------------------------------------------------------------
        # 第二阶段：消费Runner输出并发送SSE
        #
        # 这里发生错误，只表示当前客户端没有成功接收事件。
        # 后台Runner可能仍在执行，因此：
        # 1.不保存ErrorEvent；
        # 2.不把SessionRun改成FAILED；
        # 3.只通知当前SSE客户端。
        # ------------------------------------------------------------
        try:

            # 记录日志展示会话已启动
            logger.info(f"会话[{session_id}]已启动")
            logger.info(f"会话[{session_id}]任务实例: {task}")

            # 持续读取Redis输出流
            while True:
                # 从输出消息队列中获取数据
                stream_id, event_str = await task.output_stream.get(
                    start_id=latest_event_id,
                    block_ms=OUTPUT_STREAM_BLOCK_MS,
                )

                if event_str is None:
                    if task.done:
                        break
                    logger.debug(f"在会话[{session_id}]输出队列中未发现事件内容")
                    continue

                latest_event_id = stream_id

                # 前端已读取事件，清空未读数量
                event = TypeAdapter(Event).validate_json(event_str)

                logger.debug(
                    "从会话[%s]运行[%s]读取事件: %s",
                    session_id,
                    run.id,
                    type(event).__name__,
                )

                # 14.将未读消息数重置为0
                async with self._uow:
                    await self._uow.session.update_unread_message_count(session_id, 0)

                # 成功结束时删除Redis数据；失败和等待状态保留数据
                if isinstance(event, DoneEvent):
                    await self._cleanup_completed_task_streams(task, session_id)

                yield event

                # 等待、失败和完成都会结束本次SSE响应
                if isinstance(event, (DoneEvent, ErrorEvent, WaitEvent)):
                    break

            # 17.循环外面表示这次任务AI端的已结束
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
            # 这里属于请求编排错误，只返回前端，不写入session_events。

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
            # 会话完整传递给前端后，表示至少用户肯定收到了这些消息，所以不应该有未读消息数
            # 注意：当SSE客户端断开连接时，sse_starlette使用anyio cancel scope取消当前Task中
            # 所有的await操作（asyncio.shield也无法对抗anyio的cancel scope）。
            # 如果在finally块中直接执行数据库操作，该操作会被立即取消，并且SQLAlchemy在尝试
            # 终止被中断的连接时也会被取消，从而产生ERROR日志并可能污染连接池。
            # 解决方案：将数据库更新操作放到独立的asyncio Task中执行，新Task不受当前
            # cancel scope的影响，可以正常完成数据库操作。
            try:
                asyncio.create_task(self._safe_update_unread_count(session_id))
            except RuntimeError:
                # 事件循环已关闭（如应用正在关闭），无法创建后台任务
                logger.warning(f"会话[{session_id}]无法创建后台任务更新未读消息计数")

    async def interrupt_orphaned_runs(self) -> int:
        """中断服务启动前遗留且已失去执行实例的Run。"""
        uow = self._uow_factory()
        async with uow:
            return await uow.session_run.interrupt_orphaned_runs()

    async def stop_session(self, session_id: str) -> None:
        """根据传递的会话id停止指定会话"""
        # 1.查找会话是否存在
        async with self._uow:
            session = await self._uow.session.get_by_id(session_id)
            run = await self._uow.session_run.get_latest_by_session_id(
                session_id
            )

        if session is None:
            raise RuntimeError("任务会话不存在")

        if run is None:
            return

        task = await self._get_task(run)
        if task is not None:
            task.cancel()

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
        await self._task_cls.destroy()
        logger.info("所有会话任务资源清除成功")
