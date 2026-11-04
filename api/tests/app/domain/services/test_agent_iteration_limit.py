"""验证最后一轮仍能交付结果，同时不执行超额工具。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.app_config import AgentConfig
from app.domain.models.event import ErrorEvent, MessageEvent
from app.domain.models.tool_result import ToolResult
from app.domain.services.agents.base import BaseAgent
from app.infrastructure.external.json_parser.repair_json_parser import RepairJSONParser


def tool_message(call_id: str):
    return {"role": "assistant", "tool_calls": [{
        "id": call_id, "type": "function",
        "function": {"name": "demo", "arguments": "{}"},
    }]}


def make_agent(responses, iterations=1):
    agent = object.__new__(BaseAgent)
    agent._agent_config = AgentConfig(max_iterations=iterations)
    agent._json_parser = RepairJSONParser()
    agent._format = "json_object"
    agent._tool_budget_exhausted = AsyncMock(return_value=False)
    agent._get_tool = lambda _name: SimpleNamespace(name="demo")
    agent._invoke_tool = AsyncMock(return_value=ToolResult(success=True, message="已取得资料"))
    agent._invoke_llm = AsyncMock(side_effect=responses)
    return agent


@pytest.mark.parametrize("iterations", [1, 2])
def test_final_iteration_delivers_result_without_terminal_error(iterations):
    async def check():
        final = '{"success":false,"result":"部分资料无法核实","attachments":[]}'
        agent = make_agent([
            *[tool_message(f"call-{i}") for i in range(iterations)],
            {"role": "assistant", "content": final},
        ], iterations)
        events = [event async for event in agent.invoke("执行步骤")]
        assert not any(isinstance(event, ErrorEvent) for event in events)
        assert isinstance(events[-1], MessageEvent)
        assert events[-1].message == final
        assert agent._invoke_tool.await_count == iterations
        assert agent._invoke_llm.await_count == iterations + 1
        last_request = agent._invoke_llm.await_args_list[-1]
        assert last_request.kwargs["tools_enabled"] is False
        assert last_request.args[1] == "json_object"
        assert last_request.args[0][0]["role"] == "tool"
        assert last_request.args[0][-1]["role"] == "user"
    asyncio.run(check())


def test_model_cannot_execute_another_tool_during_final_response():
    async def check():
        agent = make_agent([tool_message("first"), tool_message("must-not-run")])
        events = [event async for event in agent.invoke("执行步骤")]
        agent._invoke_tool.assert_awaited_once()
        assert isinstance(events[-1], ErrorEvent)
        assert not any(isinstance(event, MessageEvent) for event in events)
    asyncio.run(check())


def test_early_answer_does_not_trigger_extra_finalization_request():
    async def check():
        agent = make_agent([{"role": "assistant", "content": "直接回答"}], iterations=2)
        events = [event async for event in agent.invoke("问答")]
        assert len(events) == 1 and isinstance(events[0], MessageEvent)
        agent._invoke_llm.assert_awaited_once()
        agent._invoke_tool.assert_not_awaited()
    asyncio.run(check())
