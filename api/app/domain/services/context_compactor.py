#!/usr/bin/env python
# -*- coding: utf-8 -*-
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Callable, cast

from openai.types.chat import ChatCompletionMessageParam

from app.domain.external.llm import LLM
from app.domain.models.agent_context_summary import AgentContextSummary
from app.domain.models.agent_message import AgentMessage
from app.domain.models.app_config import AgentConfig
from app.domain.repositories.uow import IUnitOfWork
from app.domain.services.token_counter import (
    TokenCounter,
    create_token_counter,
)

logger = logging.getLogger(__name__)


class ContextWindowLimitError(RuntimeError):
    """上下文超过硬限制且无法安全压缩。"""


class ContextCompactor:
    """Agent上下文压缩服务。

    主要职责：

    1. 查询指定Session和Agent当前的摘要；
    2. 查询尚未压缩、尚未排除的AgentMessage；
    3. 计算当前上下文的Token数量；
    4. 超过阈值时，把较早的消息压缩成Session级摘要；
    5. 保留最近一部分原始消息；
    6. 保证assistant.tool_calls和对应tool消息不会被拆开；
    7. 原子保存新摘要并标记已经进入摘要的消息。

    该服务不负责：

    - 保存Agent正常对话消息；
    - 构建最终Memory；
    - 修改当前BaseAgent中的_memory；
    - 修复损坏的工具调用链。

    压缩成功后，调用方应使用ContextBuilder重新构建Memory。
    """

    def __init__(
        self,
        uow_factory: Callable[[], IUnitOfWork],
        llm: LLM,
        agent_config: AgentConfig,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._llm = llm
        self._agent_config = agent_config
        self._token_counter = token_counter or create_token_counter(
            agent_config.context_tokenizer_path
        )
        self._uses_model_tokenizer = bool(
            agent_config.context_tokenizer_path
        )

    async def compact_if_needed(
        self,
        session_id: str,
        agent_name: str,
        system_prompt: str,
    ) -> bool:
        """检查上下文Token数量，并在超过阈值时执行压缩。

        Args:
            session_id:
                当前会话ID。摘要按照Session持续积累。

            agent_name:
                Agent名称，例如planner或react。
                不同Agent分别维护自己的摘要。

            system_prompt:
                当前Agent系统提示词。系统提示词不会被写入摘要，
                但需要参与上下文Token总量计算。

        Returns:
            True:
                当前方法生成了新摘要，或者检测到其他任务已经完成压缩。
                调用方应重新构建Memory。

            False:
                当前上下文未达到压缩阈值，或者暂时没有可安全压缩的消息。
        """

        started_at = time.perf_counter()

        # ------------------------------------------------------------
        # 第一阶段：读取摘要和活跃消息
        #
        # 这里只执行短时间数据库查询。离开UoW后数据库事务已经结束，
        # 后面的LLM调用不会长时间占用数据库连接和事务。
        # ------------------------------------------------------------
        read_uow = self._uow_factory()

        async with read_uow:
            old_summary = await read_uow.agent_context.get_summary(
                session_id=session_id,
                agent_name=agent_name,
            )

            active_messages = (
                await read_uow.agent_context.list_active_by_session(
                    session_id=session_id,
                    agent_name=agent_name,
                )
            )

        if not active_messages:
            return False

        # ------------------------------------------------------------
        # 第二阶段：判断是否达到压缩阈值
        # ------------------------------------------------------------
        total_tokens = self._calculate_context_tokens(
            system_prompt=system_prompt,
            summary=old_summary,
            active_messages=active_messages,
        )

        if (
            total_tokens
            <= self._agent_config.context_compact_threshold_tokens
        ):
            logger.debug(
                "Agent[%s]当前上下文约%s Tokens，未达到压缩阈值%s",
                agent_name,
                total_tokens,
                self._agent_config.context_compact_threshold_tokens,
            )
            return False

        logger.info(
            "Agent[%s]当前上下文约%s Tokens，达到压缩阈值%s",
            agent_name,
            total_tokens,
            self._agent_config.context_compact_threshold_tokens,
        )

        # ------------------------------------------------------------
        # 第三阶段：按工具调用链组成不可拆分消息块
        #
        # 一个消息块可能是：
        #
        # 1. 一条普通user/assistant消息；
        # 2. assistant.tool_calls以及紧随其后的所有tool结果。
        #
        # 后续只能以完整消息块为边界执行压缩。
        # ------------------------------------------------------------
        message_blocks = self._build_atomic_blocks(active_messages)

        compacted_messages = self._select_messages_to_compact(
            message_blocks=message_blocks,
            keep_recent_tokens=(
                self._agent_config.context_keep_recent_tokens
            ),
        )

        if not compacted_messages:
            message = (
                f"Agent[{agent_name}]上下文已经超过压缩阈值，"
                "但没有找到可安全压缩的完整消息前缀"
            )

            if total_tokens >= self._agent_config.context_hard_limit_tokens:
                raise ContextWindowLimitError(
                    f"{message}；当前约{total_tokens} Tokens，"
                    "已达到上下文硬限制"
                    f"{self._agent_config.context_hard_limit_tokens}"
                )

            logger.warning(message)
            return False

        last_compacted_message = compacted_messages[-1]

        if last_compacted_message.sequence is None:
            raise RuntimeError(
                "准备压缩的AgentMessage缺少sequence，无法记录摘要进度"
            )

        # ------------------------------------------------------------
        # 第四阶段：调用LLM生成增量摘要
        #
        # 注意：
        #
        # 1. 此时没有打开数据库事务；
        # 2. 直接调用底层LLM；
        # 3. 不能调用BaseAgent._invoke_llm()；
        # 4. 否则摘要请求也会写入agent_messages，造成递归。
        # ------------------------------------------------------------
        try:
            new_summary_text = await self._generate_summary(
                previous_summary=(
                    old_summary.summary
                    if old_summary is not None
                    else ""
                ),
                messages=compacted_messages,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started_at) * 1000

            if total_tokens < self._agent_config.context_hard_limit_tokens:
                # 仍低于模型输入硬限制时，摘要失败不应阻断正常业务调用。
                logger.exception(
                    "Agent[%s]上下文压缩失败，暂时使用未压缩上下文继续："
                    "tokens=%s, hard_limit=%s, elapsed_ms=%.2f",
                    agent_name,
                    total_tokens,
                    self._agent_config.context_hard_limit_tokens,
                    elapsed_ms,
                )
                return False

            raise ContextWindowLimitError(
                f"Agent[{agent_name}]上下文约{total_tokens} Tokens，"
                "已达到硬限制且摘要生成失败"
            ) from exc

        now = datetime.now(timezone.utc)

        if old_summary is None:
            new_summary = AgentContextSummary(
                session_id=session_id,
                agent_name=agent_name,
                last_run_id=last_compacted_message.run_id,
                summary=new_summary_text,
                summarized_through_sequence=(
                    last_compacted_message.sequence
                ),
                version=1,
                token_count=self._token_counter.count_text(new_summary_text),
                created_at=now,
                updated_at=now,
            )
        else:
            # 保留原摘要ID和创建时间，更新摘要内容和版本。
            new_summary = old_summary.model_copy(
                update={
                    "last_run_id": last_compacted_message.run_id,
                    "summary": new_summary_text,
                    "summarized_through_sequence": (
                        last_compacted_message.sequence
                    ),
                    "version": old_summary.version + 1,
                    "token_count": self._token_counter.count_text(
                        new_summary_text
                    ),
                    "updated_at": now,
                }
            )

        # ------------------------------------------------------------
        # 第五阶段：保存摘要并标记已压缩消息
        #
        # 两个数据库操作必须处于同一个UoW：
        #
        # 1. 摘要保存成功但消息没有标记，会导致摘要和原消息重复进入上下文；
        # 2. 消息标记成功但摘要保存失败，会造成历史消息丢失。
        #
        # 同一事务可以保证它们同时成功或同时回滚。
        # ------------------------------------------------------------
        write_uow = self._uow_factory()

        async with write_uow:
            saved = await write_uow.agent_context.save_summary(
                new_summary,
                expected_version=(
                    old_summary.version
                    if old_summary is not None
                    else None
                ),
            )

            if not saved:
                elapsed_ms = (time.perf_counter() - started_at) * 1000
                logger.warning(
                    "Agent[%s]生成摘要期间上下文摘要已经被其他任务更新，"
                    "放弃本次写入并要求调用方重新构建Memory："
                    "tokens_before=%s, elapsed_ms=%.2f",
                    agent_name,
                    total_tokens,
                    elapsed_ms,
                )

                # 返回True，让BaseAgent重新读取数据库中的最新摘要。
                return True

            await write_uow.agent_context.mark_compacted_through(
                session_id=session_id,
                agent_name=agent_name,
                sequence=last_compacted_message.sequence,
            )

        compacted_tokens = sum(
            self._count_message_tokens(message)
            for message in compacted_messages
        )
        retained_tokens = sum(
            self._count_message_tokens(message)
            for message in active_messages
        ) - compacted_tokens
        result_tokens = (
            self._token_counter.count_text(system_prompt)
            + new_summary.token_count
            + retained_tokens
        )
        elapsed_ms = (time.perf_counter() - started_at) * 1000

        logger.info(
            "Agent[%s]上下文压缩完成："
            "version=%s, last_run_id=%s, "
            "summarized_through_sequence=%s, "
            "compacted_messages=%s, tokens_before=%s, "
            "tokens_after=%s, compacted_tokens=%s, "
            "retained_tokens=%s, elapsed_ms=%.2f",
            agent_name,
            new_summary.version,
            new_summary.last_run_id,
            new_summary.summarized_through_sequence,
            len(compacted_messages),
            total_tokens,
            result_tokens,
            compacted_tokens,
            retained_tokens,
            elapsed_ms,
        )

        return True

    def _calculate_context_tokens(
        self,
        system_prompt: str,
        summary: AgentContextSummary | None,
        active_messages: list[AgentMessage],
    ) -> int:
        """估算当前完整上下文占用的Token数量。

        当前完整上下文由以下部分组成：

        1. Agent系统提示词；
        2. Session级历史摘要；
        3. 尚未压缩的原始消息。
        """

        system_tokens = self._token_counter.count_text(system_prompt)

        summary_tokens = self._count_summary_tokens(summary)

        active_message_tokens = sum(
            self._count_message_tokens(message)
            for message in active_messages
        )

        return (
            system_tokens
            + summary_tokens
            + active_message_tokens
        )

    def _count_summary_tokens(
        self,
        summary: AgentContextSummary | None,
    ) -> int:
        if summary is None:
            return 0

        # 启用真实Tokenizer后重新计算已有摘要，避免历史估算值继续污染
        # 新算法。未启用时直接使用写入数据库的估算值，减少重复序列化。
        if self._uses_model_tokenizer:
            return self._token_counter.count_text(summary.summary)

        return summary.token_count

    def _count_message_tokens(self, message: AgentMessage) -> int:
        if self._uses_model_tokenizer:
            return self._token_counter.count_message(message.message)

        return message.token_count

    @staticmethod
    def _build_atomic_blocks(
        messages: list[AgentMessage],
    ) -> list[list[AgentMessage]]:
        """把消息转换为不可拆分的消息块。

        assistant消息如果带有tool_calls，它和紧随其后的tool消息
        必须在同一个消息块中。

        示例：

            user
            assistant(tool_calls=[call_1])
            tool(tool_call_id=call_1)
            assistant

        将被分为：

            block 1: [user]
            block 2: [assistant(tool_calls), tool]
            block 3: [assistant]

        这样选择压缩边界时就不会只压缩assistant，而把tool留在活跃
        上下文中，也不会只压缩tool而保留对应的assistant。
        """

        blocks: list[list[AgentMessage]] = []
        index = 0

        while index < len(messages):
            current = messages[index]
            block = [current]
            index += 1

            tool_calls = (
                current.message.get("tool_calls") or []
                if current.role == "assistant"
                else []
            )

            if tool_calls:
                # 收集紧随assistant.tool_calls之后的连续tool消息。
                while (
                    index < len(messages)
                    and messages[index].role == "tool"
                ):
                    block.append(messages[index])
                    index += 1

            blocks.append(block)

        return blocks

    def _select_messages_to_compact(
        self,
        message_blocks: list[list[AgentMessage]],
        keep_recent_tokens: int,
    ) -> list[AgentMessage]:
        """选择需要进入摘要的历史消息前缀。

        选择规则：

        1. 从消息末尾向前保留最近keep_recent_tokens；
        2. 较早的消息作为本次压缩候选；
        3. 只能按完整消息块选择；
        4. 遇到损坏或不完整工具链时停止，不能跨过它继续压缩。

        因此返回值一定是原活跃消息的连续前缀。
        """

        if not message_blocks:
            return []

        # keep_start表示从哪个消息块开始保留原始消息。
        keep_start = len(message_blocks)
        retained_tokens = 0

        for block_index in range(
            len(message_blocks) - 1,
            -1,
            -1,
        ):
            if retained_tokens >= keep_recent_tokens:
                break

            block = message_blocks[block_index]
            block_tokens = sum(
                self._count_message_tokens(message)
                for message in block
            )

            retained_tokens += block_tokens
            keep_start = block_index

        candidate_blocks = message_blocks[:keep_start]

        if not candidate_blocks:
            return []

        compacted_messages: list[AgentMessage] = []

        for block in candidate_blocks:
            if not self._is_safe_compaction_block(block):
                logger.warning(
                    "检测到不完整或非法的工具调用消息块，"
                    "停止选择后续压缩消息"
                )
                break

            compacted_messages.extend(block)

        return compacted_messages

    @staticmethod
    def _is_safe_compaction_block(
        block: list[AgentMessage],
    ) -> bool:
        """判断一个消息块能否完整进入摘要。

        普通消息可以直接压缩。

        对于assistant.tool_calls，必须保证每个tool_call_id都存在对应的
        tool消息，否则该消息链尚未完成，不能进入历史摘要。

        孤立tool消息同样不能直接压缩，应先由
        repair_incomplete_tool_chain()修复或排除。
        """

        if not block:
            return False

        first = block[0]

        # 孤立tool消息不属于合法消息块。
        if first.role == "tool":
            return False

        if first.role != "assistant":
            return True

        tool_calls = first.message.get("tool_calls") or []

        if not tool_calls:
            return True

        expected_tool_call_ids = {
            tool_call.get("id")
            for tool_call in tool_calls
            if tool_call.get("id")
        }

        # assistant声明了工具调用，但工具调用本身没有合法ID。
        if not expected_tool_call_ids:
            return False

        actual_tool_call_ids = {
            message.tool_call_id
            for message in block[1:]
            if (
                message.role == "tool"
                and message.tool_call_id is not None
            )
        }

        return expected_tool_call_ids.issubset(
            actual_tool_call_ids
        )

    async def _generate_summary(
        self,
        previous_summary: str,
        messages: list[AgentMessage],
    ) -> str:
        """调用底层LLM生成新的增量摘要。

        previous_summary是之前已经生成的Session级摘要。

        messages是本次新进入摘要的消息。模型需要将两部分合并成一份
        完整的新摘要，而不是只总结本次消息。
        """

        serialized_messages = [
            self._serialize_message(message)
            for message in messages
        ]

        llm_messages = cast(
            list[ChatCompletionMessageParam],
            cast(object, [
                {
                    "role": "system",
                    "content": (
                        "你是AI Agent上下文压缩器。"
                        "你的任务是把旧摘要和新增历史消息合并成一份"
                        "可供Agent后续继续执行任务的中文摘要。\n\n"
                        "摘要必须保留：\n"
                        "1. 用户的主要目标和当前意图；\n"
                        "2. 用户明确提出的约束和偏好；\n"
                        "3. 已确认的重要事实和结论；\n"
                        "4. 已经完成的任务、修改和执行结果；\n"
                        "5. 重要工具调用及其结果；\n"
                        "6. 文件路径、标识符、参数和关键配置；\n"
                        "7. 失败原因、重试情况和未解决问题；\n"
                        "8. 当前进度、后续待办和等待用户确认的内容。\n\n"
                        "不要保留：\n"
                        "1. 重复内容；\n"
                        "2. 无意义寒暄；\n"
                        "3. 详细思维过程；\n"
                        "4. 已经被后续结论推翻的旧结论。\n\n"
                        "只输出摘要正文，不要输出JSON，"
                        "不要使用Markdown代码块。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "<previous_summary>\n"
                        f"{previous_summary.strip() or '无'}\n"
                        "</previous_summary>\n\n"
                        "<new_history>\n"
                        f"{json.dumps(
                            serialized_messages,
                            ensure_ascii=False,
                            separators=(',', ':'),
                        )}\n"
                        "</new_history>"
                    ),
                },
            ]),
        )

        last_error: Exception | None = None

        for attempt in range(
            1,
            self._agent_config.context_summary_max_retries + 1,
        ):
            try:
                response = await asyncio.wait_for(
                    self._llm.invoke(
                        messages=llm_messages,
                        tools=None,
                        response_format=None,
                        tool_choice=None,
                        max_tokens=(
                            self._agent_config
                            .context_summary_max_tokens
                        ),
                    ),
                    timeout=120,
                )

                content = response.get("content")

                if not isinstance(content, str):
                    raise RuntimeError(
                        "摘要模型返回的content不是字符串"
                    )

                content = content.strip()

                if not content:
                    raise RuntimeError(
                        "摘要模型返回了空内容"
                    )

                return content

            except asyncio.CancelledError:
                # 任务取消必须继续向外传播，不能作为普通LLM错误重试。
                raise

            except Exception as exc:
                last_error = exc

                logger.warning(
                    "Agent上下文摘要生成失败，第%s/%s次尝试：%s",
                    attempt,
                    self._agent_config.context_summary_max_retries,
                    str(exc) or type(exc).__name__,
                )

                if attempt < self._agent_config.context_summary_max_retries:
                    await asyncio.sleep(1)

        raise RuntimeError(
            "生成Agent上下文摘要失败，"
            f"已达到最大重试次数"
            f"({self._agent_config.context_summary_max_retries})"
        ) from last_error

    @staticmethod
    def _serialize_message(
        message: AgentMessage,
    ) -> dict[str, object]:
        """把AgentMessage转换为提供给摘要模型的安全结构。

        reasoning_content属于模型详细推理过程，不需要进入长期摘要。
        这里复制消息字典后再删除，避免修改领域对象中的原始数据。
        """

        message_data = dict(message.message)
        message_data.pop("reasoning_content", None)

        return {
            "run_id": message.run_id,
            "sequence": message.sequence,
            "role": message.role,
            "message": message_data,
        }
