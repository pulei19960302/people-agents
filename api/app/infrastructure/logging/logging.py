#!/usr/bin/env python
# -*- coding: utf-8 -*-
import logging
import sys

from core.config import get_settings


# 这些第三方库在 DEBUG 下会输出完整 SSE 内容、请求体或底层网络帧，
# 既会刷屏，也可能把用户对话写入容器日志。应用自身的 logger 不受影响。
QUIET_LOGGERS = (
    "sse_starlette.sse",
    "sqlalchemy.engine",
    "httpcore",
    "httpcore2",
    "httpx",
    "httpx2",
    "mcp.client",
)


def setup_logging():
    """配置PeopleAgent项目的日志系统，涵盖日志等级、输出格式、输出渠道等"""
    # 1.获取项目配置
    settings = get_settings()

    # 2.获取根日志处理器
    root_logger = logging.getLogger()

    # 3.清除已有的handlers，避免uvicorn的dictConfig重配置后产生冲突或重复
    root_logger.handlers.clear()

    # 4.设置根日志处理器等级
    log_level = getattr(logging, settings.log_level)
    root_logger.setLevel(log_level)

    # 5.日志输出格式定义
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # 6.创建控制台日志输出处理器(使用stderr，stderr在Python中始终无缓冲，Docker中更可靠)
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(log_level)

    # 7.将控制台日志处理器添加到根日志处理器中
    root_logger.addHandler(console_handler)

    # 8.第三方底层日志只保留警告和错误，避免 DEBUG 输出完整业务数据。
    for logger_name in QUIET_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    root_logger.info("日志系统初始化完成")
