#!/bin/bash
set -e

GREEN="\033[0;32m"
YELLOW="\033[1;33m"
NC="\033[0m"

echo -e "${GREEN}==> 停止 PeopleAgent 服务...${NC}"

# 停止并移除容器（保留数据卷）
docker compose down

echo ""
echo -e "${GREEN}✅ 服务已停止${NC}"
echo ""
echo -e "${YELLOW}注意：${NC}"
echo "  - 数据库数据已保留（volume: postgres_data, redis_data）"
echo "  - 如需完全清理数据，运行: docker compose down -v"
echo "  - 如需重新构建镜像，运行: docker compose build --no-cache"
echo ""
