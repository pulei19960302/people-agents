#!/usr/bin/env python
# -*- coding: utf-8 -*-
import asyncio
import logging
import uuid
from abc import ABC
from typing import Optional, List, AsyncGenerator, Dict, Any, Callable

from app.domain.external.json_parser import JSONParser
from app.domain.external.llm import LLM
from app.domain.models.app_config import AgentConfig
from app.domain.models.event import ToolEvent, ToolEventStatus, ErrorEvent, MessageEvent, BaseEvent
from app.domain.models.memory import Memory
from app.domain.models.message import Message
from app.domain.models.tool_result import ToolResult
from app.domain.repositories.uow import IUnitOfWork
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

    async def _ensure_memory(self) -> None:
        """确保智能体记忆是存在的"""
        if self._memory is None:
            async with self._uow:
                self._memory = await self._uow.session.get_memory(self._session_id, self.name)

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

    async def _invoke_llm(self, messages: List[Dict[str, Any]], format: Optional[str] = None) -> Dict[str, Any]:
        """调用语言模型并处理记忆内容"""
        # 1.将消息添加到记忆中
        await self._add_to_memory(messages)

        # 2.组装语言模型的响应格式
        response_format = {"type": format} if format else None

        # 3.循环向LLM发起提问直到最大重试次数
        error = "调用语言模型发生错误"
        for _ in range(self._agent_config.max_retries):
            try:
                # 4.调用语言模型获取响应内容
                message = await asyncio.wait_for(self._llm.invoke(
                    messages=self._memory.get_messages(),
                    tools=self._get_available_tools(),
                    response_format=response_format,
                    tool_choice=self._tool_choice,
                ), timeout=120)

                logger.info(f"大模型回复了内容: {message}")

                # 5.处理AI响应内容避免空回复
                if message.get("role") == "assistant":
                    if not message.get("content") and not message.get("tool_calls"):
                        logger.warning(f"LLM回复了空内容，执行重试")
                        await self._add_to_memory([
                            {"role": "assistant", "content": ""},
                            {"role": "user", "content": "AI无响应内容，请继续。"}
                        ])
                        await asyncio.sleep(self._retry_interval)
                        continue

                    # 6.取出非空消息并处理工具调用(兼容DeepSeek思考模型的写法)
                    filtered_message = {"role": "assistant", "content": message.get("content")}
                    if message.get("reasoning_content"):
                        filtered_message["reasoning_content"] = message.get("reasoning_content")
                    if message.get("tool_calls"):
                        # 7.取出工具调用的数据，限制LLM一次只能调用工具
                        filtered_message["tool_calls"] = message.get("tool_calls")[:1]
                else:
                    # 8.非AI消息则记录日志并存储message
                    logger.warning(f"LLM响应内容无法确认消息角色: {message.get('role')}")
                    filtered_message = message

                # 9.将消息添加到记忆中
                await self._add_to_memory([filtered_message])
                return filtered_message
            except Exception as e:
                # 10.记录日志并睡眠指定的时间
                logger.error(f"调用语言模型发生错误: {str(e)}")
                error = str(e)
                await asyncio.sleep(self._retry_interval)
                continue

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

    async def _add_to_memory(self, messages: List[Dict[str, Any]]) -> None:
        """将对应的信息添加到记忆中"""
        # 1.先检查确保记忆是存在的
        await self._ensure_memory()

        # 2.检查记忆的消息列表是否为空，如果是空则需要添加预设prompt作为初始记忆
        if self._memory.empty:
            self._memory.add_message({
                "role": "system", "content": self._system_prompt,
            })

        # 3.将正常消息添加到记忆中
        self._memory.add_messages(messages)

        # 4.将记忆持久化到数据仓库中
        async with self._uow:
            await self._uow.session.save_memory(self._session_id, self.name, self._memory)

    async def compact_memory(self) -> None:
        """压缩Agent的记忆"""
        await self._ensure_memory()
        self._memory.compact()
        async with self._uow:
            await self._uow.session.save_memory(self._session_id, self.name, self._memory)

    async def roll_back(self, message: Message) -> None:
        """Agent的状态回滚，该函数用于确保Agent的消息列表状态是正确，用于发送新消息、暂停/停止任务、通知用户"""
        # 1.取出记忆中的最后一条消息，检查是否是工具调用
        await self._ensure_memory()
        last_message = self._memory.get_last_message()
        if (
                not last_message or
                not last_message.get("tool_calls") or
                len(last_message.get("tool_calls")) == 0
        ):
            return

        # 2.取出消息中的工具调用参数
        tool_call = last_message.get("tool_calls")[0]

        # 3.提取工具名字、id
        function_name = tool_call.get("function", {}).get("name")
        tool_call_id = tool_call.get("id")

        # 4.判断下当前的工具是不是通知用户(message_ask_user)
        if function_name == "message_ask_user":
            self._memory.add_message({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "function_name": function_name,
                "content": message.model_dump_json(),
            })
        else:
            # 5.否则直接删除最后一条消息
            self._memory.roll_back()

        # 6.将记忆持久化
        async with self._uow:
            await self._uow.session.save_memory(self._session_id, self.name, self._memory)

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
        await self._ensure_memory()
        assert self._memory is not None
        messages = self._memory.get_messages()

        # 从后向前寻找最近一次尚未回答的message_ask_user。
        for assistant_index in range(len(messages) - 1, -1, -1):
            assistant_message = messages[assistant_index]

            if assistant_message.get("role") != "assistant":
                continue

            tool_calls = assistant_message.get("tool_calls") or []
            if not tool_calls:
                continue

            # 统计紧跟在assistant消息后的合法tool响应。
            answered_tool_call_ids: set[str] = set()
            insert_index = assistant_index + 1

            while (
                    insert_index < len(messages)
                    and messages[insert_index].get("role") == "tool"
            ):
                tool_call_id = messages[insert_index].get(
                    "tool_call_id"
                )

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

                # 必须插入到assistant.tool_calls之后，
                # 不能简单追加到消息列表末尾。
                messages.insert(
                    insert_index,
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "function_name": function_name,
                        "content": message.model_dump_json(),
                    },
                )
                uow = self._uow_factory()
                async with uow:
                    await uow.session.save_memory(
                        session_id=self._session_id,
                        agent_name=self.name,
                        memory=self._memory,
                    )

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

        await self._ensure_memory()
        assert self._memory is not None

        original_messages = list(
            self._memory.get_messages()
        )

        repaired_messages: list[dict[str, Any]] = []
        changed = False
        index = 0

        while index < len(original_messages):
            current_message = original_messages[index]

            # 没有紧跟assistant.tool_calls的tool消息本身也是非法的，
            # 因此忽略这类孤立tool消息。
            if current_message.get("role") == "tool":
                logger.warning(
                    "Agent[%s]删除孤立tool消息，tool_call_id=%s",
                    self.name,
                    current_message.get("tool_call_id"),
                )
                changed = True
                index += 1
                continue

            repaired_messages.append(current_message)

            tool_calls = (
                current_message.get("tool_calls") or []
                if current_message.get("role") == "assistant"
                else []
            )

            if not tool_calls:
                index += 1
                continue

            expected_calls = {
                tool_call.get("id"): tool_call
                for tool_call in tool_calls
                if tool_call.get("id")
            }

            answered_ids: set[str] = set()
            next_index = index + 1

            # 收集紧跟在assistant.tool_calls之后的tool消息。
            while (
                    next_index < len(original_messages)
                    and original_messages[next_index].get("role")
                    == "tool"
            ):
                tool_message = original_messages[next_index]
                tool_call_id = tool_message.get("tool_call_id")

                # 只保留当前assistant真正发出的工具调用结果，
                # 同一个tool_call_id只保留一次。
                if (
                        tool_call_id in expected_calls
                        and tool_call_id not in answered_ids
                ):
                    repaired_messages.append(tool_message)
                    answered_ids.add(tool_call_id)
                else:
                    changed = True
                    logger.warning(
                        "Agent[%s]删除无效或重复tool消息，"
                        "tool_call_id=%s",
                        self.name,
                        tool_call_id,
                    )

                next_index += 1

            # 为缺失结果的工具调用补充“结果未知”消息。
            for tool_call_id, tool_call in expected_calls.items():
                if tool_call_id in answered_ids:
                    continue

                function_name = (
                        tool_call.get("function", {}).get("name")
                        or "unknown"
                )

                repaired_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "function_name": function_name,
                    "content": (
                        "任务执行期间发生中断，未取得该工具调用的最终结果。"
                        "该操作可能已经执行。继续任务前需要检查实际状态，"
                        "避免重复执行具有副作用的操作。"
                    ),
                })

                changed = True

                logger.warning(
                    "Agent[%s]修复未完成工具调用，"
                    "tool_call_id=%s，function=%s",
                    self.name,
                    tool_call_id,
                    function_name,
                )

            index = next_index

        if not changed:
            return False

        self._memory.messages = repaired_messages

        uow = self._uow_factory()
        async with uow:
            await uow.session.save_memory(
                session_id=self._session_id,
                agent_name=self.name,
                memory=self._memory,
            )

        return True