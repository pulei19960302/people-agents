#!/usr/bin/env python
# -*- coding: utf-8 -*-
import logging
from typing import AsyncGenerator

from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr

from app.domain.models.event import (
    StepEventStatus,
    StepEvent,
    ToolEvent,
    MessageEvent,
    ErrorEvent,
    ToolEventStatus,
    WaitEvent,
    BaseEvent
)
from app.domain.models.file import File
from app.domain.models.message import Message
from app.domain.models.plan import Plan, Step, ExecutionStatus
from app.domain.services.prompts.react import REACT_SYSTEM_PROMPT, EXECUTION_PROMPT, SUMMARIZE_PROMPT
from app.domain.services.prompts.system import SYSTEM_PROMPT
from .base import BaseAgent

logger = logging.getLogger(__name__)


class _StepResult(BaseModel):
    """模型只负责提供业务结果，Step的ID和执行状态由程序维护。

    只读取当前Prompt约定的三个必填字段；模型额外返回的status等
    字段不能写入Step，避免incomplete_partial这类自造状态影响执行。
    缺少业务字段时必须纠正输出，不能用默认值悄悄丢掉已有结果。
    """
    model_config = ConfigDict(extra="ignore")

    success: StrictBool
    result: StrictStr
    attachments: list[StrictStr]


class ReActAgent(BaseAgent):
    """基于ReAct架构的执行Agent"""
    name: str = "react"
    _system_prompt: str = SYSTEM_PROMPT + REACT_SYSTEM_PROMPT
    _format: str = "json_object"  # format控制的是content、工具调用控制的是tool_calls两者不冲突

    async def _parse_step_result(self, content: str) -> _StepResult:
        """结果不符合协议时只纠正一次，不重新执行任务或调用工具。"""
        try:
            return _StepResult.model_validate(await self._json_parser.invoke(content))
        except ValueError as exc:
            logger.warning("步骤结果不符合约定，将进行一次无工具格式纠正: %s", exc)

        # 原回复已在BaseAgent中保存进上下文，这里只要求转换格式。
        # 不让模型重新执行工具，也不根据status猜测success，避免把部分结果变成成功。
        response = await self._invoke_llm(
            [{"role": "user", "content": (
                "上一条步骤回复不符合返回协议。只将上一条已有结果转换为JSON对象，"
                "必须且仅包含success（布尔值）、result（字符串）、attachments（字符串数组）。"
                "不要调用工具、检索或补做任务，不得添加status字段或Markdown代码围栏。"
                "未完成、仅部分完成或无法核实的步骤必须success=false；"
                "将已有可靠结果及未完成原因保留在result中，attachments只保留已生成的文件。"
            )}],
            self._format,
            tools_enabled=False,
        )
        if response.get("tool_calls"):
            raise ValueError("步骤结果格式纠正时不允许调用工具")
        return _StepResult.model_validate(
            await self._json_parser.invoke(response.get("content") or "")
        )

    async def execute_step(self, plan: Plan, step: Step, message: Message) -> AsyncGenerator[BaseEvent, None]:
        """根据传递的消息+规划+子步骤，执行相应的子步骤"""
        # 1.根据传递的内容生成执行消息
        query = EXECUTION_PROMPT.format(
            message=message.message,
            attachments="\n".join(message.attachments),
            language=plan.language,
            step=step.description,
        )

        # 2.更新步骤的执行状态为运行中并返回Step事件
        step.status = ExecutionStatus.RUNNING
        step.error = None  # 新尝试保留已有结果，清除上一次执行错误。
        yield StepEvent(step=step, status=StepEventStatus.STARTED)

        # 3.调用invoke获取agent返回的事件内容
        async for event in self.invoke(query):
            # 4.判断事件类型执行不同操作
            if isinstance(event, ToolEvent):
                # 5.工具事件需要判断工具的名称是否为message_ask_user
                if event.function_name == "message_ask_user":
                    # 6.工具如果在调用中，我们需要返回一条消息告知用户需要让用户处理什么
                    if event.status == ToolEventStatus.CALLING:
                        yield MessageEvent(
                            role="assistant",
                            message=event.function_args.get("text", "")
                        )
                    elif event.status == ToolEventStatus.CALLED:
                        # 7.如果工具事件为已调用，则需要返回等待事件并中断程序
                        yield WaitEvent(
                            tool_call_id=event.tool_call_id,
                            tool_name=event.tool_name,
                            function_name=event.function_name,
                        )
                        return
                    continue
            elif isinstance(event, MessageEvent):
                # 8.先解析业务结果，不能把模型输出直接当作完整Step。
                # 校验成功后再结束步骤，模型无权通过status改写流程状态。
                new_step = await self._parse_step_result(event.message)
                step.status = ExecutionStatus.COMPLETED

                # 10.更新子步骤的数据
                step.success = new_step.success
                step.result = new_step.result
                step.attachments = new_step.attachments

                # 11.返回步骤完成事件
                yield StepEvent(step=step, status=StepEventStatus.COMPLETED)

                # 12.如果子步骤拿到了结果，还需要返回一段消息给用户(将结果返回给用户)
                if step.result:
                    yield MessageEvent(role="assistant", message=step.result)
                continue
            elif isinstance(event, ErrorEvent):
                # 13.错误事件更新步骤的状态
                step.status = ExecutionStatus.FAILED
                step.error = event.error

                # 14.返回子步骤对应事件
                yield StepEvent(step=step, status=StepEventStatus.FAILED)
                yield event
                # 即使调用方继续消费生成器，也不能再把失败Step改成completed。
                return

            # 15.其他场景将事件直接返回
            yield event

        # 16.循环迭代完成后代表子步骤已实现，需要更新状态
        step.status = ExecutionStatus.COMPLETED

    async def summarize(self) -> AsyncGenerator[BaseEvent, None]:
        """调用Agent汇总历史的消息并生成最终回复+附件"""
        # 1.构建请求query
        query = SUMMARIZE_PROMPT

        # 2.调用invoke方法获取Agent生成的事件
        async for event in self.invoke(query):
            # 3.判断事件类型是否为消息事件，如果是则表示Agent结构化生成汇总内容
            if isinstance(event, MessageEvent):
                # 4.记录日志并解析输出内容
                logger.info(f"执行Agent生成汇总内容: {event.message}")
                parsed_obj = await self._json_parser.invoke(event.message)

                # 5.将解析数据转换为Message对象
                message = Message.model_validate(parsed_obj)

                # 6.提取消息中的附件信息
                attachments = [File(filepath=filepath) for filepath in message.attachments]

                # 7.返回消息事件并将消息+附件进行相应
                yield MessageEvent(
                    role="assistant",
                    message=message.message,
                    attachments=attachments,
                )
            else:
                # 8.其他事件则直接返回
                yield event
