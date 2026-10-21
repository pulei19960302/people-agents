#!/usr/bin/env python
# -*- coding: utf-8 -*-
import logging
from dataclasses import dataclass
from typing import List, Callable, Type

from app.application.errors.exceptions import NotFoundError, ServerRequestsError
from app.domain.external.sandbox import Sandbox
from app.domain.models.file import File
from app.domain.models.session import Session
from app.domain.models.session_run import SessionRun, SessionRunStatus
from app.domain.repositories.uow import IUnitOfWork
from app.interfaces.schemas.session import FileReadResponse, ShellReadResponse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionState:
    """会话基础信息及其最新一次运行状态。"""

    session: Session
    latest_run: SessionRun | None

    @property
    def run_id(self) -> str | None:
        return self.latest_run.id if self.latest_run else None

    @property
    def status(self) -> SessionRunStatus:
        if self.latest_run is None:
            return SessionRunStatus.PENDING
        return self.latest_run.status

    @property
    def can_resume(self) -> bool:
        return self.latest_run.status.can_resume if self.latest_run else False


class SessionService:
    """会话服务"""

    def __init__(
            self,
            uow_factory: Callable[[], IUnitOfWork],
            sandbox_cls: Type[Sandbox],
    ) -> None:
        """构造函数，完成会话服务初始化"""
        self._uow_factory = uow_factory
        self._uow = uow_factory()
        self._sandbox_cls = sandbox_cls

    async def create_session(self) -> Session:
        """创建一个空白的新任务会话"""
        logger.info(f"创建一个空白新任务会话")
        session = Session(title="新对话")
        async with self._uow:
            await self._uow.session.save(session)
        logger.info(f"成功创建一个新任务会话: {session.id}")
        return session

    async def get_all_sessions(self) -> List[Session]:
        """获取项目所有任务会话列表"""
        async with self._uow:
            return await self._uow.session.get_all()

    async def get_all_session_states(self) -> list[SessionState]:
        """获取所有会话及其最新一次运行，避免列表接口逐条查询Run。"""
        uow = self._uow_factory()
        async with uow:
            sessions = await uow.session.get_all()
            latest_runs = await uow.session_run.get_latest_by_session_ids(
                [session.id for session in sessions]
            )

        return [
            SessionState(
                session=session,
                latest_run=latest_runs.get(session.id),
            )
            for session in sessions
        ]

    async def clear_unread_message_count(self, session_id: str) -> None:
        """清空指定会话未读消息数"""
        logger.info(f"清除会话[{session_id}]未读消息数")
        async with self._uow:
            await self._uow.session.update_unread_message_count(session_id, 0)

    async def delete_session(self, session_id: str) -> None:
        """根据传递的会话id删除任务会话"""
        # 1.先检查会话是否存在
        logger.info(f"正在删除会话, 会话id: {session_id}")
        async with self._uow:
            session = await self._uow.session.get_by_id(session_id)
        if not session:
            logger.error(f"会话[{session_id}]不存在, 删除失败")
            raise NotFoundError(f"会话[{session_id}]不存在, 删除失败")

        # 2.根据传递的会话id删除会话
        async with self._uow:
            await self._uow.session.delete_by_id(session_id)
        logger.info(f"删除会话[{session_id}]成功")

    async def get_session(self, session_id: str) -> Session:
        """获取指定会话详情信息"""
        async with self._uow:
            return await self._uow.session.get_by_id(session_id)

    async def get_session_state(
            self,
            session_id: str,
    ) -> SessionState | None:
        """获取指定会话及其最新一次运行。"""
        uow = self._uow_factory()
        async with uow:
            session = await uow.session.get_by_id(session_id)
            if session is None:
                return None

            latest_run = await uow.session_run.get_latest_by_session_id(
                session_id
            )

        return SessionState(session=session, latest_run=latest_run)

    async def get_session_files(self, session_id: str) -> List[File]:
        """根据传递的会话id获取指定会话的文件列表信息"""
        logger.info(f"获取指定会话[{session_id}]下的文件列表信息")
        async with self._uow:
            session = await self._uow.session.get_by_id(session_id)
        if not session:
            raise RuntimeError(f"当前会话不存在[{session_id}], 请核实后重试")
        return session.files

    async def read_file(self, session_id: str, filepath: str) -> FileReadResponse:
        """根据传递的信息查看会话中指定文件的内容"""
        # 1.检查会话是否存在
        logger.info(f"获取会话[{session_id}]中的文件内容, 文件路径: {filepath}")
        async with self._uow:
            session = await self._uow.session.get_by_id(session_id)
        if not session:
            raise RuntimeError(f"当前会话不存在[{session_id}], 请核实后重试")

        # 2.根据沙箱id获取沙箱并判断是否存在
        if not session.sandbox_id:
            raise NotFoundError("当前会话无沙箱环境")
        sandbox = await self._sandbox_cls.get(session.sandbox_id)
        if not sandbox:
            raise NotFoundError("当前会话沙箱不存在或已销毁")

        # 3.调用沙箱读取文件内容
        result = await sandbox.read_file(filepath)
        if result.success:
            return FileReadResponse(**result.data)

        raise ServerRequestsError(result.message)

    async def read_shell_output(self, session_id: str, shell_session_id: str) -> ShellReadResponse:
        """根据传递的任务会话id+Shell会话id获取Shell执行结果"""
        # 1.检查会话是否存在
        logger.info(f"获取会话[{session_id}]中的Shell内容输出, Shell标识符: {shell_session_id}")
        async with self._uow:
            session = await self._uow.session.get_by_id(session_id)
        if not session:
            raise RuntimeError(f"当前会话不存在[{session_id}], 请核实后重试")

        # 2.根据沙箱id获取沙箱并判断是否存在
        if not session.sandbox_id:
            raise NotFoundError("当前会话无沙箱环境")
        sandbox = await self._sandbox_cls.get(session.sandbox_id)
        if not sandbox:
            raise NotFoundError("当前会话沙箱不存在或已销毁")

        # 3.调用沙箱查看shell内容
        result = await sandbox.read_shell_output(session_id=shell_session_id, console=True)
        if result.success:
            return ShellReadResponse(**result.data)

        raise ServerRequestsError(result.message)

    async def get_vnc_url(self, session_id: str) -> str:
        """获取指定会话的vnc链接"""
        # 1.检查会话是否存在
        logger.info(f"获取会话[{session_id}]的VNC链接")
        async with self._uow:
            session = await self._uow.session.get_by_id(session_id)
        if not session:
            raise RuntimeError(f"当前会话不存在[{session_id}], 请核实后重试")

        # 2.根据沙箱id获取沙箱并判断是否存在
        if not session.sandbox_id:
            raise NotFoundError("当前会话无沙箱环境")
        sandbox = await self._sandbox_cls.get(session.sandbox_id)
        if not sandbox:
            raise NotFoundError("当前会话沙箱不存在或已销毁")

        return sandbox.vnc_url
