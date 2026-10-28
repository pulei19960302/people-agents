#!/usr/bin/env python
# -*- coding: utf-8 -*-
import uuid
from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel, Field

from .file import File


class Session(BaseModel):
    """会话领域模型"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))  # 会话id
    sandbox_id: Optional[str] = None  # 沙箱id
    title: str = ""  # 标题
    unread_message_count: int = 0  # 未读消息数
    latest_message: str = ""  # 最新消息
    latest_message_at: Optional[datetime] = None  # 最新消息时间
    files: List[File] = Field(default_factory=list)  # 文件列表
    updated_at: datetime = Field(default_factory=datetime.now)  # 更新时间
    created_at: datetime = Field(default_factory=datetime.now)  # 创建时间
