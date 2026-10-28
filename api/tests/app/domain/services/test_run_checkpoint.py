import asyncio

from app.domain.models.event import (
    DoneEvent,
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
)
from app.domain.models.session_event import SessionEvent
from app.domain.models.session_run import SessionRun, SessionRunStatus
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.flows.base import FlowStatus
from app.domain.services.flows.planner_react import PlannerReActFlow


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
