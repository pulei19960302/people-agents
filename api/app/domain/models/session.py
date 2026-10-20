#!/usr/bin/env python
# -*- coding: utf-8 -*-
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict

from pydantic import BaseModel, Field

from .event import Event, PlanEvent
from .file import File
from .memory import Memory
from .plan import Plan


class SessionStatus(str, Enum):
    """会话状态类型枚举"""
    PENDING = "pending"  # 等待任务
    RUNNING = "running"  # 运行中
    WAITING = "waiting"  # 等待人类响应
    COMPLETED = "completed"  # 已完成
    FAILED = "failed"  # 失败


class Session(BaseModel):
    """会话领域模型"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))  # 会话id
    sandbox_id: Optional[str] = None  # 沙箱id
    task_id: Optional[str] = None  # 任务id
    title: str = ""  # 标题
    unread_message_count: int = 0  # 未读消息数
    latest_message: str = ""  # 最新消息
    latest_message_at: Optional[datetime] = None  # 最新消息时间
    files: List[File] = Field(default_factory=list)  # 文件列表
    memories: Dict[str, Memory] = Field(default_factory=dict)  # 记忆
    status: SessionStatus = SessionStatus.PENDING  # 状态
    updated_at: datetime = Field(default_factory=datetime.now)  # 更新时间
    created_at: datetime = Field(default_factory=datetime.now)  # 创建时间