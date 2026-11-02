import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.models.app_config import AgentConfig
from app.domain.models.event import (
    MessageEvent,
    PlanEvent,
    PlanEventStatus,
    StepEvent,
    StepEventStatus,
)
from app.domain.models.message import Message
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.models.session_run import SessionRun, SessionRunStatus
from app.domain.services.flows.planner_react import PlannerReActFlow


class FakeLookupRepository:
    """为Flow恢复测试提供最小的Session与Run查询能力。"""

    async def get_by_id(self, item_id: str) -> object | None:
        if item_id == "session-1":
            return object()
        if item_id == "run-1":
            return SessionRun(
                id="run-1",
                session_id="session-1",
                status=SessionRunStatus.WAITING,
            )
        return None

    async def count_tool_calls_by_run(self, run_id: str) -> int:
        assert run_id == "run-1"
        return 0

    async def count_started_steps_by_run(self, run_id: str) -> int:
        assert run_id == "run-1"
        return 0


class FakeUnitOfWork:
    def __init__(self) -> None:
        repository = FakeLookupRepository()
        self.session = repository
        self.session_run = repository
        self.session_event = repository

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


def make_recovery_flow(status: SessionRunStatus) -> PlannerReActFlow:
    """绕过构造器，只组装恢复分支执行前所需的最小Flow。"""
    flow = object.__new__(PlannerReActFlow)
    flow._uow = FakeUnitOfWork()
    flow._uow_factory = FakeUnitOfWork
    flow._session_id = "session-1"
    flow._run_id = "run-1"
    flow._resume_from_status = status
    flow.planner = MagicMock()
    flow.planner.resume_waiting_tool_call = AsyncMock()
    flow.planner.repair_interrupted_tool_call = AsyncMock()
    flow.react = MagicMock()
    flow.react.resume_waiting_tool_call = AsyncMock(return_value=True)
    flow.react.repair_interrupted_tool_call = AsyncMock(return_value=True)

    # 恢复职责验证到这里已经完成。用明确异常停止生成器，避免测试继续进入
    # checkpoint之后与本测试无关的执行额度、Plan执行等逻辑。
    flow._restore_checkpoint = AsyncMock(
        side_effect=RuntimeError("checkpoint-test-stop")
    )
    return flow


class FakeExecutionPlanner:
    def __init__(self) -> None:
        self.update_calls = 0

    async def create_plan(self, _message: Message):
        yield PlanEvent(
            status=PlanEventStatus.CREATED,
            plan=Plan(
                id="plan-1",
                steps=[
                    Step(id="step-1", description="第一步"),
                    Step(id="step-2", description="第二步"),
                ],
            ),
        )

    async def update_plan(self, plan: Plan, _step: Step):
        self.update_calls += 1
        yield PlanEvent(
            status=PlanEventStatus.UPDATED,
            plan=plan,
        )


class FakeExecutionReact:
    def __init__(self, step_results: list[bool]) -> None:
        self._step_results = iter(step_results)
        self.executed_step_ids: list[str] = []
        self.summarize_calls = 0

    async def execute_step(
        self,
        _plan: Plan,
        step: Step,
        _message: Message,
    ):
        self.executed_step_ids.append(step.id)
        step.status = ExecutionStatus.COMPLETED
        step.success = next(self._step_results)
        yield StepEvent(
            step=step,
            status=StepEventStatus.COMPLETED,
        )

    async def summarize(self):
        self.summarize_calls += 1
        yield MessageEvent(message="总结完成")


def make_execution_flow(
    step_results: list[bool],
) -> tuple[PlannerReActFlow, FakeExecutionPlanner, FakeExecutionReact]:
    """组装一个不调用外部LLM和工具的完整Flow，用于验证状态流转。"""
    flow = object.__new__(PlannerReActFlow)
    flow._uow = FakeUnitOfWork()
    flow._uow_factory = FakeUnitOfWork
    flow._session_id = "session-1"
    flow._run_id = "run-1"
    flow._resume_from_status = SessionRunStatus.PENDING
    flow._agent_config = AgentConfig()
    flow.plan = None

    planner = FakeExecutionPlanner()
    react = FakeExecutionReact(step_results)
    flow.planner = planner
    flow.react = react
    return flow, planner, react


def test_planner_has_no_tools_and_react_owns_execution_tools() -> None:
    """Planner不持有工具，全部执行工具只注册到ReAct。"""
    with (
        patch(
            "app.domain.services.flows.planner_react.PlannerAgent"
        ) as planner_class,
        patch(
            "app.domain.services.flows.planner_react.ReActAgent"
        ) as react_class,
    ):
        PlannerReActFlow(
            uow_factory=FakeUnitOfWork,
            llm=MagicMock(),
            agent_config=AgentConfig(),
            session_id="session-1",
            run_id="run-1",
            resume_from_status=SessionRunStatus.PENDING,
            json_parser=MagicMock(),
            browser=MagicMock(),
            sandbox=MagicMock(),
            search_engine=MagicMock(),
            mcp_tool=MagicMock(),
            a2a_tool=MagicMock(),
        )

        assert planner_class.call_args.kwargs["tools"] == []
        assert len(react_class.call_args.kwargs["tools"]) == 7


def test_waiting_resume_only_uses_react_and_does_not_repair_chain() -> None:
    """WAITING回答由ReAct补入；成功后不能再写入合成中断结果。"""

    async def check() -> None:
        flow = make_recovery_flow(SessionRunStatus.WAITING)

        with pytest.raises(RuntimeError, match="checkpoint-test-stop"):
            async for _ in flow.invoke(Message(message="这是我的回答")):
                pass

        flow.react.resume_waiting_tool_call.assert_awaited_once()
        flow.react.repair_interrupted_tool_call.assert_not_awaited()
        flow.planner.resume_waiting_tool_call.assert_not_awaited()
        flow.planner.repair_interrupted_tool_call.assert_not_awaited()

    asyncio.run(check())


def test_waiting_resume_fails_when_react_has_no_pending_user_call() -> None:
    """状态为WAITING但ReAct找不到询问用户调用时，应明确暴露数据不一致。"""

    async def check() -> None:
        flow = make_recovery_flow(SessionRunStatus.WAITING)
        flow.react.resume_waiting_tool_call.return_value = False

        with pytest.raises(
            RuntimeError,
            match="没有找到等待用户回答的工具调用",
        ):
            async for _ in flow.invoke(Message(message="这是我的回答")):
                pass

        flow.react.resume_waiting_tool_call.assert_awaited_once()
        flow._restore_checkpoint.assert_not_awaited()
        flow.planner.resume_waiting_tool_call.assert_not_awaited()
        flow.planner.repair_interrupted_tool_call.assert_not_awaited()

    asyncio.run(check())


def test_interrupted_resume_only_repairs_react_tool_chain() -> None:
    """普通工具中断只可能来自ReAct，因此恢复时不扫描Planner。"""

    async def check() -> None:
        flow = make_recovery_flow(SessionRunStatus.INTERRUPTED)

        with pytest.raises(RuntimeError, match="checkpoint-test-stop"):
            async for _ in flow.invoke(Message(message="继续")):
                pass

        flow.react.repair_interrupted_tool_call.assert_awaited_once_with()
        flow.planner.repair_interrupted_tool_call.assert_not_awaited()
        flow.planner.resume_waiting_tool_call.assert_not_awaited()

    asyncio.run(check())


def test_successful_steps_skip_planner_update_and_continue_execution() -> None:
    """成功步骤直接推进到下一步，最后一步成功后直接总结。"""

    async def check() -> None:
        flow, planner, react = make_execution_flow([True, True])
        events = [
            event
            async for event in flow.invoke(Message(message="执行任务"))
        ]

        assert react.executed_step_ids == ["step-1", "step-2"]
        assert react.summarize_calls == 1
        assert planner.update_calls == 0
        plan_snapshots = [
            event
            for event in events
            if (
                isinstance(event, PlanEvent)
                and event.status == PlanEventStatus.UPDATED
            )
        ]
        assert len(plan_snapshots) == 2
        assert all(
            snapshot.plan.steps[0].success
            for snapshot in plan_snapshots
        )
        assert any(
            isinstance(event, PlanEvent)
            and event.status == PlanEventStatus.COMPLETED
            for event in events
        )

    asyncio.run(check())


def test_unsuccessful_step_still_updates_remaining_plan() -> None:
    """步骤未成功取得结果时，仍允许Planner调整后续未完成步骤。"""

    async def check() -> None:
        flow, planner, react = make_execution_flow([False, True])

        async for _ in flow.invoke(Message(message="执行任务")):
            pass

        assert react.executed_step_ids == ["step-1", "step-2"]
        assert planner.update_calls == 1
        assert react.summarize_calls == 1

    asyncio.run(check())
