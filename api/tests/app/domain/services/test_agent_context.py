import asyncio
from datetime import datetime, timezone
from typing import Any

from app.domain.models.agent_context_summary import AgentContextSummary
from app.domain.models.agent_message import AgentMessage
from app.domain.models.app_config import AgentConfig
from app.domain.models.message import Message
from app.domain.models.event import ErrorEvent
from app.domain.services.agents.base import BaseAgent
from app.domain.services.context_builder import ContextBuilder
from app.domain.services.context_compactor import (
    ContextCompactor,
    ContextWindowLimitError,
)
from app.domain.services.token_counter import ModelTokenCounter
from app.domain.services.tools.base import BaseTool, tool


class FakeBudgetTool(BaseTool):
    name = "test"

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    @tool(name="test_call", description="test", parameters={}, required=[])
    async def test_call(self):
        self.calls += 1
        return {"success": True}


def test_tool_budget_stops_before_extra_execution() -> None:
    async def run_test() -> None:
        tool = FakeBudgetTool()
        agent = object.__new__(BaseAgent)
        agent._agent_config = AgentConfig(max_tool_calls_per_run=1)
        agent._tools = [tool]

        async def budget_exhausted() -> bool:
            return True

        async def first_response(messages, format=None):
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "tool-1",
                    "function": {"name": "test_call", "arguments": "{}"},
                }],
            }

        agent._tool_budget_exhausted = budget_exhausted
        agent._invoke_llm = first_response
        events = [event async for event in agent.invoke("test")]
        assert len(events) == 1
        assert isinstance(events[0], ErrorEvent)
        assert "达到上限" in events[0].error
        assert tool.calls == 0

    asyncio.run(run_test())


class FakeAgentContextRepository:
    def __init__(
        self,
        messages: list[AgentMessage] | None = None,
        summary: AgentContextSummary | None = None,
    ) -> None:
        self.messages = list(messages or [])
        self.summary = summary
        self.saved_summaries: list[AgentContextSummary] = []
        self.compacted_through: list[int] = []

    async def append_many(
        self,
        messages: list[AgentMessage],
    ) -> list[AgentMessage]:
        next_sequence = max(
            (item.sequence or 0 for item in self.messages),
            default=0,
        )
        stored: list[AgentMessage] = []

        for message in messages:
            next_sequence += 1
            record = message.model_copy(
                update={"sequence": next_sequence}
            )
            self.messages.append(record)
            stored.append(record)

        return stored

    async def list_active_by_session(
        self,
        session_id: str,
        agent_name: str,
    ) -> list[AgentMessage]:
        return [
            item
            for item in self.messages
            if (
                item.session_id == session_id
                and item.agent_name == agent_name
                and item.compacted_at is None
                and item.excluded_at is None
            )
        ]

    async def list_active_by_run(
        self,
        run_id: str,
        agent_name: str,
    ) -> list[AgentMessage]:
        return [
            item
            for item in self.messages
            if (
                item.run_id == run_id
                and item.agent_name == agent_name
                and item.compacted_at is None
                and item.excluded_at is None
            )
        ]

    async def get_summary(
        self,
        session_id: str,
        agent_name: str,
    ) -> AgentContextSummary | None:
        if self.summary is None:
            return None

        if (
            self.summary.session_id == session_id
            and self.summary.agent_name == agent_name
        ):
            return self.summary

        return None

    async def save_summary(
        self,
        summary: AgentContextSummary,
        expected_version: int | None,
    ) -> bool:
        if expected_version is None:
            if self.summary is not None:
                return False
        elif (
            self.summary is None
            or self.summary.version != expected_version
        ):
            return False

        self.summary = summary
        self.saved_summaries.append(summary)
        return True

    async def mark_compacted_through(
        self,
        session_id: str,
        agent_name: str,
        sequence: int,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.compacted_through.append(sequence)
        self.messages = [
            item.model_copy(update={"compacted_at": now})
            if (
                item.session_id == session_id
                and item.agent_name == agent_name
                and item.sequence is not None
                and item.sequence <= sequence
                and item.excluded_at is None
            )
            else item
            for item in self.messages
        ]

    async def mark_excluded(
        self,
        message_ids: list[str],
    ) -> None:
        now = datetime.now(timezone.utc)
        ids = set(message_ids)
        self.messages = [
            item.model_copy(update={"excluded_at": now})
            if item.id in ids
            else item
            for item in self.messages
        ]


class FakeUnitOfWork:
    def __init__(
        self,
        repository: FakeAgentContextRepository,
    ) -> None:
        self.agent_context = repository

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc_val: object,
        exc_tb: object,
    ) -> bool:
        return False


class FakeLLM:
    async def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        tool_choice: str | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": "用户正在完成上下文存储改造，旧消息已经压缩。",
        }


class FailingLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        tool_choice: str | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        self.calls += 1
        raise RuntimeError("摘要服务暂时不可用")


class FakeJSONParser:
    async def invoke(
        self,
        text: str,
        default_value: Any = None,
    ) -> Any:
        return default_value


class DemoAgent(BaseAgent):
    name = "react"
    _system_prompt = "你是测试Agent。"


def make_message(
    *,
    message_id: str,
    sequence: int,
    role: str,
    message: dict[str, Any],
    run_id: str = "run-1",
    token_count: int = 20,
) -> AgentMessage:
    return AgentMessage(
        id=message_id,
        session_id="session-1",
        run_id=run_id,
        agent_name="react",
        sequence=sequence,
        role=role,
        message=message,
        tool_call_id=message.get("tool_call_id"),
        token_count=token_count,
    )


def make_agent(
    repository: FakeAgentContextRepository,
) -> DemoAgent:
    return DemoAgent(
        uow_factory=lambda: FakeUnitOfWork(repository),
        session_id="session-1",
        agent_config=AgentConfig(
            max_retries=2,
            context_compact_threshold_tokens=100,
            context_keep_recent_tokens=20,
        ),
        llm=FakeLLM(),
        json_parser=FakeJSONParser(),
        tools=[],
        run_id="run-1",
    )


def test_context_builder_orders_system_summary_and_active_messages() -> None:
    async def run_test() -> None:
        summary = AgentContextSummary(
            session_id="session-1",
            agent_name="react",
            summary="此前已经完成需求分析。",
            summarized_through_sequence=2,
        )
        repository = FakeAgentContextRepository(
            summary=summary,
            messages=[
                make_message(
                    message_id="old",
                    sequence=2,
                    role="user",
                    message={"role": "user", "content": "旧消息"},
                ),
                make_message(
                    message_id="active",
                    sequence=3,
                    role="user",
                    message={"role": "user", "content": "新消息"},
                ),
            ],
        )
        builder = ContextBuilder(
            uow_factory=lambda: FakeUnitOfWork(repository)
        )

        memory = await builder.build(
            session_id="session-1",
            agent_name="react",
            system_prompt="系统规则",
        )

        messages = memory.get_messages()
        assert messages[0] == {
            "role": "system",
            "content": "系统规则",
        }
        assert messages[1]["role"] == "system"
        assert "此前已经完成需求分析" in messages[1]["content"]
        assert messages[2] == {
            "role": "user",
            "content": "新消息",
        }
        assert len(messages) == 3

    asyncio.run(run_test())


def test_context_builder_does_not_copy_prior_run_raw_messages() -> None:
    async def run_test() -> None:
        repository = FakeAgentContextRepository(messages=[
            make_message(
                message_id="old-task", sequence=1, role="user",
                message={"role": "user", "content": "核查AI周报"},
                run_id="run-old",
            ),
            make_message(
                message_id="current-task", sequence=2, role="user",
                message={"role": "user", "content": "分析就业形势"},
                run_id="run-new",
            ),
        ])
        builder = ContextBuilder(
            uow_factory=lambda: FakeUnitOfWork(repository)
        )
        memory = await builder.build(
            session_id="session-1",
            agent_name="react",
            system_prompt="系统规则",
            run_id="run-new",
        )
        contents = [item["content"] for item in memory.get_messages()]
        assert "分析就业形势" in contents
        assert "核查AI周报" not in contents

    asyncio.run(run_test())


def test_context_compactor_keeps_tool_chain_atomic() -> None:
    async def run_test() -> None:
        repository = FakeAgentContextRepository(
            messages=[
                make_message(
                    message_id="user-1",
                    sequence=1,
                    role="user",
                    message={"role": "user", "content": "开始任务"},
                ),
                make_message(
                    message_id="assistant-tool",
                    sequence=2,
                    role="assistant",
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "browser_view",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                ),
                make_message(
                    message_id="tool-1",
                    sequence=3,
                    role="tool",
                    message={
                        "role": "tool",
                        "tool_call_id": "call-1",
                        "content": "工具结果",
                    },
                ),
                make_message(
                    message_id="recent",
                    sequence=4,
                    role="user",
                    message={"role": "user", "content": "最近消息"},
                ),
            ]
        )
        compactor = ContextCompactor(
            uow_factory=lambda: FakeUnitOfWork(repository),
            llm=FakeLLM(),
            agent_config=AgentConfig(
                max_retries=2,
                context_compact_threshold_tokens=30,
                context_keep_recent_tokens=10,
                context_summary_max_tokens=100,
            ),
        )

        compacted = await compactor.compact_if_needed(
            session_id="session-1",
            agent_name="react",
            system_prompt="",
        )

        assert compacted is True
        assert repository.summary is not None
        assert repository.summary.summarized_through_sequence == 3
        assert repository.summary.last_run_id == "run-1"
        assert repository.compacted_through == [3]
        active = await repository.list_active_by_session(
            "session-1",
            "react",
        )
        assert [item.id for item in active] == ["recent"]

    asyncio.run(run_test())


def test_resume_waiting_tool_call_persists_tool_message() -> None:
    async def run_test() -> None:
        repository = FakeAgentContextRepository(
            messages=[
                make_message(
                    message_id="waiting",
                    sequence=1,
                    role="assistant",
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "ask-1",
                                "type": "function",
                                "function": {
                                    "name": "message_ask_user",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                )
            ]
        )
        agent = make_agent(repository)

        resumed = await agent.resume_waiting_tool_call(
            Message(message="继续执行")
        )

        assert resumed is True
        assert repository.messages[-1].role == "tool"
        assert repository.messages[-1].tool_call_id == "ask-1"
        assert "继续执行" in repository.messages[-1].message["content"]

    asyncio.run(run_test())


def test_roll_back_excludes_unfinished_non_user_tool_call() -> None:
    async def run_test() -> None:
        repository = FakeAgentContextRepository(
            messages=[
                make_message(
                    message_id="unfinished",
                    sequence=1,
                    role="assistant",
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-shell",
                                "type": "function",
                                "function": {
                                    "name": "shell_exec",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                )
            ]
        )
        agent = make_agent(repository)

        await agent.roll_back(Message(message="开始新任务"))

        assert repository.messages[0].excluded_at is not None
        assert await repository.list_active_by_run(
            "run-1",
            "react",
        ) == []

    asyncio.run(run_test())


def test_repair_excludes_non_tail_broken_tool_chain() -> None:
    async def run_test() -> None:
        repository = FakeAgentContextRepository(
            messages=[
                make_message(
                    message_id="broken",
                    sequence=1,
                    role="assistant",
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "shell_exec",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                ),
                make_message(
                    message_id="later",
                    sequence=2,
                    role="user",
                    message={"role": "user", "content": "后续消息"},
                ),
            ]
        )
        agent = make_agent(repository)

        repaired = await agent.repair_incomplete_tool_chain()

        assert repaired is True
        broken = next(
            item for item in repository.messages
            if item.id == "broken"
        )
        assert broken.excluded_at is not None
        active = await repository.list_active_by_session(
            "session-1",
            "react",
        )
        assert [item.id for item in active] == ["later"]

    asyncio.run(run_test())


def test_repair_appends_synthetic_result_for_tail_tool_call() -> None:
    async def run_test() -> None:
        repository = FakeAgentContextRepository(
            messages=[
                make_message(
                    message_id="broken-tail",
                    sequence=1,
                    role="assistant",
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-tail",
                                "type": "function",
                                "function": {
                                    "name": "browser_navigate",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                )
            ]
        )
        agent = make_agent(repository)

        repaired = await agent.repair_incomplete_tool_chain()

        assert repaired is True
        assert repository.messages[-1].role == "tool"
        assert repository.messages[-1].tool_call_id == "call-tail"
        assert "可能已经执行" in repository.messages[-1].message["content"]

    asyncio.run(run_test())


def test_compaction_failure_degrades_below_hard_limit() -> None:
    async def run_test() -> None:
        repository = FakeAgentContextRepository(
            messages=[
                make_message(
                    message_id="old",
                    sequence=1,
                    role="user",
                    message={"role": "user", "content": "旧消息"},
                ),
                make_message(
                    message_id="recent",
                    sequence=2,
                    role="user",
                    message={"role": "user", "content": "最近消息"},
                ),
            ]
        )
        llm = FailingLLM()
        compactor = ContextCompactor(
            uow_factory=lambda: FakeUnitOfWork(repository),
            llm=llm,
            agent_config=AgentConfig(
                max_retries=3,
                context_compact_threshold_tokens=30,
                context_hard_limit_tokens=100,
                context_keep_recent_tokens=10,
                context_summary_max_retries=2,
            ),
        )

        compacted = await compactor.compact_if_needed(
            session_id="session-1",
            agent_name="react",
            system_prompt="",
        )

        assert compacted is False
        assert llm.calls == 2
        assert repository.summary is None
        assert repository.compacted_through == []

    asyncio.run(run_test())


def test_compaction_failure_stops_at_hard_limit() -> None:
    async def run_test() -> None:
        repository = FakeAgentContextRepository(
            messages=[
                make_message(
                    message_id="old",
                    sequence=1,
                    role="user",
                    message={"role": "user", "content": "旧消息"},
                ),
                make_message(
                    message_id="recent",
                    sequence=2,
                    role="user",
                    message={"role": "user", "content": "最近消息"},
                ),
            ]
        )
        llm = FailingLLM()
        compactor = ContextCompactor(
            uow_factory=lambda: FakeUnitOfWork(repository),
            llm=llm,
            agent_config=AgentConfig(
                max_retries=3,
                context_compact_threshold_tokens=30,
                context_hard_limit_tokens=35,
                context_keep_recent_tokens=10,
                context_summary_max_retries=2,
            ),
        )

        try:
            await compactor.compact_if_needed(
                session_id="session-1",
                agent_name="react",
                system_prompt="",
            )
        except ContextWindowLimitError as exc:
            assert "达到硬限制" in str(exc)
        else:
            raise AssertionError("达到硬限制且压缩失败时应终止请求")

        assert llm.calls == 2
        assert repository.summary is None
        assert repository.compacted_through == []

    asyncio.run(run_test())


def test_compaction_cas_conflict_does_not_mark_messages() -> None:
    async def run_test() -> None:
        repository = FakeAgentContextRepository(
            messages=[
                make_message(
                    message_id="old",
                    sequence=1,
                    role="user",
                    message={"role": "user", "content": "旧消息"},
                ),
                make_message(
                    message_id="recent",
                    sequence=2,
                    role="user",
                    message={"role": "user", "content": "最近消息"},
                ),
            ]
        )

        class RacingLLM(FakeLLM):
            async def invoke(
                self,
                messages: list[dict[str, Any]],
                tools: list[dict[str, Any]] | None = None,
                response_format: dict[str, Any] | None = None,
                tool_choice: str | None = None,
                max_tokens: int | None = None,
            ) -> dict[str, Any]:
                repository.summary = AgentContextSummary(
                    session_id="session-1",
                    agent_name="react",
                    summary="另一个任务生成的摘要",
                    summarized_through_sequence=1,
                    version=1,
                )
                return await super().invoke(
                    messages,
                    tools,
                    response_format,
                    tool_choice,
                    max_tokens,
                )

        compactor = ContextCompactor(
            uow_factory=lambda: FakeUnitOfWork(repository),
            llm=RacingLLM(),
            agent_config=AgentConfig(
                max_retries=2,
                context_compact_threshold_tokens=30,
                context_hard_limit_tokens=100,
                context_keep_recent_tokens=10,
            ),
        )

        compacted = await compactor.compact_if_needed(
            session_id="session-1",
            agent_name="react",
            system_prompt="",
        )

        assert compacted is True
        assert repository.summary.summary == "另一个任务生成的摘要"
        assert repository.compacted_through == []

    asyncio.run(run_test())


def test_model_token_counter_loads_local_tokenizer(tmp_path: Any) -> None:
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace

    tokenizer = Tokenizer(
        WordLevel(
            vocab={
                "[UNK]": 0,
                "hello": 1,
                "world": 2,
            },
            unk_token="[UNK]",
        )
    )
    tokenizer.pre_tokenizer = Whitespace()
    tokenizer_path = tmp_path / "tokenizer.json"
    tokenizer.save(str(tokenizer_path))

    counter = ModelTokenCounter(str(tokenizer_path))

    assert counter.count_text("hello world") == 2
