#!/usr/bin/env python
# -*- coding: utf-8 -*-
import logging
from typing import AsyncGenerator, Callable, Optional

from app.domain.external.browser import Browser
from app.domain.exceptions import RunBudgetExceeded
from app.domain.external.json_parser import JSONParser
from app.domain.external.llm import LLM
from app.domain.external.sandbox import Sandbox
from app.domain.external.search import SearchEngine
from app.domain.models.app_config import AgentConfig
from app.domain.models.event import (
    BaseEvent,
    DoneEvent,
    MessageEvent,
    PlanEvent,
    PlanEventStatus,
    TitleEvent,
)
from app.domain.models.message import Message
from app.domain.models.plan import Plan, Step, ExecutionStatus
from app.domain.models.run_checkpoint import (
    RunCheckpoint,
    RunCheckpointPhase,
    RunStopReason,
)
from app.domain.models.session_run import SessionRun
from app.domain.services.agents.planner import PlannerAgent
from app.domain.services.agents.react import ReActAgent
from app.domain.services.tools.a2a import A2ATool
from app.domain.services.tools.browser import BrowserTool
from app.domain.services.tools.file import FileTool
from app.domain.services.tools.mcp import MCPTool
from app.domain.services.tools.message import MessageTool
from app.domain.services.tools.search import SearchTool
from app.domain.services.tools.shell import ShellTool
from .base import BaseFlow, FlowStatus
from ...models.session_run import SessionRunStatus
from ...repositories.uow import IUnitOfWork

logger = logging.getLogger(__name__)


class PlannerReActFlow(BaseFlow):
    """规划与执行流"""

    def __init__(
            self,
            uow_factory: Callable[[], IUnitOfWork],  # uow模块
            llm: LLM,  # 大语言模型
            agent_config: AgentConfig,  # 智能体配置
            session_id: str,  # 会话id
            run_id: str,
            resume_from_status: SessionRunStatus,
            json_parser: JSONParser,  # JSON解析器
            browser: Browser,  # 浏览器
            sandbox: Sandbox,  # 沙箱
            search_engine: SearchEngine,  # 搜索引擎
            mcp_tool: MCPTool,  # mcp工具
            a2a_tool: A2ATool,  # a2a远程agent
    ) -> None:
        """构造函数，完成规划与执行流的初始化"""
        # 1.流初始化数据配置
        self._uow_factory = uow_factory
        self._uow = uow_factory()
        self._session_id = session_id
        self._agent_config = agent_config
        self.status = FlowStatus.IDLE
        self._run_id = run_id
        self._resume_from_status = resume_from_status
        self.plan: Optional[Plan] = None

        # 2.初始化执行Agent的工具列表。
        # Planner只负责创建、更新Plan，不执行工具，也不会产生WAITING状态；
        # 文件、Shell、浏览器、搜索、询问用户等工具统一由ReAct负责调用。
        react_tools = [
            FileTool(sandbox=sandbox),
            ShellTool(sandbox=sandbox),
            BrowserTool(browser=browser),
            SearchTool(search_engine=search_engine),
            MessageTool(),
            mcp_tool,
            a2a_tool,
        ]

        # 3.创建规划Agent
        self.planner = PlannerAgent(
            uow_factory=uow_factory,
            session_id=session_id,
            agent_config=agent_config,
            llm=llm,
            json_parser=json_parser,
            # Planner只生成结构化Plan。显式传入空列表，避免规划阶段误调用
            # message_ask_user或其他执行工具，也让恢复逻辑的职责边界保持清晰。
            tools=[],
            run_id=run_id
        )
        logger.debug(f"创建规划Agent成功, 会话id: {self._session_id}")

        # 4.创建执行Agent
        self.react = ReActAgent(
            uow_factory=uow_factory,
            session_id=session_id,
            agent_config=agent_config,
            llm=llm,
            json_parser=json_parser,
            tools=react_tools,
            run_id=run_id
        )
        logger.debug(f"创建执行Agent成功, 会话id: {self._session_id}")


    # 恢复plan的方法
    async def _restore_plan(
            self,
            plan_event_id: str | None = None,
    ) -> bool:
        """根据checkpoint指针或当前Run的最新PlanEvent恢复Plan。"""
        uow = self._uow_factory()
        async with uow:
            if plan_event_id is not None:
                session_plan = await uow.session_event.get_by_event_id(
                    plan_event_id
                )
            else:
                session_plan = await uow.session_event.get_last_plan(
                    session_id=self._session_id,
                    run_id=self._run_id,
                )

        if session_plan is None:
            logger.warning(
                "运行[%s]没有找到可以恢复的Plan",
                self._run_id,
            )
            return False

        if (
            session_plan.session_id != self._session_id
            or session_plan.run_id != self._run_id
        ):
            raise RuntimeError(
                f"PlanEvent[{session_plan.event_id}]不属于当前运行"
                f"[{self._run_id}]"
            )

        event = session_plan.to_event()

        if not isinstance(event, PlanEvent):
            raise TypeError(
                "最新计划记录无法恢复为PlanEvent，"
                f"实际类型为[{type(event).__name__}]"
            )

        self.plan = event.plan

        logger.info(
            "运行[%s]恢复Plan[%s]成功",
            self._run_id,
            self.plan.id,
        )

        return True

    async def _restore_checkpoint(
            self,
            run: SessionRun,
    ) -> Step | None:
        """恢复流程阶段，并用当前Step快照覆盖Plan中的旧状态。"""
        if not run.checkpoint:
            restored = await self._restore_plan()
            self.status = (
                FlowStatus.EXECUTING
                if restored
                else FlowStatus.PLANNING
            )
            return None

        checkpoint = RunCheckpoint.model_validate(run.checkpoint)
        restored = await self._restore_plan(checkpoint.plan_event_id)

        if not restored:
            self.status = FlowStatus.PLANNING
            return None

        current_step = checkpoint.current_step
        if current_step is not None:
            # 只重新激活EXECUTING阶段尚未完成的当前步骤。业务失败的
            # UPDATING快照必须保留，已成功完成的Step也不能重复执行。
            if checkpoint.flow_status == RunCheckpointPhase.EXECUTING and (
                current_step.status in {ExecutionStatus.RUNNING, ExecutionStatus.FAILED}
                or (checkpoint.stop_reason == RunStopReason.BUDGET_EXHAUSTED
                    and not current_step.success)
            ):
                current_step.status = ExecutionStatus.RUNNING
                current_step.error = None
                current_step.success = False
            for index, plan_step in enumerate(self.plan.steps):
                if plan_step.id == current_step.id:
                    current_step = current_step.model_copy(deep=True)
                    self.plan.steps[index] = current_step
                    break
            else:
                raise RuntimeError(
                    f"checkpoint中的步骤[{current_step.id}]"
                    "不在恢复的Plan中"
                )

        phase_mapping = {
            RunCheckpointPhase.PLANNING: FlowStatus.PLANNING,
            RunCheckpointPhase.EXECUTING: FlowStatus.EXECUTING,
            RunCheckpointPhase.UPDATING: FlowStatus.UPDATING,
            RunCheckpointPhase.SUMMARIZING: FlowStatus.SUMMARIZING,
            # 用户回答已经补入工具消息，恢复后重新进入当前Step。
            RunCheckpointPhase.WAITING: FlowStatus.EXECUTING,
            RunCheckpointPhase.COMPLETED: FlowStatus.COMPLETED,
        }
        self.status = phase_mapping[checkpoint.flow_status]

        if self.status == FlowStatus.UPDATING and current_step is None:
            raise RuntimeError("UPDATING checkpoint缺少current_step")

        logger.info(
            "运行[%s]从checkpoint恢复：phase=%s, step=%s, plan_event=%s",
            self._run_id,
            checkpoint.flow_status.value,
            checkpoint.current_step_id,
            checkpoint.plan_event_id,
        )
        return current_step

    async def invoke(self, message: Message) -> AsyncGenerator[BaseEvent, None]:
        """传递消息，运行流，在六中调用planner&react智能体组合完成任务并返回对应事件"""

        # 调用会话仓库查询会话是否存在
        async with self._uow:
            session = await self._uow.session.get_by_id(self._session_id)
            run = await self._uow.session_run.get_by_id(self._run_id)
        if not session:
            raise ValueError(f"会话[{self._session_id}]不存在, 请核实后尝试")

        if run is None:
            raise ValueError(f"运行[{self._run_id}]不存在")

        # 本次Task创建前的状态。
        resume_status = self._resume_from_status

        logger.info(
            "运行[%s]开始执行，恢复前状态=%s",
            self._run_id,
            resume_status.value,
        )

        # 恢复UPDATING阶段时，后续需要把当前Step再次传给Planner。
        step = None

        # 等待状态只可能由ReAct调用message_ask_user产生。
        # Planner不持有工具，因此既不需要、也不应该扫描Planner消息历史。
        if self._resume_from_status == SessionRunStatus.WAITING:
            react_resumed = await self.react.resume_waiting_tool_call(message)

            if not react_resumed:
                raise RuntimeError(
                    "任务处于WAITING状态，"
                    "但没有找到等待用户回答的工具调用"
                )

            # resume_waiting_tool_call()已经把真实用户回答补成对应的tool消息，
            # 此时消息链是完整合法的，不应再追加“工具调用被中断”的合成结果。

            step = await self._restore_checkpoint(run)

        elif self._resume_from_status in {
            SessionRunStatus.INTERRUPTED,
            SessionRunStatus.FAILED,
        }:
            # 只有ReAct会调用工具，因此只修复ReAct在当前Run末尾留下的
            # 悬空普通工具调用，不扫描Planner，也不改写历史Run。
            await self.react.repair_interrupted_tool_call()

            step = await self._restore_checkpoint(run)

        # 新创建的Run正常进入规划
        else:
            self.status = FlowStatus.PLANNING

        # 防止同一个Flow再次invoke时重复执行恢复操作。
        self._resume_from_status = SessionRunStatus.RUNNING

        # 对恢复任务先读取已经持久化的工具数量；已耗尽额度时
        # 不再继续旧计划的步骤或重新规划。
        budget_uow = self._uow_factory()
        async with budget_uow:
            initial_tool_calls = await budget_uow.session_event.count_tool_calls_by_run(
                self._run_id
            )
            executed_steps = await budget_uow.session_event.count_started_steps_by_run(
                self._run_id
            )
        # checkpoint中正在执行的Step已经计入持久化用量。
        # 恢复它不新增步骤；后续真正的新Step仍受原有上限约束。
        resumed_step_id = step.id if step is not None and step.status == ExecutionStatus.RUNNING else None
        if initial_tool_calls >= self._agent_config.max_tool_calls_per_run:
            raise RunBudgetExceeded(
                "本轮任务工具调用已达到上限"
                f"({self._agent_config.max_tool_calls_per_run})，"
                "请调整额度后继续，或开始新任务；恢复不会清零已有用量"
            )

        logger.info(f"Planner&ReAct流接收消息: {message.message[:50]}...")

        # 7.创建死循环执行任务，根据流的不同状态执行不同的操作
        """
            步骤成功 + 还有后续步骤 → 直接执行后续步骤
            步骤成功 + 没有后续步骤 → 直接总结
            步骤失败 → 才调用Planner修改后续计划
        """
        while True:
            if self.status == FlowStatus.PLANNING:
                # 流状态为规划中，则调用规划Agent
                logger.info(f"Planner&ReAct流开始创建计划/Plan")
                async for event in self.planner.create_plan(message):
                    # 10.判断规划Agent是否返回规划事件
                    if isinstance(event, PlanEvent) and event.status == PlanEventStatus.CREATED:
                        # 11 .创建计划成功时需要更新计划
                        self.plan = event.plan  # 保存计划
                        logger.info(f"Planner&ReAct流成功创建计划, 共计: {len(event.plan.steps)} 步")

                        # 12.在计划中同步生成了会话标题+初始AI消息
                        yield TitleEvent(title=event.plan.title)
                        yield MessageEvent(role="assistant", message=event.plan.message)

                    # 13.将生成的事件直接输出(一般来说是PlanEvent)
                    yield event

                # 14.计划创建完成，更新流状态为执行中
                logger.info(f"Planner&ReAct流状态从{FlowStatus.PLANNING}变成{FlowStatus.EXECUTING}")
                self.status = FlowStatus.EXECUTING

                # 判断计划是否生成，步骤是否正常
                if not self.plan or len(self.plan.steps) == 0:
                    logger.info(f"Planner&ReAct流创建计划失败或无子步骤")
                    self.status = FlowStatus.COMPLETED
            elif self.status == FlowStatus.EXECUTING:
                # 16.流的状态为执行中，先将计划状态调整为运行中，同时调用执行Agent完成每个子步骤
                self.plan.status = ExecutionStatus.RUNNING

                # 17.获取当前计划的下一个需要执行的子步骤
                step = self.plan.get_next_step()

                # 18.如果不存在下一个需要执行的步骤，则更新流状态
                if not step:
                    logger.info(f"Planner&ReAct流状态从{FlowStatus.EXECUTING}变成{FlowStatus.SUMMARIZING}")
                    self.status = FlowStatus.SUMMARIZING
                    continue

                # 规划器可以反复改写未完成步骤，不能只限制数组长度。
                if step.id != resumed_step_id and executed_steps >= self._agent_config.max_plan_steps:
                    raise RuntimeError(
                        f"本次任务执行步骤超过上限({self._agent_config.max_plan_steps})，请缩小任务范围"
                    )
                if step.id != resumed_step_id:
                    executed_steps += 1
                resumed_step_id = None

                # 19.调用执行Agent执行对应的步骤
                logger.info(f"Planner&ReAct流开始执行步骤 {step.id}: {step.description[:50]}...")
                async for event in self.react.execute_step(self.plan, step, message):
                    yield event

                # 21.步骤执行成功时，不需要再让Planner调用一次LLM改写计划。
                # ReAct直接修改的是Plan中的Step对象，因此这里可以根据更新后的
                # Plan继续执行下一个未完成步骤；全部完成后直接进入总结阶段。
                # 只有步骤没有成功取得结果时，才进入UPDATING，让Planner根据
                # 失败结果调整后续未完成步骤。
                if step.success:

                    # 这里的PlanEvent只是把当前Plan完整快照持久化，供断点恢复
                    # 使用，不会调用PlannerAgent或产生额外的LLM请求。如果只在
                    # checkpoint里保存最后一个current_step，多步骤任务重启后会
                    # 丢失更早步骤的完成状态。
                    yield PlanEvent(
                        status=PlanEventStatus.UPDATED,
                        plan=self.plan,
                    )

                    next_step = self.plan.get_next_step()
                    if next_step is None:
                        logger.info(
                            "步骤[%s]执行成功且没有剩余步骤，直接进入总结阶段",
                            step.id,
                        )
                        self.status = FlowStatus.SUMMARIZING
                    else:
                        logger.info(
                            "步骤[%s]执行成功，继续执行下一步骤[%s]",
                            step.id,
                            next_step.id,
                        )
                        self.status = FlowStatus.EXECUTING
                else:
                    # 无工具收尾可能返回success=false。这表示额度不足，
                    # 不应继续让Planner生成无法执行的检索/补救步骤。
                    async with self._uow_factory() as budget_uow:
                        used = await budget_uow.session_event.count_tool_calls_by_run(self._run_id)
                    if used >= self._agent_config.max_tool_calls_per_run:
                        raise RunBudgetExceeded(
                            f"本轮任务工具调用已达到上限({self._agent_config.max_tool_calls_per_run})，"
                            "已保留当前步骤结果；请调整额度后继续，或开始新任务"
                        )
                    logger.info(
                        "步骤[%s]未成功完成，进入计划更新阶段",
                        step.id,
                    )
                    self.status = FlowStatus.UPDATING
            elif self.status == FlowStatus.UPDATING:
                # 22.流状态为更新表示需要更新计划
                logger.info(f"Planner&ReAct流开始更新计划")
                async for event in self.planner.update_plan(self.plan, step):
                    yield event

                # 23.计划更新完成，需要执行相应的子步骤
                logger.info(f"Planner&ReAct流状态从{FlowStatus.UPDATING}变成{FlowStatus.EXECUTING}")
                self.status = FlowStatus.EXECUTING
            elif self.status == FlowStatus.SUMMARIZING:
                # 24.流状态为总结中，则意味着所有子步骤都执行完成
                logger.info(f"Planner&ReAct流开始总结")
                async for event in self.react.summarize():
                    yield event

                # 25.总结完毕，意味着流即将结束
                logger.info(f"Planner&ReAct流状态从{FlowStatus.SUMMARIZING}变成{FlowStatus.COMPLETED}")
                self.status = FlowStatus.COMPLETED
            elif self.status == FlowStatus.COMPLETED:
                # 26.计划状态已完成则更新plan状态，并发送计划事件通知API已完成
                self.plan.status = ExecutionStatus.COMPLETED
                self.status = FlowStatus.IDLE
                yield PlanEvent(status=PlanEventStatus.COMPLETED, plan=self.plan)
                break
        # 28.任务已经结束则返回结束事件
        yield DoneEvent()
        logger.info(f"Planner&ReAct流处理任务消息已完毕")

    @property
    def done(self) -> bool:
        """只读属性，返回流是否运行结束"""
        return self.status == FlowStatus.IDLE
