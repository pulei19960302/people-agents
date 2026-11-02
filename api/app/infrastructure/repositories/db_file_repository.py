#!/usr/bin/env python
# -*- coding: utf-8 -*-
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models.file import File
from app.domain.repositories.file_repository import FileRepository
from app.infrastructure.models import FileModel


class DBFileRepository(FileRepository):
    """基于数据库的文件数据仓库"""

    def __init__(self, db_session: AsyncSession) -> None:
        """构造函数，完成数据仓库初始化"""
        self.db_session = db_session

    async def save(self, file: File) -> None:
        """根据传递的文件模型存储or更新数据"""
        # 1.根据id查询记录是否存在
        stmt = select(FileModel).where(FileModel.id == file.id)
        result = await self.db_session.execute(stmt)
        record = result.scalar_one_or_none()

        # 2.判断如果文件不存在则新建文件
        if not record:
            record = FileModel.from_domain(file)
            self.db_session.add(record)
            return

        # 3.文件存在则直接更新文件
        record.update_from_domain(file)

    async def get_by_id(self, file_id: str) -> Optional[File]:
        """根据传递的文件id获取文件信息"""
        # 1.根据id查询记录是否存在
        stmt = select(FileModel).where(FileModel.id == file_id)
        result = await self.db_session.execute(stmt)
        record = result.scalar_one_or_none()

        # 2.判断文件记录是否存在返回不同的值
        return record.to_domain() if record is not None else None

    async def get_by_ids(self, file_ids: list[str]) -> list[File]:
        """批量获取文件，并保持调用方传入的顺序。"""
        if not file_ids:
            return []

        # 一次查询取回全部文件，避免附件数量增加时产生N+1查询。
        stmt = select(FileModel).where(FileModel.id.in_(set(file_ids)))
        result = await self.db_session.execute(stmt)
        records_by_id = {
            record.id: record.to_domain()
            for record in result.scalars().all()
        }

        return [
            records_by_id[file_id]
            for file_id in file_ids
            if file_id in records_by_id
        ]
