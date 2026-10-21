#!/usr/bin/env python
# -*- coding: utf-8 -*-
import asyncio
import logging
from typing import Optional

from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.domain.repositories.uow import IUnitOfWork
from .db_file_repository import DBFileRepository
from .db_session_event_repository import DBSessionEventRepository
from .db_session_repository import DBSessionRepository
from .db_session_run_repository import DBSessionRunRepository

logger = logging.getLogger(__name__)


class DBUnitOfWork(IUnitOfWork):
    """基于Postgres数据库的UoW实例"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        """构造函数，完成UoW类初始化"""
        self.session_factory = session_factory
        self.db_session: Optional[AsyncSession] = None

    async def commit(self):
        """提交数据库持久化"""
        await self.db_session.commit()

    async def rollback(self):
        """数据库回退操作"""
        await self.db_session.rollback()

    async def __aenter__(self) -> "DBUnitOfWork":
        """进入UoW操作上下文管理器的逻辑"""
        # 1.为每个上下文开启一个新的会话
        self.db_session = self.session_factory()

        # 2.初始化所有数据库仓库
        self.file = DBFileRepository(db_session=self.db_session)
        self.session = DBSessionRepository(db_session=self.db_session)
        self.session_event = DBSessionEventRepository(db_session=self.db_session)
        self.session_run = DBSessionRunRepository(db_session=self.db_session)

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """正常退出时提交，异常退出时回滚，并传播所有异常。"""
        if self.db_session is None:
            return False

        try:
            if exc_type is None:
                await self.commit()
            else:
                await self.rollback()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("UoW提交或回滚失败")
            raise
        finally:
            try:
                await self.db_session.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("UoW关闭数据库会话失败")
                raise

        return False
