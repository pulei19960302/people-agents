#!/usr/bin/env python
# -*- coding: utf-8 -*-
import logging
from typing import Optional, AsyncGenerator

from app.domain.models.event import BaseEvent, MessageEvent, PlanEvent, PlanEventStatus
from app.domain.models.message import Message
from app.domain.models.plan import Plan, Step
from app.domain.services.prompts.planner import (
    PLANNER_SYSTEM_PROMPT,
    CREATE_PLAN_PROMPT,
    UPDATE_PLAN_PROMPT,
)
from app.domain.services.prompts.system import SYSTEM_PROMPT
from .base import BaseAgent

"""
多Agent系统/flow=PlannerAgent+ReActAgent

顺序:
1. PlannerAgent生成规划;
2. 循环取出规划中的子步骤，让ReActAgent执行，依次迭代;
3. ReActAgent执行完每一个子步骤之后，需要将子步骤结果+Plan传递给PlannerAgent让其更新计划/Plan；
4. 循环取出规划中的子步骤，让ReActAgent执行，依次迭代;
5. ...
6. 直到所有子任务/步骤都完成，这时候将子步骤的所有结果汇总进行总结(ReActAgent);

PlannerAgent:
- 功能: 将用户的需求拆解成多个子任务+根据已完成的子任务更新规划
- 提示词: 创建规划的prompt、更新规划的prompt

ReActAgent:
- 功能: 迭代执行完每一个子任务、汇总所有的子任务进行总结
- 提示词: 执行任务的prompt、汇总总结prompt
"""

logger = logging.getLogger(__name__)


class PlannerAgent(BaseAgent):
    """规划Agent，用于将用户的任务/需求拆解成多个子步骤"""
    name: str = "planner"
    _system_prompt: str = SYSTEM_PROMPT + PLANNER_SYSTEM_PROMPT
    _format: Optional[str] = "json_object"
    _tool_choice: Optional[str] = "none"

    async def create_plan(self, message: Message) -> AsyncGenerator[BaseEvent, None]:
        """根据用户传递的消息创建计划/规划，迭代返回对应的事件"""
        # 1.根据用户传递的消息生成创建plan的提示词
        query = CREATE_PLAN_PROMPT.format(
            message=message.message,
            attachments="\n".join(message.attachments),
        )

        # 2.调用invoke函数返回迭代事件
        async for event in self.invoke(query):
            # 3.规划智能体因为使用json_object，正常情况下会返回MessageEvent
            if isinstance(event, MessageEvent):
                # 4.记录日志并使用json解析器解析得到对应的数据
                logger.info(f"PlannerAgent生成消息: {event.message}")
                parsed_obj = await self._json_parser.invoke(event.message)

                # 5.将解析对象转换成Plan计划
                plan = Plan.model_validate(parsed_obj)

                # 6.返回PlanEvent表示规划创建成功
                yield PlanEvent(plan=plan, status=PlanEventStatus.CREATED)
            else:
                # 返回不是消息事件的事件
                yield event

    async def update_plan(self, plan: Plan, step: Step) -> AsyncGenerator[BaseEvent, None]:
        """根据传递的原始规划+子步骤更新事件"""
        # 1.使用plan+step创建更新Plan提示词
        query = UPDATE_PLAN_PROMPT.format(
            plan=plan.model_dump_json(),
            step=step.model_dump_json(),
        )

        # 2.调用invoke获取对应的事件
        async for event in self.invoke(query):
            # 3.判断规划Agent生成的事件是不是消息事件
            if isinstance(event, MessageEvent):
                # 4.记录日志并解析json
                logger.info(f"PlannerAgent生成消息: {event.message}")
                parsed_obj = await self._json_parser.invoke(event.message)

                # 5.将解析对象转换成Plan
                updated_plan = Plan.model_validate(parsed_obj)

                # 6.拷贝Planner返回的后续步骤，避免与解析结果共享可变对象。
                new_steps = [
                    Step.model_validate(new_step)
                    for new_step in updated_plan.steps
                ]

                # 7.update_plan()处理的是刚执行完但未成功的当前步骤。
                # 当前步骤本身属于已经发生的历史，必须保留；Planner返回的
                # steps只用于替换它后面的未执行部分。不能再依赖“第一个未完成
                # 步骤”，因为当前失败步骤可能正好是原计划的最后一步，此时旧
                # 实现会得到None并把Planner生成的补救步骤全部丢弃。
                current_step_index = next(
                    (
                        index
                        for index, plan_step in enumerate(plan.steps)
                        if plan_step.id == step.id
                    ),
                    None,
                )
                if current_step_index is None:
                    raise RuntimeError(
                        f"当前步骤[{step.id}]不在Plan[{plan.id}]中，无法更新计划"
                    )

                # 8.保留当前步骤以及它之前的完整执行历史，再替换后续步骤。
                retained_steps = plan.steps[:current_step_index + 1]
                updated_steps = [*retained_steps, *new_steps]

                # 9.Step ID是前端关联和checkpoint恢复的重要标识，不能允许
                # Planner返回与历史步骤重复的ID，否则恢复时无法确定对应步骤。
                step_ids = [plan_step.id for plan_step in updated_steps]
                if len(step_ids) != len(set(step_ids)):
                    raise RuntimeError(
                        "Planner更新计划时产生了重复的Step ID，无法安全保存计划"
                    )

                # 10.保存更新后的完整计划。
                plan.steps = updated_steps

                # 11.返回规划更新事件
                yield PlanEvent(plan=plan, status=PlanEventStatus.UPDATED)
            else:
                # 其他事件则直接返回
                yield event
