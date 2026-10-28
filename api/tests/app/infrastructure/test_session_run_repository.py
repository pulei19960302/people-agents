import asyncio
from typing import Any

from sqlalchemy.dialects import postgresql

from app.domain.models.plan import Step
from app.domain.models.run_checkpoint import RunCheckpoint, RunCheckpointPhase
from app.infrastructure.repositories.db_session_run_repository import (
    DBSessionRunRepository,
)


class FakeResult:
    rowcount = 1


class FakeAsyncSession:
    def __init__(self) -> None:
        self.statement: Any = None

    async def execute(self, statement: Any) -> FakeResult:
        self.statement = statement
        return FakeResult()


def test_update_checkpoint_writes_step_id_and_json_together() -> None:
    async def check() -> None:
        db_session = FakeAsyncSession()
        repository = DBSessionRunRepository(db_session)  # type: ignore[arg-type]
        checkpoint = RunCheckpoint(
            flow_status=RunCheckpointPhase.EXECUTING,
            plan_event_id="plan-event-1",
            current_step=Step(
                id="step-1",
                description="核查资料",
            ),
            last_event_id="step-event-1",
        )

        await repository.update_checkpoint("run-1", checkpoint)

        assert db_session.statement is not None
        compiled = db_session.statement.compile(
            dialect=postgresql.dialect(),
        )
        assert compiled.params["id_1"] == "run-1"
        assert compiled.params["current_step_id"] == "step-1"
        assert compiled.params["checkpoint"] == checkpoint.model_dump(
            mode="json"
        )

    asyncio.run(check())
