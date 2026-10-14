#!/usr/bin/env python
# -*- coding: utf-8 -*-
from typing import Protocol, Any, Tuple


class MessageQueue(Protocol):
    """消息队列协议"""

    async def put(self, message: Any) -> str:
        """往消息队列中添加一条消息"""
        ...

    async def get(
            self,
            start_id: str | None = None,
            block_ms: int | None = None,
    ) -> Tuple[str | None, Any | None]:
        """根据传递的开始id+阻塞时间，获取1条数据"""
        ...

    async def pop(self) -> Tuple[str, Any]:
        """获取并移除消息队列中的第一条消息"""
        ...

    async def clear(self) -> None:
        """删除消息队列及其中的所有消息"""
        ...

    async def expire(self, seconds: int) -> bool:
        """设置消息队列的过期时间"""
        ...

    async def is_empty(self) -> bool:
        """判断消息队列是否为空"""
        ...

    async def size(self) -> int:
        """获取消息队列的长度"""
        ...

    async def delete_message(self, message_id: str) -> bool:
        """根据传递的消息id删除队列中指定的消息"""
