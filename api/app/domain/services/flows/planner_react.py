#!/usr/bin/env python
# -*- coding: utf-8 -*-
import logging
from typing import AsyncGenerator, Optional, Callable, cast

from app.domain.external.browser import Browser
from app.domain.external.json_parser import JSONParser
from app.domain.external.llm import LLM
from app.domain.external.sandbox import Sandbox
from app.domain.external.search import SearchEngine
from app.domain.models.app_config import AgentConfig
from app.domain.models.event import BaseEvent, PlanEvent, PlanEventStatus, TitleEvent, MessageEvent
from app.domain.models.event import DoneEvent
from app.domain.models.message import Message
from app.domain.models.plan import Plan, ExecutionStatus
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
from ...models.session_event import SessionEvent
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

        # 2.初始化Agent预设工具列表
        tools = [
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
            tools=tools,
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
            tools=tools,
            run_id=run_id
        )
        logger.debug(f"创建执行Agent成功, 会话id: {self._session_id}")


    async def _restore_plan(self) -> bool:
        """根据当前run_id恢复最新Plan。"""
        uow = self._uow_factory()
        async with uow:
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

        if self._resume_from_status == SessionRunStatus.WAITING:
            planner_resumed = await self.planner.resume_waiting_tool_call(message)
            react_resumed = await self.react.resume_waiting_tool_call(message)

            if not planner_resumed and not react_resumed:
                raise RuntimeError(
                    "任务处于WAITING状态，"
                    "但没有找到等待用户回答的工具调用"
                )

                # 修复同一份历史中其他可能存在的悬空工具调用。
            await self.planner.repair_incomplete_tool_chain()
            await self.react.repair_incomplete_tool_chain()



            restored = await self._restore_plan()
            self.status = (
                FlowStatus.EXECUTING
                if restored
                else FlowStatus.PLANNING
            )

        elif self._resume_from_status in {
            SessionRunStatus.INTERRUPTED,
            SessionRunStatus.FAILED,
        }:
            await self.planner.repair_incomplete_tool_chain()
            await self.react.repair_incomplete_tool_chain()

            restored = await self._restore_plan()
            self.status = (
                FlowStatus.EXECUTING
                if restored
                else FlowStatus.PLANNING
            )

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
        if initial_tool_calls >= self._agent_config.max_tool_calls_per_run:
            raise RuntimeError(
                "本轮任务工具调用已达到上限"
                f"({self._agent_config.max_tool_calls_per_run})，"
                "请开始新任务或缩小任务范围"
            )

        logger.info(f"Planner&ReAct流接收消息: {message.message[:50]}...")

        # 6.定义当前正在执行的子步骤
        step = None

        # 7.创建死循环执行任务，根据流的不同状态执行不同的操作
        while True:
            # 8.如果流的状态为空闲，则只需要将状态修改为规划中
            if self.status == FlowStatus.IDLE:
                logger.info(f"Planner&ReAct流状态从{FlowStatus.IDLE}变成{FlowStatus.PLANNING}")
                self.status = FlowStatus.PLANNING
            elif self.status == FlowStatus.PLANNING:
                # 9.流状态为规划中，则调用规划Agent
                logger.info(f"Planner&ReAct流开始创建计划/Plan")
                async for event in self.planner.create_plan(message):
                    # 10.判断规划Agent是否返回规划事件
                    if isinstance(event, PlanEvent) and event.status == PlanEventStatus.CREATED:
                        # 11 .创建计划成功时需要更新计划
                        self.plan = event.plan
                        logger.info(f"Planner&ReAct流成功创建计划, 共计: {len(event.plan.steps)} 步")

                        # 12.在计划中同步生成了会话标题+初始AI消息
                        yield TitleEvent(title=event.plan.title)
                        yield MessageEvent(role="assistant", message=event.plan.message)

                    # 13.将生成的事件直接输出(一般来说是PlanEvent)
                    yield event

                # 14.计划创建完成，更新流状态为执行中
                logger.info(f"Planner&ReAct流状态从{FlowStatus.PLANNING}变成{FlowStatus.EXECUTING}")
                self.status = FlowStatus.EXECUTING

                # 15.判断计划是否生成，步骤是否正常
                if not self.plan or len(self.plan.steps) == 0:
                    logger.info(f"Planner&ReAct流创建计划失败或无子步骤")
                    self.status = FlowStatus.COMPLETED
            elif self.status == FlowStatus.EXECUTING:
                # 16.流的状态为执行中，先将计划状态调整为运行中，同时调用执行Agent完成每个子步骤
                self.plan.status = ExecutionStatus.RUNNING

                # 17.获取当前计划的下一个需要执行的子步骤
                step = self.plan.get_next_step()

                # 18.如果不存在下一个需要执行的自己花，则更新流状态并执行后续步骤
                if not step:
                    logger.info(f"Planner&ReAct流状态从{FlowStatus.EXECUTING}变成{FlowStatus.SUMMARIZING}")
                    self.status = FlowStatus.SUMMARIZING
                    continue

                # 规划器可以反复改写未完成步骤，不能只限制数组长度。
                if executed_steps >= self._agent_config.max_plan_steps:
                    raise RuntimeError(
                        f"本次任务执行步骤超过上限({self._agent_config.max_plan_steps})，请缩小任务范围"
                    )
                executed_steps += 1

                # 19.调用执行Agent执行对应的步骤
                logger.info(f"Planner&ReAct流开始执行步骤 {step.id}: {step.description[:50]}...")
                async for event in self.react.execute_step(self.plan, step, message):
                    yield event
                # 21.将状态更新为updating
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
