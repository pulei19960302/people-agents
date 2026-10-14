#!/bin/bash
set -e

echo "==> 运行数据库迁移..."
alembic upgrade head
echo "==> 迁移完成"

echo "==> 启动 API 服务..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 5
