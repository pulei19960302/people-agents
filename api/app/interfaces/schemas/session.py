#!/usr/bin/env python
# -*- coding: utf-8 -*-
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.domain.models.file import File
from app.domain.models.session_run import SessionRunStatus


class CreateSessionResponse(BaseModel):
    """创建会话响应结构"""
    session_id: str  # 会话id


class ListSessionItem(BaseModel):
    """会话列表条目基础信息"""
    session_id: str = ""
    title: str = ""
    latest_message: str = ""
    latest_message_at: Optional[datetime] = None
    run_id: str | None = None
    status: SessionRunStatus = SessionRunStatus.PENDING
    can_resume: bool = False
    unread_message_count: int = 0


class ListSessionResponse(BaseModel):
    """获取会话列表基础信息响应结构"""
    sessions: List[ListSessionItem]


class ChatRequest(BaseModel):
    """聊天请求结构"""
    message: Optional[str] = None  # 人类消息
    attachments: Optional[List[str]] = Field(default_factory=list)  # 附件列表(传递的是文件id列表)
    event_id: Optional[str] = None  # 最新事件id
    timestamp: Optional[int] = None  # 当前时间戳


class GetSessionResponse(BaseModel):
    """获取会话详情响应结构"""
    session_id: str
    title: Optional[str] = None
    run_id: str | None = None
    status: SessionRunStatus
    can_resume: bool = False


class GetSessionFilesResponse(BaseModel):
    """获取会话文件列表响应结构"""
    files: List[File] = Field(default_factory=list)


class FileReadRequest(BaseModel):
    """需要读取的沙箱文件请求结构"""
    filepath: str


class FileReadResponse(BaseModel):
    """需要读取的沙箱文件响应结构体"""
    filepath: str
    content: str


class ShellReadRequest(BaseModel):
    """需要读取的沙箱shell请求结构体"""
    session_id: str  # Shell会话id


class ConsoleRecord(BaseModel):
    """控制台记录模型，包含ps1、command、output"""
    ps1: str
    command: str
    output: str


class ShellReadResponse(BaseModel):
    """需要读取的沙箱shell响应结构体"""
    session_id: str
    output: str
    console_records: List[ConsoleRecord] = Field(default_factory=list)


class ResumeRunRequest(BaseModel):
    message: str = "继续执行这个任务"
    attachments: list[str] = Field(default_factory=list)
    event_id: str | None = None
    timestamp: int | None = None
