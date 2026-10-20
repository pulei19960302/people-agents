#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Agent 事件的接口层响应模型与映射器。

这个模块位于 interfaces 层，负责把 domain 层的事件转换成前端使用的
统一事件结构：

    Domain Event
        -> EventMapper.event_to_sse_event()
        -> {"event": "事件类型", "data": {事件数据}}

需要注意：这里的 ``SSEEvent`` 是“适合通过 SSE 发送的数据模型”，它本身
不负责建立连接、设置 ``text/event-stream`` 响应或写入 Redis。实时接口可以
逐条发送这些模型，历史时间线接口也可以把这些模型作为普通 JSON 列表返回。
这样实时事件和历史事件就可以复用同一套前端解析与渲染逻辑。

模型分为三层：

1. ``BaseEventData`` 及其子类：定义 ``data`` 内部的数据结构。
2. ``BaseSSEEvent`` 及其子类：定义顶层 ``event + data`` 结构。
3. ``EventMapper``：根据领域事件的 ``type`` 选择对应的 SSE 模型。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any, Self, Type, Literal, List, Union, get_args

from pydantic import BaseModel, Field, ConfigDict

from app.domain.models.event import Event, PlanEvent, ToolEventStatus, ToolEvent, StepEvent, MessageEvent
from app.domain.models.file import File
from app.domain.models.plan import ExecutionStatus


class BaseEventData(BaseModel):
    """所有前端事件 ``data`` 共有的字段。

    ``event_id`` 用于唯一标识一条业务事件，可用于前端去重以及历史分页数据和
    实时数据的合并。它不同于 ``session_events.id``：后者是数据库记录主键，
    前者是 Agent 在创建领域事件时生成的业务 ID。

    ``run_id`` 标识产生该事件的一次 Agent 任务运行。同一轮任务生成的消息、
    步骤、工具和终止事件拥有相同的 ``run_id``，前端可以据此进行归组。

    ``created_at`` 在 Python 模型内部仍然是 ``datetime``。模型序列化为 JSON
    时，下面的 ``json_encoders`` 会将其转换为 Unix 秒级时间戳，以保持现有
    前端协议不变。
    """

    event_id: Optional[str] = None
    run_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now)

    # 这里只影响 JSON 序列化结果，不会把模型实例中的 created_at 改成 int。
    # 例如 model_dump_json() 输出 1735689600，而 event_data.created_at 仍是 datetime。
    model_config = ConfigDict(json_encoders={
        datetime: lambda v: int(v.timestamp())
    })

    @classmethod
    def base_event_data(cls, event: Event) -> Dict[str, Any]:
        """提取所有事件共有的接口字段。

        具体事件的 ``from_event`` 可以展开这个字典，再补充自己的业务字段，
        从而避免每个映射器重复处理 ``id`` 和 ``created_at``。

        虽然这里先返回时间戳，Pydantic 在构造 ``created_at: datetime`` 时会将
        时间戳解析回 ``datetime``；最终输出 JSON 时再按 ``model_config`` 转为
        秒级时间戳。这保持了模型类型正确，也保持了对外协议稳定。
        """
        return {
            "event_id": event.id,
            "run_id": event.run_id,
            "created_at": int(event.created_at.timestamp()),
        }

    @classmethod
    def from_event(cls, event: Event) -> Self:
        """从领域事件创建对应的接口 ``data`` 模型。

        该通用实现会保留公共字段，并把领域事件剩余字段交给具体的 Data 模型
        校验。多数结构简单的事件可以直接复用它；字段需要重命名或重组的事件
        （例如 step、plan、tool）会在自己的 SSE 模型中重写转换逻辑。
        """
        return cls(
            **cls.base_event_data(event),
            **event.model_dump(mode="json", exclude={"id", "run_id", "type", "created_at"}),
        )


class BaseSSEEvent(BaseModel):
    """前端事件的统一外层结构。

    序列化后的形状固定为：

    ``{"event": "message", "data": {...}}``

    ``event`` 用来做事件分派；``data`` 存放公共字段和具体事件业务数据。
    """

    event: str
    data: BaseEventData

    @classmethod
    def from_event(cls, event: Event) -> Self:
        """使用当前 SSE 子类声明的 ``data`` 类型转换领域事件。

        例如调用 ``TitleSSEEvent.from_event(title_event)`` 时，方法会从
        ``TitleSSEEvent.__annotations__`` 读取 ``TitleEventData``，随后调用
        ``TitleEventData.from_event`` 完成载荷转换。

        如果子类没有重新声明 ``data``（例如 done、wait），就使用只有公共字段
        的 ``BaseEventData``。这些终止/等待事件不需要额外业务字段。
        """
        # 子类直接声明了 data 时使用子类类型；否则回退到 BaseEventData。
        data_class: Type[BaseEventData] | None = cls.__annotations__.get("data", BaseEventData)

        # event.type 同时也是 SSE 的事件名，data 由对应 Data 模型负责校验。
        return cls(
            event=event.type,
            data=data_class.from_event(event),
        )


class CommonEventData(BaseEventData):
    """未注册事件类型的通用载荷。

    ``extra="allow"`` 很重要：当系统以后新增一种领域事件、但尚未为它定义专用
    SSE Schema 时，事件独有字段不会被 Pydantic 丢弃。它是向前兼容的兜底，
    不是已知事件的首选模型。
    """

    model_config = ConfigDict(
        json_encoders={
            datetime: lambda v: int(v.timestamp()),
        },
        extra="allow",
    )


class CommonSSEEvent(BaseSSEEvent):
    """无法匹配专用 Schema 时返回的通用前端事件。"""

    # event 使用 str 而不是 Literal，因此它不会被自动注册进事件映射表。
    event: str
    data: CommonEventData


class MessageEventData(BaseEventData):
    """用户或 Assistant 的聊天消息载荷。"""

    # role 决定前端将消息渲染在用户侧还是 Assistant 侧。
    role: Literal["user", "assistant"] = "assistant"
    message: str = ""
    # 使用 default_factory，确保不同模型实例不会共享同一个可变列表。
    attachments: List[File] = Field(default_factory=list)


class MessageSSEEvent(BaseSSEEvent):
    """消息事件的前端响应结构。

    此处显式实现 ``from_event``，只暴露前端消息渲染真正需要的字段，避免把
    MessageEvent 的内部结构无差别透传到接口层。
    """

    event: Literal["message"] = "message"
    data: MessageEventData

    @classmethod
    def from_event(cls, event: MessageEvent) -> Self:
        return cls(
            data=MessageEventData(
                **BaseEventData.base_event_data(event),
                role=event.role,
                message=event.message,
                attachments=event.attachments,
            )
        )


class TitleEventData(BaseEventData):
    """会话标题更新事件的载荷。"""

    title: str


class TitleSSEEvent(BaseSSEEvent):
    """通知前端更新当前会话标题。"""

    event: Literal["title"] = "title"
    data: TitleEventData


class StepEventData(BaseEventData):
    """执行计划中单个步骤的展示数据。"""

    # 步骤 ID，不是事件 ID。同一步骤的多次状态更新应拥有相同的 id。
    id: str
    # 前端通过状态判断步骤处于等待、运行、完成或失败等阶段。
    status: ExecutionStatus
    description: str


class StepSSEEvent(BaseSSEEvent):
    """单个步骤创建或状态变化事件。

    领域模型把步骤信息放在 ``event.step`` 中；接口模型为了方便前端使用，
    将 ``step.id/status/description`` 扁平化到 ``data`` 下。
    """

    event: Literal["step"] = "step"
    data: StepEventData

    @classmethod
    def from_event(cls, event: StepEvent) -> Self:
        return cls(
            data=StepEventData(
                **BaseEventData.base_event_data(event),
                status=event.step.status,
                id=event.step.id,
                description=event.step.description
            )
        )


class PlanEventData(BaseEventData):
    """一份执行计划及其全部步骤的快照。"""

    steps: List[StepEventData]


class PlanSSEEvent(BaseSSEEvent):
    """计划创建或更新事件。

    每次 plan 事件携带当前完整步骤列表，前端因此可以直接用最新的 plan 事件
    替换计划面板，而不必自己合并增量步骤。
    """

    event: Literal["plan"] = "plan"
    data: PlanEventData

    @classmethod
    def from_event(cls, event: PlanEvent) -> Self:
        return cls(
            data=PlanEventData(
                **BaseEventData.base_event_data(event),
                steps=[
                    StepEventData(
                        # 当前数据协议中，每个计划步骤沿用 plan 事件的公共事件信息。
                        **BaseEventData.base_event_data(event),
                        id=step.id,
                        status=step.status,
                        description=step.description,
                    )
                    for step in event.plan.steps
                ]
            )
        )


class ToolEventData(BaseEventData):
    """一次工具调用的前端展示数据。

    同一个工具调用通常至少产生 calling 和 called 两条事件。两条事件使用相同
    的 ``tool_call_id``，前端可据此把“调用中”更新成“已完成”，而不是渲染成
    两个不同工具。
    """

    tool_call_id: str
    # 工具集合/提供方名称，例如 browser、shell 或某个 MCP server。
    name: str
    status: ToolEventStatus
    # 实际调用的函数名称。
    function: str
    # 大模型生成并传给工具函数的参数。
    args: Dict[str, Any]
    # 工具执行后的展示内容；calling 阶段通常为 None。
    content: Optional[Any] = None


class ToolSSEEvent(BaseSSEEvent):
    """工具调用状态变化事件。

    这里同时承担接口字段重命名：领域模型的 ``tool_name``、``function_name``、
    ``function_args``、``tool_content`` 分别映射为前端协议的 ``name``、
    ``function``、``args``、``content``。
    """

    event: Literal["tool"] = "tool"
    data: ToolEventData

    @classmethod
    def from_event(cls, event: ToolEvent) -> Self:
        return cls(
            data=ToolEventData(
                **BaseEventData.base_event_data(event),
                tool_call_id=event.tool_call_id,
                name=event.tool_name,
                status=event.status,
                function=event.function_name,
                args=event.function_args,
                content=event.tool_content,
            )
        )


class DoneSSEEvent(BaseSSEEvent):
    """本次 Agent 运行正常结束。

    它表示一轮任务结束，不表示会话记录被删除。前端收到后可以停止本轮 SSE
    读取并把运行状态切换为 completed。
    """

    event: Literal["done"] = "done"


class WaitSSEEvent(BaseSSEEvent):
    """Agent 暂停执行并等待用户继续输入。"""

    event: Literal["wait"] = "wait"


class ErrorEventData(BaseEventData):
    """最终需要展示给前端的任务错误信息。"""

    error: str


class ErrorSSEEvent(BaseSSEEvent):
    """本轮 Agent 运行以错误状态终止。"""

    event: Literal["error"] = "error"
    data: ErrorEventData


# 所有允许出现在接口响应中的事件联合类型。
# EventMapper 也会通过 get_args() 遍历该联合类型来自动建立注册表，因此新增一个
# 专用 SSE 事件时，除了定义模型，还必须把模型加入这个 Union。
AgentSSEEvent = Union[
    CommonSSEEvent,
    MessageSSEEvent,
    TitleSSEEvent,
    StepSSEEvent,
    PlanSSEEvent,
    ToolSSEEvent,
    DoneSSEEvent,
    ErrorSSEEvent,
    WaitSSEEvent,
]


@dataclass
class EventMapping:
    """一条事件类型注册信息。

    例如 ``event_type="tool"`` 对应 ``ToolSSEEvent`` 和 ``ToolEventData``。
    ``data_class`` 当前主要用于保存 Schema 元数据；实际转换入口仍然是
    ``sse_event_class.from_event``。
    """

    sse_event_class: Type[BaseSSEEvent]
    data_class: Type[BaseEventData]
    event_type: str


class EventMapper:
    """把领域事件分派给对应接口 Schema 的统一入口。

    映射关系不是手写字典，而是从 ``AgentSSEEvent`` 联合类型及每个模型的
    ``event: Literal[...]`` 声明中生成。例如 ``Literal["tool"]`` 会注册为：

    ``"tool" -> ToolSSEEvent``

    实时 SSE 与历史时间线接口都应调用这里，确保两种接口对同一领域事件产生
    完全一致的数据结构。
    """

    # 反射只需要执行一次；之后复用类级缓存，避免每发一条事件都重新扫描模型。
    _cache_mapping: Optional[Dict[str, EventMapping]] = None

    @staticmethod
    def _get_event_type_mapping() -> Dict[str, EventMapping]:
        """构建 ``领域事件 type -> SSE Schema`` 映射并缓存结果。

        只有直接声明为 ``Literal["..."]`` 的 event 字段才能自动注册。
        ``CommonSSEEvent.event`` 是普通 ``str``，所以不会进入映射表，它只作为
        ``event_to_sse_event`` 找不到专用类型时的兜底。
        """
        # 已构建过映射时直接复用。
        if EventMapper._cache_mapping is not None:
            return EventMapper._cache_mapping

        # Union 的每一个成员都是一种可返回的 SSE Schema。
        sse_event_classes = get_args(AgentSSEEvent)
        mapping = {}

        # 逐个读取 Schema 自己声明的 event 和 data 类型。
        for sse_event_class in sse_event_classes:
            # 当前 Union 中没有 BaseSSEEvent；保留此判断可防止以后误加入基类。
            if sse_event_class == BaseSSEEvent:
                continue

            # 使用 __annotations__ 只读取子类直接声明的字段，避免把基类的普通
            # event: str 当成某个具体事件类型注册。
            if hasattr(sse_event_class, "__annotations__") and "event" in sse_event_class.__annotations__:
                event_field = sse_event_class.__annotations__["event"]

                # Literal["tool"].__args__[0] 即字符串 "tool"。
                # 普通 str 没有可用的 __args__，因此 CommonSSEEvent 会自然跳过。
                if hasattr(event_field, "__args__") and len(event_field.__args__) > 0:
                    event_type = event_field.__args__[0]

                    # done/wait 没有直接声明 data，此时值为 None。当前转换过程不
                    # 使用 EventMapping.data_class，而由 BaseSSEEvent.from_event
                    # 自行回退到 BaseEventData。
                    data_class = None
                    if hasattr(sse_event_class, "__annotations__") and "data" in sse_event_class.__annotations__:
                        data_class = sse_event_class.__annotations__["data"]

                    # 同名 event 后注册的模型会覆盖前者，所以事件名应保持唯一。
                    mapping[event_type] = EventMapping(
                        sse_event_class=sse_event_class,
                        data_class=data_class,
                        event_type=event_type
                    )

        # 缓存完整映射表，后续事件转换直接按字符串查找。
        EventMapper._cache_mapping = mapping
        return mapping

    @staticmethod
    def event_to_sse_event(event: Event) -> AgentSSEEvent:
        """把单个领域事件转换成前端事件模型。

        已知类型使用专用 Schema，以获得明确字段和校验；未知类型使用
        ``CommonSSEEvent``，保留额外字段，使新增领域事件不会直接导致接口失败。
        """
        event_type_mapping = EventMapper._get_event_type_mapping()

        # 领域事件的 type 是分派键，例如 message、step、tool。
        event_mapping = event_type_mapping.get(event.type)

        # 由具体 SSE Schema 决定字段是否需要筛选、扁平化或重命名。
        if event_mapping:
            sse_event = event_mapping.sse_event_class.from_event(event)
            return sse_event

        # 未注册事件仍可输出，避免整个事件流因新类型中断。
        return CommonSSEEvent.from_event(event)

    @staticmethod
    def events_to_sse_events(events: List[Event]) -> List[AgentSSEEvent]:
        """批量转换事件，并保持输入列表的原始顺序。

        历史时间线接口可以直接使用这个方法转换一页领域事件。分页顺序和边界
        应在 Repository/Service 层处理，本方法只负责逐条协议转换。
        """
        return list(filter(lambda x: x is not None, [
            EventMapper.event_to_sse_event(event) for event in events
        ]))
