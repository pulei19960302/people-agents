#!/usr/bin/env python
# -*- coding: utf-8 -*-
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """沙箱API服务基础配置信息"""
    log_level: str = "INFO"  # 日志等级
    # 固定Compose沙箱默认常驻；动态沙箱由API通过环境变量传入TTL。
    server_timeout_minutes: int | None = None

    # 使用pydantic v2提供的写法完成环境变量信息的声明
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache()
def get_settings() -> Settings:
    return Settings()
