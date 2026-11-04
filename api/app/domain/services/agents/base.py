#!/usr/bin/env python
# -*- coding: utf-8 -*-
import asyncio
import logging
import uuid
from abc import ABC
from typing import Optional, List, AsyncGenerator, Dict, Any, Callable

from app.domain.external.json_parser import JSONParser
from app.domain.external.llm import LLM
from app.domain.models.agent_message import AgentMessage
from app.domain.models.app_config import AgentConfig
from app.domain.models.event import ToolEvent, ToolEventStatus, ErrorEvent, MessageEvent, BaseEvent
from app.domain.models.memory import Memory
from app.domain.models.message import Message
from app.domain.models.tool_result import ToolResult
from app.domain.models.run_checkpoint import RunStopReason
from app.domain.repositories.uow import IUnitOfWork
from app.domain.services.context_builder import ContextBuilder
from app.domain.services.context_compactor import ContextCompactor
from app.domain.services.token_counter import create_token_counter
from app.domain.services.tools.base import BaseTool

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """基础Agent智能体"""
    name: str = ""  # 智能体名字
    _system_prompt: str = ""  # 系统预设prompt
    _format: Optional[str] = None  # Agent的响应格式
    _retry_interval: float = 1.0  # 重试间隔
    _tool_choice: Optional[str] = None  # 强制选择工具

    def __init__(
            self,
            uow_factory: Callable[[], IUnitOfWork],
            session_id: str,  # 会话id
            agent_config: AgentConfig,  # Agent配置
            llm: LLM,  # 语言模型协议
            json_parser: JSONParser,  # JSON输出解析器
            tools: List[BaseTool],  # 工具列表
            run_id: str # 运行的id
    ) -> None:
        """构造函数，完成Agent的初始化"""
        self._uow_factory = uow_factory
        self._uow = uow_factory()
        self._session_id = session_id
        self._agent_config = agent_config
        self._llm = llm
        self._memory: Optional[Memory] = None
        self._json_parser = json_parser
        self._tools = tools
        self._run_id = run_id
        self._token_counter = create_token_counter(
            agent_config.context_tokenizer_path
        )
        self._context_builder = ContextBuilder(
            uow_factory=uow_factory,
        )

        # 上下文压缩器
        self._context_compactor = ContextCompactor(
            llm=self._llm,
            uow_factory=uow_factory,
            agent_config=self._agent_config,
            token_counter=self._token_counter,
        )

    async def _ensure_memory(self) -> None:
        """首次使用Agent时，从数据库重新构建运行上下文。"""
        if self._memory is not None:
            return

        await self._reload_memory()

    async def _reload_memory(self) -> None:
        """从Session摘要和活跃消息重新构建当前Agent上下文。"""
        self._memory = await self._context_builder.build(
            session_id=self._session_id,
            agent_name=self.name,
            system_prompt=self._system_prompt,
            run_id=self._run_id,
        )


    async def _compact_if_needed(self) -> bool:
        """
            检查并压缩当前Agent的上下文。
            压缩成功后，数据库中的部分AgentMessage会被标记为已压缩，
            因此必须重新使用ContextBuilder构建Memory。
        """

        compacted = await self._context_compactor.compact_if_needed(
            session_id=self._session_id,
            agent_name=self.name,
            system_prompt=self._system_prompt,
        )

        if compacted:
            await self._reload_memory()

        return compacted



    def _get_available_tools(self) -> List[Dict[str, Any]]:
        """获取Agent所有可用的工具列表参数声明/Schema"""
        available_tools = []
        for tool in self._tools:
            available_tools.extend(tool.get_tools())
        return available_tools

    def _get_tool(self, tool_name: str) -> BaseTool:
        """获取对应工具所在的工具集/包"""
        # 1.循环遍历所有工具包
        for tool in self._tools:
            # 2.判断工具包中是否存在该工具
            if tool.has_tool(tool_name):
                return tool

        raise ValueError(f"未知工具: {tool_name}")

    async def _tool_budget_exhausted(self) -> bool:
        uow = self._uow_factory()
        async with uow:
            used = await uow.session_event.count_tool_calls_by_run(self._run_id)
        return used >= self._agent_config.max_tool_calls_per_run

    async def _invoke_llm(
            self,
            messages: List[Dict[str, Any]],
            format: Optional[str] = None,
            *,
            tools_enabled: bool = True,
    ) -> Dict[str, Any]:
        """调用语言模型并处理记忆内容"""


        # 新消息先保存到agent_messages并加入当前Memory。
        await self._add_to_memory(messages)

        # 2.组装语言模型的响应格式
        response_format = {"type": format} if format else None

        # 3.循环向LLM发起提问直到最大重试次数
        error = "调用语言模型发生错误"

        # 压缩使用独立的重试策略。放在业务LLM重试循环外，避免
        # “摘要重试次数 × 业务请求重试次数”的放大效应。
        await self._compact_if_needed()

        assert self._memory is not None

        # 额度用完后不要再把工具列表交给执行Agent，否则模型仍可产生
        # tool_calls，产生必须补齐的悬空调用；只让它基于已有结果收尾。
        budget_exhausted = (
            self.name == "react"
            and await self._tool_budget_exhausted()
        )
        # 输出格式纠正只能整理已经取得的结果，不能再次执行外部工具。
        available_tools = [] if budget_exhausted or not tools_enabled else self._get_available_tools()
        llm_messages = self._memory.get_messages()
        if budget_exhausted:
            llm_messages = [
                *llm_messages,
                {
                    "role": "system",
                    "content": (
                        "本轮任务的工具调用额度已用尽。不要继续调用工具、"
                        "搜索或新增任务步骤；请用要求的JSON格式总结已有结果，"
                        "明确说明哪些信息未能核实。"
                    ),
                },
            ]

        for attempt in range(1, self._agent_config.max_retries + 1):
            try:
                message = await asyncio.wait_for(
                    self._llm.invoke(
                        messages=llm_messages,
                        tools=available_tools,
                        response_format=response_format,
                        tool_choice=None if budget_exhausted or not tools_enabled else self._tool_choice,
                    ),
                    timeout=120,
                )

                if message.get("role") == "assistant":
                    if not message.get("content") and not message.get("tool_calls"):
                        logger.warning(
                            "LLM回复了空内容，第%s次重试",
                            attempt,
                        )
                        await self._add_to_memory([
                            {
                                "role": "assistant",
                                "content": "",
                            },
                            {
                                "role": "user",
                                "content": "AI无响应内容，请继续。",
                            },
                        ])

                        await asyncio.sleep(self._retry_interval)
                        continue

                    filtered_message: dict[str, Any] = {
                        "role": "assistant",
                        "content": message.get("content"),
                    }

                    if message.get("reasoning_content"):
                        filtered_message["reasoning_content"] = (
                            message["reasoning_content"]
                        )

                    if message.get("tool_calls"):
                        filtered_message["tool_calls"] = (
                            message["tool_calls"][:1]
                        )
                else:
                    logger.warning(
                        "LLM响应内容无法确认消息角色: %s",
                        message.get("role"),
                    )
                    filtered_message = message

                await self._add_to_memory([filtered_message])
                return filtered_message

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                error = str(exc) or type(exc).__name__

                logger.exception(
                    "调用语言模型发生错误，第%s/%s次尝试",
                    attempt,
                    self._agent_config.max_retries,
                )

                if attempt < self._agent_config.max_retries:
                    await asyncio.sleep(self._retry_interval)

        # 11.所有重试均已耗尽仍未获得有效响应，抛出异常避免返回None
        raise RuntimeError(f"调用语言模型失败, 已达到最大重试次数({self._agent_config.max_retries}): {error}")

    async def _invoke_tool(self, tool: BaseTool, tool_name: str, arguments: Dict[str, Any]) -> ToolResult:
        """传递工具包+工具名字+对应参数调用指定工具"""
        # 1.执行循环调用工具获取结果
        err = ""
        for _ in range(self._agent_config.max_retries):
            try:
                return await tool.invoke(tool_name, **arguments)
            except Exception as e:
                err = str(e)
                logger.exception(f"调用工具[{tool_name}]出错, 错误: {str(e)}")
                await asyncio.sleep(self._retry_interval)
                continue

        # 2.循环最大重试次数后没有结果则将错误作为工具的执行结果，让LLM自行处理
        return ToolResult(success=False, message=err)

    async def _append_messages(self,  messages: list[dict[str, Any]]) -> list[AgentMessage]:
        """将新增消息持久化到agent_messages表。"""

        records = [
            AgentMessage(
                session_id=self._session_id,
                run_id=self._run_id,
                agent_name=self.name,
                role=message["role"],
                message=message,
                tool_call_id=message.get("tool_call_id"),
                token_count=self._token_counter.count_message(message),
            )
            for message in messages
        ]

        uow = self._uow_factory()

        async with uow:
            return await uow.agent_context.append_many(records)

    async def _add_to_memory(self, messages: List[dict[str, Any]]) -> None:

        """保存新增消息，并同步更新当前Agent的内存上下文。"""

        # 1.先检查确保记忆是存在的
        if not messages:
            return

        await self._ensure_memory()

        assert self._memory is not None

        # 先持久化。数据库失败时，不修改内存，避免两边状态不一致。
        await self._append_messages(messages)

        # 持久化成功后，再更新当前Agent实例中的Memory。
        self._memory.add_messages(messages)


    async def roll_back(self, message: Message) -> None:
        """回滚当前Run末尾未完成的工具调用。

        message_ask_user表示Agent正在等待用户回答，此时将用户回答保存为
        对应的tool消息。其他未完成工具调用不能再进入上下文，通过
        excluded_at将assistant消息排除，而不是覆盖整个Session Memory。
        """
        read_uow = self._uow_factory()

        async with read_uow:
            records = await read_uow.agent_context.list_active_by_run(
                run_id=self._run_id,
                agent_name=self.name,
            )

        if not records:
            return

        last_record = records[-1]
        last_message = last_record.message
        tool_calls = (
            last_message.get("tool_calls") or []
            if last_record.role == "assistant"
            else []
        )

        if not tool_calls:
            return

        tool_call = tool_calls[0]
        tool_call_id = tool_call.get("id")
        function_name = tool_call.get("function", {}).get("name")

        if function_name == "message_ask_user" and tool_call_id:
            tool_message = {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "function_name": function_name,
                "content": message.model_dump_json(),
            }
            await self._append_messages([tool_message])
        else:
            write_uow = self._uow_factory()
            async with write_uow:
                await write_uow.agent_context.mark_excluded(
                    [last_record.id]
                )

        await self._reload_memory()

    async def invoke(self, query: str, format: Optional[str] = None) -> AsyncGenerator[BaseEvent, None]:
        """传递消息+响应格式调用程序生成异步迭代内容"""
        # 1.需要判断下是否传递了format
        format = format if format else self._format

        logger.info(f"给大模型传递的query={query}")

        # 2.调用语言模型获取响应内容
        message = await self._invoke_llm(
            [{"role": "user", "content": query}],
            format,
        )

        # 3.循环遍历直到最大迭代次数
        for iteration in range(self._agent_config.max_iterations):
            # 4.如果LLM响应为空或无工具调用则表示LLM生成了文本回答，这时候就是最终答案
            if not message or not message.get("tool_calls"):
                break

            # 5.循环遍历工具参数并执行
            tool_messages = []
            for tool_call in message["tool_calls"]:
                if not tool_call.get("function"):
                    continue

                if await self._tool_budget_exhausted():
                    yield ErrorEvent(
                        stop_reason=RunStopReason.BUDGET_EXHAUSTED,
                        error=(
                            "本轮任务工具调用已达到上限"
                            f"({self._agent_config.max_tool_calls_per_run})，"
                            "请缩小任务范围或开始新任务"
                        )
                    )
                    return

                # 6.取出调用工具id、名字、参数信息
                tool_call_id = tool_call["id"] or str(uuid.uuid4())
                function_name = tool_call["function"]["name"]
                function_args = await self._json_parser.invoke(tool_call["function"]["arguments"])

                # 7.取出Agent中对应的工具
                tool = self._get_tool(function_name)

                # 8.返回工具即将调用事件，其中tool_content比较特殊，需要在具体业务中进行实现，这里留空即可
                yield ToolEvent(
                    tool_call_id=tool_call_id,
                    tool_name=tool.name,
                    function_name=function_name,
                    function_args=function_args,
                    status=ToolEventStatus.CALLING,
                )

                # 9.调用工具并获取结果
                result = await self._invoke_tool(tool, function_name, function_args)

                # 10.返回工具调用结果，其中tool_content比较特殊，需要在业务中进行实现
                yield ToolEvent(
                    tool_call_id=tool_call_id,
                    tool_name=tool.name,
                    function_name=function_name,
                    function_args=function_args,
                    function_result=result,
                    status=ToolEventStatus.CALLED,
                )

                # 11.组装工具响应
                tool_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "function_name": function_name,
                    "content": result.model_dump_json(),
                })

            # 12.所有工具都执行完成后，调用LLM获取汇总消息二次提供
            # 首次请求和工具响应后的请求必须保持同一种输出格式，
            # 否则ReAct在执行工具后会丢失原本的JSON模式约束。
            if iteration == self._agent_config.max_iterations - 1:
                # 最后一轮工具已经执行，仍需把结果交给模型形成最终回复。
                # 本次只允许整理已有结果，不再提供工具，避免生成下一轮
                # 无法执行的调用。部分完成应据实说明，不能为了收尾虚构成功。
                tool_messages.append({
                    "role": "user",
                    "content": (
                        "当前步骤已达到工具迭代上限。请停止操作，按当前要求的返回格式"
                        "整理已有结果，明确未完成或未核实的部分；"
                        "若返回格式包含success，只有达到步骤目标时才设为true。"
                        "不要再调用工具，也不要声称执行了尚未完成的操作。"
                    ),
                })
                message = await self._invoke_llm(tool_messages, format, tools_enabled=False)
            else:
                message = await self._invoke_llm(tool_messages, format)

        # 13.循环自然结束不等于失败：最后一次请求可能已返回有效答案。
        # 仅在模型无视禁用工具约束时终止，不能执行超出迭代上限的调用，
        # 也不能把带有未执行tool_calls的回复当作最终结果。
        if message and message.get("tool_calls"):
            yield ErrorEvent(error="已达到Agent迭代上限，但模型仍请求调用工具，未能生成收尾结果")
            return

        # 14.在指定步骤内完成了迭代则返回消息事件
        if message and message.get("content") is not None:
            yield MessageEvent(message=message["content"])
        else:
            yield ErrorEvent(error="Agent未能生成有效回复内容")


    async def resume_waiting_tool_call(
            self,
            message: Message,
    ) -> bool:
        """把用户回答补充为当前Run末尾message_ask_user的tool响应。

        当前项目关闭了并行工具调用，并且只保存LLM返回的第一个
        tool_call。因此WAITING状态的合法上下文必须以一条assistant消息
        结束，并且该消息只能包含一个message_ask_user调用。

        返回True表示成功补充用户回答；返回False表示等待调用不属于
        当前Agent。调用方会同时检查Planner和ReAct，并要求至少一个成功。
        """

        read_uow = self._uow_factory()

        async with read_uow:
            last_record = await read_uow.agent_context.get_last_active_by_run(
                run_id=self._run_id,
                agent_name=self.name,
            )

        if last_record is None or last_record.role != "assistant":
            return False

        tool_calls = last_record.message.get("tool_calls") or []
        if len(tool_calls) != 1:
            return False

        tool_call = tool_calls[0]
        tool_call_id = tool_call.get("id")
        function_name = tool_call.get("function", {}).get("name")

        if function_name != "message_ask_user" or not tool_call_id:
            return False

        # 用户发送的是人类消息，但在LLM协议中它是
        # message_ask_user工具的返回值，因此必须保存为role=tool。
        tool_message = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "function_name": function_name,
            "content": message.model_dump_json(),
        }

        # 先持久化，再重建内存，避免数据库和当前Agent状态不一致。
        await self._append_messages([tool_message])
        await self._reload_memory()

        logger.info(
            "Agent[%s]已补充等待工具调用[%s]的用户回答",
            self.name,
            tool_call_id,
        )

        return True

    async def repair_interrupted_tool_call(self) -> bool:
        """补齐当前Run末尾因中断而缺失的普通工具响应。

        工具执行可能已经产生副作用，但进程在tool消息持久化前中断。
        此时只能追加一条明确标记“结果未知”的合成tool响应，使LLM消息
        协议重新合法，并提醒模型先检查实际状态再决定是否重试。

        message_ask_user必须由resume_waiting_tool_call()补入真实用户回答，
        不能在这里生成合成结果。
        """
        read_uow = self._uow_factory()

        async with read_uow:
            last_record = await read_uow.agent_context.get_last_active_by_run(
                run_id=self._run_id,
                agent_name=self.name,
            )

        # 最后一条不是assistant工具调用，说明当前Run末尾没有悬空调用。
        if last_record is None or last_record.role != "assistant":
            return False

        tool_calls = last_record.message.get("tool_calls") or []
        if not tool_calls:
            return False

        # LLM层关闭并行工具调用，BaseAgent也只保存第一个tool_call。
        # 出现多个调用说明持久化数据不符合当前协议，不能猜测如何修复。
        if len(tool_calls) != 1:
            raise RuntimeError(
                f"Agent[{self.name}]当前Run末尾包含多个tool_calls，"
                "无法按单工具协议恢复"
            )

        tool_call = tool_calls[0]
        tool_call_id = tool_call.get("id")
        function_name = tool_call.get("function", {}).get("name")

        if not tool_call_id or not function_name:
            raise RuntimeError(
                f"Agent[{self.name}]当前Run末尾的工具调用缺少id或function name"
            )

        if function_name == "message_ask_user":
            raise RuntimeError(
                "检测到尚未回答的message_ask_user，但Run未按WAITING状态恢复"
            )

        tool_message = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "function_name": function_name,
            "content": (
                "任务执行期间发生中断，未取得该工具调用的最终结果。"
                "该操作可能已经执行。继续任务前必须检查实际状态，"
                "避免重复执行有副作用的操作。"
            ),
        }

        await self._append_messages([tool_message])

        await self._reload_memory()

        logger.warning(
            "Agent[%s]已补充中断工具调用结果，tool_call_id=%s，function=%s",
            self.name,
            tool_call_id,
            function_name,
        )

        return True
