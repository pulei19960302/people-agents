from datetime import datetime, timezone
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    """返回带时区的UTC时间。"""
    return datetime.now(timezone.utc)


class SessionRunStatus(str, Enum):
    """一轮Agent任务的运行状态。"""
    PENDING = "pending" # 已创建，尚未开始执行
    RUNNING = "running" # 正在执行
    WAITING = "waiting"  # 等待用户输入
    INTERRUPTED = "interrupted"  # 执行被中断，可以恢复
    COMPLETED = "completed"  # 正常执行完成
    FAILED = "failed"  # 执行失败，可以由用户决定是否重试
    CANCELLED = "cancelled"  # 用户明确放弃，不再继续


    @property
    def is_active(self) -> bool:
        """是否属于当前正在进行的任务。"""
        return self in {
            SessionRunStatus.PENDING,
            SessionRunStatus.RUNNING,
            SessionRunStatus.WAITING,
        }


    # FAILED 属于下面2种 交给用户来判断
    @property
    def can_resume(self) -> bool:
        """当前状态是否允许恢复执行。"""
        return self in {
            SessionRunStatus.WAITING,
            SessionRunStatus.INTERRUPTED,
            SessionRunStatus.FAILED,
        }

    @property
    def is_terminal(self) -> bool:
        """是否属于终态"""
        return self in {
            SessionRunStatus.COMPLETED,
            SessionRunStatus.FAILED,
            SessionRunStatus.CANCELLED,
        }



class SessionRun(BaseModel):
    """
        Session中的一轮逻辑任务。
        id是稳定的run_id。任务中断后重新创建RedisStreamTask时，
        active_task_id可以变化，但id保持不变。
    """

    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="逻辑运行ID，同时作为事件的run_id",
    )

    session_id: str = Field(
        description="所属会话ID",
    )

    status: SessionRunStatus = Field(
        default=SessionRunStatus.PENDING,
        description="当前运行状态",
    )

    active_task_id: str | None = Field(
        default=None,
        description="当前RedisStreamTask ID，不代表稳定的run_id",
    )

    attempt_count: int = Field(
        default=0,
        ge=0,
        description="该逻辑任务已经启动的执行次数",
    )

    current_step_id: str | None = Field(
        default=None,
        description="当前正在执行或等待恢复的计划步骤ID",
    )

    checkpoint: dict[str, Any] = Field(
        default_factory=dict,
        description="恢复任务需要的流程检查点",
    )

    error: str | None = Field(
        default=None,
        description="最后一次执行错误",
    )

    started_at: datetime | None = Field(
        default=None,
        description="首次开始执行时间",
    )

    heartbeat_at: datetime | None = Field(
        default=None,
        description="当前执行实例最后一次心跳时间",
    )

    finished_at: datetime | None = Field(
        default=None,
        description="任务进入终态的时间",
    )

    created_at: datetime = Field(
        default_factory=utc_now,
        description="记录创建时间",
    )

    updated_at: datetime = Field(
        default_factory=utc_now,
        description="记录更新时间",
    )
