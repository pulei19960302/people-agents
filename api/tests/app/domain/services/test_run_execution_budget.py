import asyncio
from dataclasses import dataclass

import pytest
from sqlalchemy.dialects import postgresql

from app.domain.models.app_config import AgentConfig
from app.domain.models.event import PlanEvent, PlanEventStatus
from app.domain.models.message import Message
from app.domain.models.plan import Plan, Step
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
        assert SessionEventModel.__tablename__ == "session_events"

    asyncio.run(check())
