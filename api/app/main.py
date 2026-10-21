#!/usr/bin/env python
# -*- coding: utf-8 -*-
import asyncio
import logging
from contextlib import asynccontextmanager

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import os

from app.infrastructure.logging import setup_logging
from app.infrastructure.storage.cos import get_cos
from app.infrastructure.storage.postgres import get_postgres
from app.infrastructure.storage.redis import get_redis
from app.interfaces.endpoints.routes import router
from app.interfaces.errors.exception_handlers import register_exception_handlers
from app.interfaces.service_dependencies import get_agent_service
from core.config import get_settings

# 1.加载配置信息
settings = get_settings()

# 2.初始化日志系统
setup_logging()
logger = logging.getLogger()

# 3.定义FastAPI路由tags标签
openapi_tags = [
    {
        "name": "状态模块",
        "description": "包含 **状态监测** 等API 接口，用于监测系统的运行状态。"
    }
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """创建FastAPI应用生命周期上下文管理器"""
    # 0.重新初始化日志系统(uvicorn启动时dictConfig会影响根日志处理器，需要在此重新配置)
    setup_logging()

    # 1.日志打印代码已经开始执行了
    logger.info("PeopleAgent正在初始化")

    # 2.运行数据库迁移(将数据同步到生产环境)
    alembic_cfg = Config("alembic.ini")
    command.upgrade(alembic_cfg, "head")

    # Alembic 会读取 alembic.ini 并重设根日志等级；迁移结束后恢复应用运行期日志。
    setup_logging()

    # 3.初始化Redis/Postgres/Cos客户端
    await get_redis().init()
    await get_postgres().init()

    interrupted_count = (
        await get_agent_service().interrupt_orphaned_runs()
    )
    logger.info(
        "启动时标记了%s个失联任务为interrupted",
        interrupted_count,
    )


    # COS初始化可能失败（本地开发环境未配置），只记录警告
    try:
        await get_cos().init()
    except Exception as e:
        logger.warning(f"Cos腾讯云对象存储初始化失败（本地开发可忽略）: {str(e)}")

    try:
        # 4.lifespan分界点
        yield
    finally:
        try:
            # 5.等待agent服务关闭
            logger.info("PeopleAgent正在关闭")
            await asyncio.wait_for(get_agent_service().shutdown(), timeout=30.0)
            logger.info("Agent服务成功关闭")
        except asyncio.TimeoutError:
            logger.warning("Agent服务关闭超时, 强制关闭, 部分任务将被释放")
        except Exception as e:
            logger.error(f"Agent服务关闭期间出现错误: {str(e)}")

        # 6.关闭其他应用
        await get_redis().shutdown()
        await get_postgres().shutdown()
        await get_cos().shutdown()

        logger.info("Manus应用关闭成功")


# 4.创建PeopleAgent应用实例
app = FastAPI(
    title="PeopleAgent通用智能体",
    description="PeopleAgent是一个通用的AI Agent系统，可以完全私有部署，使用A2A+MCP连接Agent/Tool，同时支持在沙箱中运行各种内置工具和操作",
    lifespan=lifespan,
    openapi_tags=openapi_tags,
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# 5.配置CORS中间件，解决跨域问题
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 6.注册错误处理器
register_exception_handlers(app)

# 7.集成路由
app.include_router(router, prefix="/api")

# 8.挂载本地文件存储目录，使上传的文件可通过HTTP访问
_uploads_path = os.environ.get("FILE_STORAGE_PATH", "./uploads")
_uploads_dir = str(Path(_uploads_path).resolve())
try:
    app.mount("/uploads", StaticFiles(directory=_uploads_dir), name="uploads")
    logger.info(f"已挂载静态文件目录: {_uploads_dir} -> /uploads")
except Exception as e:
    logger.warning(f"挂载静态文件目录失败（目录可能不存在）: {str(e)}")
