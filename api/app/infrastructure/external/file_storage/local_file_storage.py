#!/usr/bin/env python
# -*- coding: utf-8 -*-
import logging
import os
import shutil
import uuid
from datetime import datetime
from typing import Tuple, BinaryIO, Callable
from pathlib import Path

from fastapi import UploadFile

from app.domain.external.file_storage import FileStorage
from app.domain.models.file import File
from app.domain.repositories.uow import IUnitOfWork

logger = logging.getLogger(__name__)


class LocalFileStorage(FileStorage):
    """基于本地文件系统的文件存储实现"""

    def __init__(
            self,
            base_dir: str,
            uow_factory: Callable[[], IUnitOfWork],
    ) -> None:
        """构造函数，完成本地文件存储初始化"""
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._uow_factory = uow_factory
        self._uow = uow_factory()
        logger.info(f"本地文件存储初始化成功，存储目录: {self.base_dir}")

    async def upload_file(self, upload_file: UploadFile) -> File:
        """将文件保存到本地文件系统"""
        try:
            # 1.生成随机的uuid作为文件id并获取文件扩展名
            file_id = str(uuid.uuid4())
            _, file_extension = os.path.splitext(upload_file.filename)
            if not file_extension:
                file_extension = ""

            # 2.生成日期路径并拼接最终文件路径
            date_path = datetime.now().strftime("%Y/%m/%d")
            local_dir = self.base_dir / date_path
            local_dir.mkdir(parents=True, exist_ok=True)
            
            file_path = local_dir / f"{file_id}{file_extension}"
            
            # 3.保存文件到本地
            content = await upload_file.read()
            with open(file_path, "wb") as f:
                f.write(content)
            
            # 重置文件指针，以便后续使用
            await upload_file.seek(0)
            
            logger.info(f"文件上传成功: {upload_file.filename} -> {file_path} (ID: {file_id})")

            # 4.构建file模型并将数据存储到数据库中
            file = File(
                id=file_id,
                filename=upload_file.filename,
                key=str(file_path),  # 使用本地路径作为key
                extension=file_extension,
                mime_type=upload_file.content_type or "",
                size=len(content),
            )
            async with self._uow:
                await self._uow.file.save(file)

            return file
        except Exception as e:
            logger.error(f"上传文件[{upload_file.filename}]失败: {str(e)}")
            raise

    async def download_file(self, file_id: str) -> Tuple[BinaryIO, File]:
        """根据文件id查询数据并从本地文件系统下载文件"""
        try:
            # 1.查询对应的文件记录是否存在
            async with self._uow:
                file = await self._uow.file.get_by_id(file_id)
            if not file:
                raise ValueError(f"该文件不存在, 文件id: {file_id}")

            # 2.从本地文件系统读取文件
            file_path = Path(file.key)
            if not file_path.exists():
                raise FileNotFoundError(f"文件不存在: {file_path}")
            
            file_obj = open(file_path, "rb")
            
            # 3.返回文件流+文件信息
            return file_obj, file
        except Exception as e:
            logger.error(f"下载文件[{file_id}]失败: {str(e)}")
            raise
