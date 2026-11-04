import asyncio
from dataclasses import dataclass

import pytest
from unittest.mock import AsyncMock

from app.domain.exceptions import RunBudgetExceeded
from app.domain.models.run_checkpoint import RunCheckpoint, RunCheckpointPhase, RunStopReason
from sqlalchemy.dialects import postgresql

from app.domain.models.app_config import AgentConfig
from app.domain.models.event import PlanEvent, PlanEventStatus, MessageEvent, DoneEvent
from app.domain.models.message import Message
from app.domain.models.plan import Plan, Step, ExecutionStatus
from app.domain.services.flows.base import FlowStatus
from app.domain.models.session_run import SessionRun, SessionRunStatus
from app.domain.services.flows.planner_react import PlannerReActFlow
from app.infrastructure.models.session_event import SessionEventModel
from app.infrastructure.repositories.db_session_event_repository import (
    DBSessionEventRepository,
)


@dataclass
class FakeSessionEventRepository:
    tool_calls: int = 0
    started_steps: int = 0

    async def count_tool_calls_by_run(self, run_id: str) -> int:
        assert run_id == "run-1"
        return self.tool_calls

    async def count_started_steps_by_run(self, run_id: str) -> int:
        assert run_id == "run-1"
        return self.started_steps


class FakeUnitOfWork:
    def __init__(self, events: FakeSessionEventRepository) -> None:
        self.session_event = events
        self.session = self
        self.session_run = self

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get_by_id(self, item_id: str) -> object:
        if item_id == "run-1":
            return SessionRun(id="run-1", session_id="session-1")
        return object()


class FakePlanner:
    async def create_plan(self, message: Message):
        yield PlanEvent(
            status=PlanEventStatus.CREATED,
            plan=Plan(steps=[Step(id="1", description="步骤")]),
        )


class FakeReact:
    def __init__(self) -> None:
        self.calls = 0

    async def execute_step(self, *args: object):
        self.calls += 1
        if False:
            yield None


def make_flow(events: FakeSessionEventRepository, config: AgentConfig):
    flow = object.__new__(PlannerReActFlow)
    flow._uow_factory = lambda: FakeUnitOfWork(events)
    flow._uow = FakeUnitOfWork(events)
    flow._session_id = "session-1"
    flow._run_id = "run-1"
    flow._resume_from_status = SessionRunStatus.PENDING
    flow._agent_config = config
    flow.planner = FakePlanner()
    flow.react = FakeReact()
    flow.plan = None
    return flow


def test_run_tool_budget_blocks_execution_after_restart() -> None:
    async def check() -> None:
        flow = make_flow(
            FakeSessionEventRepository(tool_calls=12),
            AgentConfig(max_tool_calls_per_run=12),
        )
        with pytest.raises(RuntimeError, match="工具调用已达到上限"):
            async for _ in flow.invoke(Message(message="继续")):
                pass
        assert flow.react.calls == 0

    asyncio.run(check())


def test_run_step_budget_blocks_replanning_after_restart() -> None:
    async def check() -> None:
        flow = make_flow(
            FakeSessionEventRepository(started_steps=1),
            AgentConfig(max_plan_steps=1),
        )
        with pytest.raises(RuntimeError, match="执行步骤超过上限"):
            async for _ in flow.invoke(Message(message="继续")):
                pass
        assert flow.react.calls == 0

    asyncio.run(check())


def test_resume_current_step_does_not_consume_another_step_slot() -> None:
    async def check() -> None:
        flow = make_flow(
            FakeSessionEventRepository(tool_calls=12, started_steps=1),
            AgentConfig(max_tool_calls_per_run=36, max_plan_steps=1),
        )
        step = Step(id="1", status=ExecutionStatus.RUNNING)
        flow.plan = Plan(steps=[step])
        flow._resume_from_status = SessionRunStatus.FAILED

        async def repair():
            return False

        async def restore(_run):
            flow.status = FlowStatus.EXECUTING
            return step

        async def execute(_plan, current, _message):
            current.status, current.success = ExecutionStatus.COMPLETED, True
            yield MessageEvent(message="恢复完成")

        async def summarize():
            yield MessageEvent(message="结果")

        flow.react.repair_interrupted_tool_call = repair
        flow.react.execute_step = execute
        flow.react.summarize = summarize
        flow._restore_checkpoint = restore
        events = [event async for event in flow.invoke(Message(message="继续"))]
        assert isinstance(events[-1], DoneEvent)
        assert step.success

    asyncio.run(check())


def test_tool_and_step_budget_queries_filter_event_status() -> None:
    class FakeDBSession:
        def __init__(self) -> None:
            self.queries: list[str] = []

        async def execute(self, stmt):
            self.queries.append(str(stmt.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )))
            return self

        def scalar_one(self) -> int:
            return 3

    async def check() -> None:
        db = FakeDBSession()
        repository = DBSessionEventRepository(db)
        assert await repository.count_tool_calls_by_run("run-1") == 3
        assert await repository.count_started_steps_by_run("run-1") == 3
        assert "session_events.payload ->> 'status'" in db.queries[0]
        assert "calling" in db.queries[0]
        assert "started" in db.queries[1]
        assert "distinct" in db.queries[1]
        assert "'step'" in db.queries[1] and "'id'" in db.queries[1]
        assert SessionEventModel.__tablename__ == "session_events"

    asyncio.run(check())


def test_exhausted_budget_does_not_ask_planner_for_more_steps():
    async def check():
        usage = FakeSessionEventRepository(tool_calls=35)
        flow = make_flow(usage, AgentConfig(max_tool_calls_per_run=36))
        flow.planner.update_plan = AsyncMock(side_effect=AssertionError("不能重新规划"))

        async def execute(_plan, step, _message):
            usage.tool_calls += 1
            step.status, step.success, step.result = ExecutionStatus.COMPLETED, False, "已取得部分资料"
            yield MessageEvent(message=step.result)

        flow.react.execute_step = execute
        with pytest.raises(RunBudgetExceeded):
            async for _ in flow.invoke(Message(message="执行")):
                pass
        assert usage.tool_calls == 36
        assert flow.plan.steps[0].result == "已取得部分资料"
        flow.planner.update_plan.assert_not_called()
    asyncio.run(check())


def test_resume_failed_execution_runs_same_step_without_replanning():
    async def check():
        usage = FakeSessionEventRepository(tool_calls=12, started_steps=1)
        flow = make_flow(usage, AgentConfig(max_tool_calls_per_run=36, max_plan_steps=1))
        flow.plan = Plan(steps=[Step(id="original")])
        checkpoint = RunCheckpoint(
            flow_status=RunCheckpointPhase.EXECUTING,
            current_step=Step(id="original", status=ExecutionStatus.FAILED, error="503", result="已采集资料"),
            stop_reason=RunStopReason.EXECUTION_ERROR,
        )
        uow = FakeUnitOfWork(usage)
        uow.get_by_id = AsyncMock(return_value=SessionRun(
            id="run-1", session_id="session-1", checkpoint=checkpoint.model_dump(mode="json"),
        ))
        flow._uow_factory = lambda: uow
        flow._uow = uow
        flow._resume_from_status = SessionRunStatus.FAILED
        flow._restore_plan = AsyncMock(return_value=True)
        flow.react.repair_interrupted_tool_call = AsyncMock()
        flow.planner.create_plan = AsyncMock(side_effect=AssertionError("不创建新计划"))
        flow.planner.update_plan = AsyncMock(side_effect=AssertionError("不重新规划"))

        async def execute(_plan, step, _message):
            assert step.id == "original" and step.error is None
            assert step.result == "已采集资料"
            step.status, step.success = ExecutionStatus.COMPLETED, True
            yield MessageEvent(message="恢复成功")

        async def summarize():
            yield MessageEvent(message="总结")

        flow.react.execute_step, flow.react.summarize = execute, summarize
        events = [event async for event in flow.invoke(Message(message="继续"))]
        assert isinstance(events[-1], DoneEvent)
        assert len(flow.plan.steps) == 1 and flow.plan.steps[0].id == "original"
        assert usage.tool_calls == 12 and usage.started_steps == 1
        flow.planner.create_plan.assert_not_called()
        flow.planner.update_plan.assert_not_called()
    asyncio.run(check())
