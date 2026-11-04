"""只覆盖本次恢复失败涉及的输出校验与JSON模式，不访问真实LLM。"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.domain.models.app_config import AgentConfig
from app.domain.models.event import MessageEvent, ErrorEvent, StepEvent, StepEventStatus
from app.domain.models.message import Message
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.models.tool_result import ToolResult
from app.domain.services.agents.base import BaseAgent
from app.domain.services.agents.react import ReActAgent
from app.infrastructure.external.json_parser.repair_json_parser import RepairJSONParser


def make_react():
    agent = object.__new__(ReActAgent)
    agent._json_parser = RepairJSONParser()
    return agent


def test_extra_status_cannot_override_step_or_hide_partial_result():
    async def check():
        agent = make_react()
        agent._invoke_llm = AsyncMock(side_effect=AssertionError("合规业务字段无需重新调用模型"))

        async def invoke(_query):
            yield MessageEvent(message=json.dumps({
                "status": "incomplete_partial", "id": "forged",
                "success": False, "result": "已取得部分资料，正文仍未核实", "attachments": [],
            }, ensure_ascii=False))

        agent.invoke = invoke
        step = Step(id="original")
        events = [event async for event in agent.execute_step(Plan(), step, Message(message="继续"))]
        assert step.id == "original"
        assert step.status == ExecutionStatus.COMPLETED
        assert step.success is False
        assert events[-1].message == "已取得部分资料，正文仍未核实"
        agent._invoke_llm.assert_not_awaited()

    asyncio.run(check())


def test_missing_result_fields_get_one_tool_free_correction():
    async def check():
        agent = make_react()
        agent._invoke_llm = AsyncMock(return_value={
            "role": "assistant", "content": json.dumps({
                "success": False, "result": "已有候选，但未完成核实", "attachments": [],
            }),
        })
        result = await agent._parse_step_result('```json\n{"status":"incomplete_partial","summary":"已有候选"}\n```')
        assert result.success is False
        assert result.result == "已有候选，但未完成核实"
        agent._invoke_llm.assert_awaited_once()
        assert agent._invoke_llm.call_args.kwargs["tools_enabled"] is False
        assert agent._invoke_llm.call_args.args[1] == "json_object"

    asyncio.run(check())


@pytest.mark.parametrize("response, expected_error", [
    ({"role": "assistant", "content": '{"status":"incomplete_partial"}'}, ValidationError),
    ({"role": "assistant", "content": "", "tool_calls": [{"id": "unexpected"}]}, ValueError),
    ({"role": "assistant", "content": '{"success":"false","result":"partial","attachments":[]}'}, ValidationError),
])
def test_invalid_correction_stops_after_one_attempt(response, expected_error):
    async def check():
        agent = make_react()
        agent._invoke_llm = AsyncMock(return_value=response)
        with pytest.raises(expected_error):
            await agent._parse_step_result('{"status":"incomplete_partial"}')
        agent._invoke_llm.assert_awaited_once()

    asyncio.run(check())


def test_json_mode_is_kept_after_tool_response():
    async def check():
        agent = object.__new__(BaseAgent)
        agent._format, agent._agent_config = "json_object", AgentConfig()
        agent._json_parser = RepairJSONParser()
        agent._tool_budget_exhausted = AsyncMock(return_value=False)
        agent._get_tool = lambda _name: SimpleNamespace(name="demo")
        agent._invoke_tool = AsyncMock(return_value=ToolResult(success=True))
        agent._invoke_llm = AsyncMock(side_effect=[
            {"role": "assistant", "tool_calls": [{"id": "call-1", "function": {
                "name": "demo", "arguments": "{}",
            }}]},
            {"role": "assistant", "content": '{"success":true,"result":"ok","attachments":[]}'},
        ])
        events = [event async for event in agent.invoke("执行")]
        assert isinstance(events[-1], MessageEvent)
        assert [call.args[1] for call in agent._invoke_llm.await_args_list] == ["json_object", "json_object"]
        agent._invoke_tool.assert_awaited_once()

    asyncio.run(check())


def test_correction_request_does_not_expose_tools_even_with_budget_left():
    async def check():
        agent = make_react()
        agent._agent_config = AgentConfig()
        agent._add_to_memory = AsyncMock()
        agent._compact_if_needed = AsyncMock()
        agent._tool_budget_exhausted = AsyncMock(return_value=False)
        agent._get_available_tools = lambda: [{"type": "function", "function": {"name": "must_not_run"}}]
        agent._memory = type("Memory", (), {"get_messages": lambda self: []})()
        agent._llm = type("LLM", (), {"invoke": AsyncMock(return_value={
            "role": "assistant", "content": '{"success":false,"result":"partial","attachments":[]}',
        })})()
        await agent._invoke_llm([], "json_object", tools_enabled=False)
        request = agent._llm.invoke.call_args.kwargs
        assert request["tools"] == []
        assert request["response_format"] == {"type": "json_object"}
        assert request["tool_choice"] is None

    asyncio.run(check())


def test_terminal_error_does_not_fall_through_to_completed_step():
    async def check():
        agent = make_react()
        async def invoke(_query):
            yield ErrorEvent(error="工具执行异常")
            yield MessageEvent(message="不应消费错误后的消息")
        agent.invoke = invoke
        step = Step(id="original", error="上次错误")
        events = [event async for event in agent.execute_step(Plan(), step, Message(message="继续"))]
        assert isinstance(events[-1], ErrorEvent)
        assert step.status == ExecutionStatus.FAILED
        assert step.error == "工具执行异常"
        assert not any(isinstance(e, MessageEvent) for e in events)
        assert any(isinstance(e, StepEvent) and e.status == StepEventStatus.FAILED for e in events)
    asyncio.run(check())
