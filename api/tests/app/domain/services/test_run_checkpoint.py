import asyncio
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from app.domain.models.event import (
    DoneEvent,
    ErrorEvent,
    PlanEvent,
    PlanEventStatus,
    StepEvent,
    StepEventStatus,
    WaitEvent,
)
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.models.run_checkpoint import (
    RunCheckpoint,
    RunCheckpointPhase,
    RunStopReason,
)
from app.domain.models.session_event import SessionEvent
from app.domain.models.session_run import SessionRun, SessionRunStatus
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.flows.base import FlowStatus
from app.domain.services.flows.planner_react import PlannerReActFlow
from app.domain.exceptions import RunBudgetExceeded


class FakeEventRepository:
    def __init__(self, operations: list[str]) -> None:
        self.operations = operations
        self.events: list[SessionEvent] = []
        self.by_event_id: dict[str, SessionEvent] = {}

    async def add(self, event: SessionEvent) -> None:
        self.operations.append("event")
        self.events.append(event)
        self.by_event_id[event.event_id] = event

    async def get_by_event_id(self, event_id: str) -> SessionEvent | None:
        return self.by_event_id.get(event_id)


class FakeRunRepository:
    def __init__(self, operations: list[str]) -> None:
        self.operations = operations
        self.checkpoint: RunCheckpoint | None = None
        self.status: SessionRunStatus | None = None

    async def update_checkpoint(
        self,
        run_id: str,
        checkpoint: RunCheckpoint,
    ) -> None:
        assert run_id == "run-1"
        self.operations.append("checkpoint")
        self.checkpoint = checkpoint.model_copy(deep=True)

    async def update_status(
        self,
        run_id: str,
        status: SessionRunStatus,
        error: str | None = None,
    ) -> None:
        assert run_id == "run-1"
        self.operations.append("status")
        self.status = status


class FakeUnitOfWork:
    def __init__(
        self,
        event_repository: FakeEventRepository,
        run_repository: FakeRunRepository,
        operations: list[str],
    ) -> None:
        self.session_event = event_repository
        self.session_run = run_repository
        self.operations = operations

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, exc_type, *_args: object) -> bool:
        self.operations.append("rollback" if exc_type else "commit")
        return False


class FakeOutputStream:
    def __init__(self, operations: list[str]) -> None:
        self.operations = operations

    async def put(self, message: str) -> str:
        self.operations.append("redis")
        return "1-0"


class FakeTask:
    def __init__(self, operations: list[str]) -> None:
        self.output_stream = FakeOutputStream(operations)


class FailingOutputStream(FakeOutputStream):
    async def put(self, message: str) -> str:
        self.operations.append("redis")
        raise ConnectionError("Redis unavailable")


def make_runner(
    event_repository: FakeEventRepository,
    run_repository: FakeRunRepository,
    operations: list[str],
) -> AgentTaskRunner:
    runner = object.__new__(AgentTaskRunner)
    runner._session_id = "session-1"
    runner._run_id = "run-1"
    runner._checkpoint = RunCheckpoint()
    runner._uow_factory = lambda: FakeUnitOfWork(
        event_repository,
        run_repository,
        operations,
    )
    return runner


def test_runner_persists_event_and_checkpoint_before_redis() -> None:
    async def check() -> None:
        operations: list[str] = []
        events = FakeEventRepository(operations)
        runs = FakeRunRepository(operations)
        runner = make_runner(events, runs, operations)
        task = FakeTask(operations)
        step = Step(id="step-1", description="核查资料")

        await runner._put_and_add_event(
            task,
            StepEvent(step=step, status=StepEventStatus.STARTED),
        )

        assert operations == ["event", "checkpoint", "commit", "redis"]
        assert runs.checkpoint is not None
        assert runs.checkpoint.flow_status == RunCheckpointPhase.EXECUTING
        assert runs.checkpoint.current_step_id == "step-1"

    asyncio.run(check())


def test_step_result_selects_matching_recovery_phase() -> None:
    """成功步骤恢复后继续执行；未成功步骤恢复后才重新规划。"""
    operations: list[str] = []
    runner = make_runner(
        FakeEventRepository(operations),
        FakeRunRepository(operations),
        operations,
    )

    successful_step = Step(
        id="step-success",
        description="成功步骤",
        status=ExecutionStatus.COMPLETED,
        success=True,
    )
    successful_checkpoint = runner._build_checkpoint(
        StepEvent(
            step=successful_step,
            status=StepEventStatus.COMPLETED,
        )
    )

    assert successful_checkpoint is not None
    assert (
        successful_checkpoint.flow_status
        == RunCheckpointPhase.EXECUTING
    )

    unsuccessful_step = Step(
        id="step-failed",
        description="未取得结果的步骤",
        status=ExecutionStatus.COMPLETED,
        success=False,
    )
    unsuccessful_checkpoint = runner._build_checkpoint(
        StepEvent(
            step=unsuccessful_step,
            status=StepEventStatus.COMPLETED,
        )
    )

    assert unsuccessful_checkpoint is not None
    assert (
        unsuccessful_checkpoint.flow_status
        == RunCheckpointPhase.UPDATING
    )


def test_wait_event_and_status_are_committed_together() -> None:
    async def check() -> None:
        operations: list[str] = []
        events = FakeEventRepository(operations)
        runs = FakeRunRepository(operations)
        runner = make_runner(events, runs, operations)
        task = FakeTask(operations)

        await runner._put_and_add_event(
            task,
            WaitEvent(
                tool_call_id="ask-1",
                tool_name="message",
                function_name="message_ask_user",
            ),
        )

        assert operations == [
            "event",
            "checkpoint",
            "status",
            "commit",
            "redis",
        ]
        assert runs.status == SessionRunStatus.WAITING
        assert runs.checkpoint is not None
        assert runs.checkpoint.flow_status == RunCheckpointPhase.WAITING
        assert runs.checkpoint.pending_tool_call is not None
        assert runs.checkpoint.pending_tool_call.tool_call_id == "ask-1"
        assert runs.checkpoint.pending_tool_call.waiting_for_user is True

    asyncio.run(check())


def test_redis_failure_does_not_rollback_completed_run() -> None:
    async def check() -> None:
        operations: list[str] = []
        events = FakeEventRepository(operations)
        runs = FakeRunRepository(operations)
        runner = make_runner(events, runs, operations)
        task = FakeTask(operations)
        task.output_stream = FailingOutputStream(operations)

        await runner._put_and_add_event(task, DoneEvent())

        assert operations == [
            "event",
            "checkpoint",
            "status",
            "commit",
            "redis",
        ]
        assert runs.status == SessionRunStatus.COMPLETED
        assert runs.checkpoint is not None
        assert runs.checkpoint.flow_status == RunCheckpointPhase.COMPLETED
        assert len(events.events) == 1

    asyncio.run(check())


def test_flow_restores_updating_step_from_checkpoint() -> None:
    async def check() -> None:
        operations: list[str] = []
        events = FakeEventRepository(operations)
        runs = FakeRunRepository(operations)
        original_step = Step(id="step-1", description="核查资料")
        plan_event = PlanEvent(
            id="plan-event-1",
            run_id="run-1",
            status=PlanEventStatus.CREATED,
            plan=Plan(id="plan-1", steps=[original_step]),
        )
        stored_plan = SessionEvent.from_event(
            session_id="session-1",
            event=plan_event,
            run_id="run-1",
        )
        events.by_event_id[stored_plan.event_id] = stored_plan

        completed_step = original_step.model_copy(
            update={
                "status": ExecutionStatus.COMPLETED,
                "success": True,
                "result": "已完成",
            }
        )
        checkpoint = RunCheckpoint(
            flow_status=RunCheckpointPhase.UPDATING,
            plan_event_id=plan_event.id,
            current_step=completed_step,
        )
        run = SessionRun(
            id="run-1",
            session_id="session-1",
            status=SessionRunStatus.INTERRUPTED,
            checkpoint=checkpoint.model_dump(mode="json"),
        )

        flow = object.__new__(PlannerReActFlow)
        flow._session_id = "session-1"
        flow._run_id = "run-1"
        flow._uow_factory = lambda: FakeUnitOfWork(
            events,
            runs,
            operations,
        )
        flow.plan = None
        flow.status = FlowStatus.IDLE

        restored_step = await flow._restore_checkpoint(run)

        assert flow.status == FlowStatus.UPDATING
        assert restored_step is not None
        assert restored_step.status == ExecutionStatus.COMPLETED
        assert flow.plan is not None
        assert flow.plan.steps[0].result == "已完成"

    asyncio.run(check())


def test_failed_step_event_keeps_execution_phase_and_error_is_atomic():
    async def check():
        operations = []
        events, runs = FakeEventRepository(operations), FakeRunRepository(operations)
        runner = make_runner(events, runs, operations)
        step = Step(id="same-step", status=ExecutionStatus.FAILED, error="LLM 503")
        runner._build_checkpoint(StepEvent(step=step, status=StepEventStatus.FAILED))
        await runner._put_and_add_event(FakeTask(operations), ErrorEvent(error="LLM 503"))
        assert operations == ["event", "checkpoint", "status", "commit", "redis"]
        assert runs.status == SessionRunStatus.FAILED
        assert runs.checkpoint.flow_status == RunCheckpointPhase.EXECUTING
        assert runs.checkpoint.current_step_id == "same-step"
        assert runs.checkpoint.stop_reason == RunStopReason.EXECUTION_ERROR
    asyncio.run(check())


@pytest.mark.parametrize("phase", [
    RunCheckpointPhase.PLANNING, RunCheckpointPhase.UPDATING,
    RunCheckpointPhase.SUMMARIZING,
])
def test_execution_exception_does_not_change_non_execution_phase(phase):
    runner = make_runner(FakeEventRepository([]), FakeRunRepository([]), [])
    runner._checkpoint = RunCheckpoint(flow_status=phase)
    checkpoint = runner._build_checkpoint(ErrorEvent(error="请求超时"))
    assert checkpoint.flow_status == phase
    assert checkpoint.stop_reason == RunStopReason.EXECUTION_ERROR


@pytest.mark.parametrize("phase,step_status,reason,expected", [
    (RunCheckpointPhase.EXECUTING, ExecutionStatus.FAILED, RunStopReason.EXECUTION_ERROR, ExecutionStatus.RUNNING),
    (RunCheckpointPhase.EXECUTING, ExecutionStatus.RUNNING, RunStopReason.INTERRUPTED, ExecutionStatus.RUNNING),
    (RunCheckpointPhase.EXECUTING, ExecutionStatus.COMPLETED, RunStopReason.BUDGET_EXHAUSTED, ExecutionStatus.RUNNING),
    (RunCheckpointPhase.UPDATING, ExecutionStatus.COMPLETED, RunStopReason.EXECUTION_ERROR, ExecutionStatus.COMPLETED),
    # 旧数据没有stop_reason，仍按明确记录的UPDATING恢复，不猜测错误原因。
    (RunCheckpointPhase.UPDATING, ExecutionStatus.FAILED, None, ExecutionStatus.FAILED),
])
def test_restore_reactivates_only_current_execution_step(phase, step_status, reason, expected):
    async def check():
        flow = object.__new__(PlannerReActFlow)
        flow._run_id = "run-1"
        done = Step(id="done", status=ExecutionStatus.COMPLETED, success=True)
        current = Step(id="same-step", status=step_status, error="旧错误", result="已有资料")
        flow.plan = Plan(steps=[done, Step(id=current.id), Step(id="next")])
        flow._restore_plan = AsyncMock(return_value=True)
        checkpoint = RunCheckpoint(flow_status=phase, current_step=current, stop_reason=reason)
        run = SessionRun(id="run-1", session_id="session-1", checkpoint=checkpoint.model_dump(mode="json"))
        restored = await flow._restore_checkpoint(run)
        assert restored is flow.plan.steps[1]
        assert restored.id == "same-step" and restored.status == expected
        assert restored.result == "已有资料"
        assert flow.plan.steps[0] is done and done.success
        assert len(flow.plan.steps) == 3
        assert flow.status.value == phase.value
        if expected == ExecutionStatus.RUNNING:
            assert restored.error is None
            assert flow.plan.get_next_step() is restored
        else:
            assert restored.error == "旧错误"
    asyncio.run(check())


def test_budget_partial_result_is_resumable_without_replanning():
    runner = make_runner(FakeEventRepository([]), FakeRunRepository([]), [])
    partial = Step(id="same-step", status=ExecutionStatus.COMPLETED, success=False, result="部分资料")
    runner._build_checkpoint(StepEvent(step=partial, status=StepEventStatus.COMPLETED))
    checkpoint = runner._build_checkpoint(ErrorEvent(
        error="额度已耗尽", stop_reason=RunStopReason.BUDGET_EXHAUSTED,
    ))
    assert checkpoint.flow_status == RunCheckpointPhase.EXECUTING
    assert checkpoint.stop_reason == RunStopReason.BUDGET_EXHAUSTED
    assert checkpoint.current_step.result == "部分资料"
    checkpoint = runner._build_checkpoint(StepEvent(
        step=partial.model_copy(update={"status": ExecutionStatus.RUNNING}),
        status=StepEventStatus.STARTED,
    ))
    assert checkpoint.stop_reason is None


@pytest.mark.parametrize("exception,reason,status", [
    (RuntimeError("LLM 503"), RunStopReason.EXECUTION_ERROR, SessionRunStatus.FAILED),
    (RunBudgetExceeded("额度耗尽"), RunStopReason.BUDGET_EXHAUSTED, SessionRunStatus.FAILED),
    (asyncio.CancelledError(), RunStopReason.INTERRUPTED, SessionRunStatus.INTERRUPTED),
])
def test_runner_records_typed_stop_reason(exception, reason, status):
    async def check():
        operations = []
        events, runs = FakeEventRepository(operations), FakeRunRepository(operations)
        runner = make_runner(events, runs, operations)
        runner._uow = runner._uow_factory()
        runner._checkpoint = RunCheckpoint(
            flow_status=RunCheckpointPhase.EXECUTING,
            current_step=Step(id="same-step", status=ExecutionStatus.RUNNING),
        )
        runner._load_checkpoint = AsyncMock()
        runner._cleanup_tools = AsyncMock()
        runner._sandbox = SimpleNamespace(ensure_sandbox=AsyncMock(side_effect=exception))
        if isinstance(exception, asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await runner.invoke(FakeTask(operations))
        else:
            await runner.invoke(FakeTask(operations))
        assert runs.checkpoint.stop_reason == reason
        assert runs.checkpoint.flow_status == RunCheckpointPhase.EXECUTING
        assert runs.checkpoint.current_step_id == "same-step"
        assert runs.status == status
        runner._cleanup_tools.assert_awaited_once()
    asyncio.run(check())
