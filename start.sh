#!/bin/bash
set -e

GREEN="\033[0;32m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
NC="\033[0m"

echo -e "${GREEN}==> PeopleAgent 一键启动${NC}"

# 1. 检查 Docker
if ! command -v docker &> /dev/null; then
    echo -e "${RED}错误: 未检测到 Docker，请先安装 Docker Desktop${NC}"
    echo "   https://www.docker.com/products/docker-desktop/"
    exit 1
fi

if ! docker compose version &> /dev/null; then
    echo -e "${RED}错误: docker compose 不可用，请升级 Docker 到最新版本${NC}"
    exit 1
fi

# 2. 准备 .env（优先用已有的，没有则从 .env.example 复制）
if [ ! -f .env ]; then
    if [ -f api/.env.example ]; then
        echo -e "${YELLOW}==> .env 不存在，从 api/.env.example 复制...${NC}"
        cp api/.env.example .env
        echo ""
        echo -e "${GREEN}⚠️  请先编辑 .env 文件，填入 LLM API Key：${NC}"
        echo -e "   ${YELLOW}LLM_API_KEY=sk-your-real-key-here${NC}"
        echo -e "   ${YELLOW}修改完成后重新运行 bash start.sh${NC}"
        echo ""
        echo -e "   获取 API Key：https://api-docs.deepseek.com/zh-cn/"
        exit 0
    fi
fi

# 检查 LLM_API_KEY 是否已填写（非空且不是默认值）
LLM_KEY=$(grep -E "^LLM_API_KEY=" .env 2>/dev/null | cut -d'=' -f2- | tr -d '"'\''\"')
if [ -z "$LLM_KEY" ] || [ "$LLM_KEY" = "sk-your-key-here" ]; then
    echo ""
    echo -e "${YELLOW}⚠️  警告: LLM_API_KEY 未填写或使用默认值${NC}"
    echo -e "   ${YELLOW}请编辑 .env 文件，设置正确的 LLM_API_KEY${NC}"
    echo -e "   ${YELLOW}修改完成后重新运行 bash start.sh${NC}"
    echo ""
    read -p "已填写 LLM_API_KEY，按回车继续启动..."
fi

# 3. 构建镜像（--pull 保证 base image 最新）
echo -e "${GREEN}==> 构建 Docker 镜像（首次耗时较长，请耐心等待）...${NC}"
docker compose build --pull

# 4. 启动服务
echo -e "${GREEN}==> 启动所有服务...${NC}"
docker compose up -d

# 5. 等待服务健康
echo -e "${GREEN}==> 服务启动中，等待健康检测...${NC}"
echo -e "${YELLOW}    （首次启动需要运行数据库迁移，可能需要 1-2 分钟）${NC}"

# 最多等 120s
for i in $(seq 1 60); do
    HEALTHY=$(docker compose ps --format '{{.Service}} {{.Status}}' 2>/dev/null | grep -c "healthy" || true)
    TOTAL=$(docker compose ps --format '{{.Service}}' 2>/dev/null | wc -l | tr -d ' ')
    if [ "$HEALTHY" -ge 3 ] 2>/dev/null; then
        echo -e "${GREEN}==> 已有 $HEALTHY/$TOTAL 个服务健康，继续...${NC}"
        break
    fi
    printf "."
    sleep 2
done
echo ""

# 6. 完成提示
echo ""
echo -e "${GREEN}✅ 启动完成！${NC}"
echo ""
echo -e "   前端地址:   ${YELLOW}http://localhost:${NGINX_PORT:-8090}${NC}"
echo -e "   API 文档:   ${YELLOW}http://localhost:${NGINX_PORT:-8090}/api/docs${NC}"
echo ""
echo -e "${YELLOW}常用命令:${NC}"
echo "  查看日志:   docker compose logs -f"
echo "  停止服务:   bash stop.sh"
echo "  重启服务:   docker compose restart"
echo ""
