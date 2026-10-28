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

    async def _invoke_llm(self, messages: List[Dict[str, Any]], format: Optional[str] = None) -> Dict[str, Any]:
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
        available_tools = [] if budget_exhausted else self._get_available_tools()
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
                        tool_choice=None if budget_exhausted else self._tool_choice,
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

        logger.info(f"调用之前传递的format={format}")

        # 2.调用语言模型获取响应内容
        message = await self._invoke_llm(
            [{"role": "user", "content": query}],
            format,
        )

        # 3.循环遍历直到最大迭代次数
        for _ in range(self._agent_config.max_iterations):
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
            message = await self._invoke_llm(tool_messages)
        else:
            # 13.超过最大迭代次数后，则抛出错误
            yield ErrorEvent(error=f"Agent迭代超过最大迭代次数: {self._agent_config.max_iterations}, 任务处理失败")

        # 14.在指定步骤内完成了迭代则返回消息事件
        if message and message.get("content") is not None:
            yield MessageEvent(message=message["content"])
        else:
            yield ErrorEvent(error="Agent未能生成有效回复内容")


    async def resume_waiting_tool_call(
            self,
            message: Message,
    ) -> bool:
        """将用户回答补充为message_ask_user对应的tool消息。

        返回True表示找到了等待回答的工具调用并成功补充，
        返回False表示当前Agent没有等待回答的工具调用。
        """
        read_uow = self._uow_factory()

        async with read_uow:
            records = await read_uow.agent_context.list_active_by_run(
                run_id=self._run_id,
                agent_name=self.name,
            )

        # 从后向前寻找当前Run最近一次尚未回答的message_ask_user。
        for assistant_index in range(len(records) - 1, -1, -1):
            assistant_record = records[assistant_index]

            if assistant_record.role != "assistant":
                continue

            assistant_message = assistant_record.message
            tool_calls = assistant_message.get("tool_calls") or []
            if not tool_calls:
                continue

            # 统计紧跟在assistant消息后的合法tool响应。
            answered_tool_call_ids: set[str] = set()
            insert_index = assistant_index + 1

            while (
                    insert_index < len(records)
                    and records[insert_index].role == "tool"
            ):
                tool_call_id = records[insert_index].tool_call_id

                if tool_call_id:
                    answered_tool_call_ids.add(tool_call_id)

                insert_index += 1

            for tool_call in tool_calls:
                tool_call_id = tool_call.get("id")
                function_name = (
                    tool_call.get("function", {}).get("name")
                )

                if function_name != "message_ask_user":
                    continue

                if not tool_call_id:
                    continue

                if tool_call_id in answered_tool_call_ids:
                    continue

                if insert_index != len(records):
                    logger.warning(
                        "Agent[%s]等待回答的工具调用[%s]后已经存在其他消息，"
                        "不能在消息尾部补写用户回答",
                        self.name,
                        tool_call_id,
                    )
                    return False

                # WAITING是工具调用链的终点，所以新增记录自然紧跟在
                # assistant.tool_calls之后，无需覆盖或重排旧消息。
                tool_message = {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "function_name": function_name,
                    "content": message.model_dump_json(),
                }

                await self._append_messages([tool_message])
                await self._reload_memory()

                logger.info(
                    "Agent[%s]已恢复等待用户回答的工具调用[%s]",
                    self.name,
                    tool_call_id,
                )

                return True

        return False

    async def repair_incomplete_tool_chain(self) -> bool:
        """修复中断产生的不完整工具调用消息链。

        assistant消息只要包含tool_calls，每个tool_call_id都必须立即
        跟随一条对应的tool消息，否则OpenAI兼容接口会返回HTTP 400。

        返回True表示修改了记忆，False表示原消息链已经完整。
        """

        read_uow = self._uow_factory()

        async with read_uow:
            # 上下文按Session连续，因此不仅检查当前Run，也要检查其他
            # 尚未压缩的Run，避免旧Run中的损坏链污染本次LLM请求。
            records = await read_uow.agent_context.list_active_by_session(
                session_id=self._session_id,
                agent_name=self.name,
            )

        if not records:
            return False

        excluded_ids: list[str] = []
        synthetic_messages: list[AgentMessage] = []
        index = 0

        while index < len(records):
            current_record = records[index]
            current_message = current_record.message

            # 没有紧跟assistant.tool_calls的tool消息本身也是非法的，
            # 因此把这类孤立tool消息标记为排除。
            if current_record.role == "tool":
                logger.warning(
                    "Agent[%s]排除孤立tool消息，tool_call_id=%s",
                    self.name,
                    current_record.tool_call_id,
                )
                excluded_ids.append(current_record.id)
                index += 1
                continue

            tool_calls = (
                current_message.get("tool_calls") or []
                if current_record.role == "assistant"
                else []
            )

            if not tool_calls:
                index += 1
                continue


            # tool_call id 存在
            expected_calls = {
                tool_call.get("id"): tool_call
                for tool_call in tool_calls
                if tool_call.get("id")
            }

            if not expected_calls:
                excluded_ids.append(current_record.id)
                logger.warning(
                    "Agent[%s]排除缺少tool_call_id的assistant消息，"
                    "assistant_message_id=%s",
                    self.name,
                    current_record.id,
                )
                index += 1
                continue

            answered_ids: set[str] = set()
            following_tool_records: list[AgentMessage] = []
            next_index = index + 1

            # 收集紧跟在assistant.tool_calls之后的tool消息。
            while (
                    next_index < len(records)
                    and records[next_index].role == "tool"
            ):
                tool_record = records[next_index]
                following_tool_records.append(tool_record)
                tool_call_id = tool_record.tool_call_id

                # 只保留当前assistant真正发出的工具调用结果，
                # 同一个tool_call_id只保留一次。
                if (
                        tool_call_id in expected_calls
                        and tool_call_id not in answered_ids
                ):
                    answered_ids.add(tool_call_id)
                else:
                    excluded_ids.append(tool_record.id)
                    logger.warning(
                        "Agent[%s]排除无效或重复tool消息，"
                        "tool_call_id=%s",
                        self.name,
                        tool_call_id,
                    )

                next_index += 1

            missing_calls = {
                tool_call_id: tool_call
                for tool_call_id, tool_call in expected_calls.items()
                if tool_call_id not in answered_ids
            }

            if missing_calls:
                if next_index < len(records):
                    # 数据库sequence只能向尾部追加。如果损坏链后面已经
                    # 存在其他消息，无法再把tool结果插回正确位置，因此
                    # 排除整个assistant工具调用块。
                    excluded_ids.append(current_record.id)
                    excluded_ids.extend(
                        record.id for record in following_tool_records
                    )
                    logger.warning(
                        "Agent[%s]排除无法原位修复的工具调用链，"
                        "assistant_message_id=%s",
                        self.name,
                        current_record.id,
                    )
                else:
                    # 损坏链位于活跃上下文末尾，可以安全追加synthetic
                    # tool结果，并沿用原assistant消息所属的run_id。
                    for tool_call_id, tool_call in missing_calls.items():
                        function_name = (
                            tool_call.get("function", {}).get("name")
                            or "unknown"
                        )
                        tool_message = {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "function_name": function_name,
                            "content": (
                                "任务执行期间发生中断，未取得该工具调用的"
                                "最终结果。该操作可能已经执行。继续任务前"
                                "必须检查实际状态，避免重复执行有副作用的操作。"
                            ),
                        }
                        synthetic_messages.append(
                            AgentMessage(
                                session_id=self._session_id,
                                run_id=current_record.run_id,
                                agent_name=self.name,
                                role="tool",
                                message=tool_message,
                                tool_call_id=tool_call_id,
                                token_count=self._token_counter.count_message(
                                    tool_message
                                ),
                            )
                        )
                        logger.warning(
                            "Agent[%s]补充中断工具调用结果，"
                            "tool_call_id=%s，function=%s",
                            self.name,
                            tool_call_id,
                            function_name,
                        )

            index = next_index

        # 同一条记录可能被多个检查分支发现，写库前去重。
        excluded_ids = list(dict.fromkeys(excluded_ids))

        if not excluded_ids and not synthetic_messages:
            return False

        # 排除错误记录和添加补偿消息必须在同一事务内完成。
        write_uow = self._uow_factory()
        async with write_uow:
            await write_uow.agent_context.mark_excluded(excluded_ids)
            if synthetic_messages:
                await write_uow.agent_context.append_many(
                    synthetic_messages
                )

        await self._reload_memory()
        return True
