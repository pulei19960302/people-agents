import asyncio

import pytest

from app.domain.models.event import MessageEvent, PlanEvent, PlanEventStatus
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.services.agents.planner import PlannerAgent


class FakeJSONParser:
    def __init__(self, parsed_result: dict) -> None:
        self._parsed_result = parsed_result

    async def invoke(self, _text: str) -> dict:
        return self._parsed_result


def make_planner(updated_steps: list[dict[str, str]]) -> PlannerAgent:
    """创建不访问LLM的Planner，只返回指定的更新步骤。"""
    planner = object.__new__(PlannerAgent)
    planner._json_parser = FakeJSONParser({"steps": updated_steps})

    async def fake_invoke(_query: str):
        yield MessageEvent(message="planner-update")

    planner.invoke = fake_invoke
    return planner


def test_update_plan_can_append_recovery_step_after_failed_last_step() -> None:
    """失败的是最后一步时，Planner生成的补救步骤不能被丢弃。"""

    async def check() -> None:
        failed_step = Step(
            id="step-1",
            description="从首选来源取数",
            status=ExecutionStatus.COMPLETED,
            success=False,
        )
        plan = Plan(id="plan-1", steps=[failed_step])
        planner = make_planner([
            {
                "id": "step-2",
                "description": "改用备用来源取数",
            },
        ])

        events = [
            event
            async for event in planner.update_plan(plan, failed_step)
        ]

        assert [step.id for step in plan.steps] == ["step-1", "step-2"]
        assert plan.steps[0] is failed_step
        assert len(events) == 1
        assert isinstance(events[0], PlanEvent)
        assert events[0].status == PlanEventStatus.UPDATED

    asyncio.run(check())


def test_update_plan_replaces_only_steps_after_current_step() -> None:
    """保留执行历史，并用Planner输出完整替换尚未执行的后续部分。"""

    async def check() -> None:
        completed_step = Step(
            id="step-1",
            description="已完成步骤",
            status=ExecutionStatus.COMPLETED,
            success=True,
        )
        failed_step = Step(
            id="step-2",
            description="失败步骤",
            status=ExecutionStatus.COMPLETED,
            success=False,
        )
        old_pending_step = Step(
            id="step-3",
            description="不再适用的旧步骤",
        )
        plan = Plan(
            id="plan-1",
            steps=[completed_step, failed_step, old_pending_step],
        )
        planner = make_planner([
            {
                "id": "step-4",
                "description": "调整后的新步骤",
            },
        ])

        async for _ in planner.update_plan(plan, failed_step):
            pass

        assert [step.id for step in plan.steps] == [
            "step-1",
            "step-2",
            "step-4",
        ]

    asyncio.run(check())


def test_update_plan_rejects_duplicate_step_ids() -> None:
    """拒绝Planner用新步骤覆盖已经执行过的历史步骤标识。"""

    async def check() -> None:
        failed_step = Step(
            id="step-1",
            description="失败步骤",
            status=ExecutionStatus.COMPLETED,
            success=False,
        )
        plan = Plan(id="plan-1", steps=[failed_step])
        planner = make_planner([
            {
                "id": "step-1",
                "description": "错误地复用了历史ID",
            },
        ])

        with pytest.raises(RuntimeError, match="重复的Step ID"):
            async for _ in planner.update_plan(plan, failed_step):
                pass

    asyncio.run(check())
