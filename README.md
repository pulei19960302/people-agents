# PeopleAgent - 通用 AI Agent 系统

PeopleAgent 是一个通用的 AI Agent 系统，支持完全私有化部署，使用 A2A + MCP 连接 Agent/Tool，同时支持在沙箱中运行各种内置工具和操作。

## ⚡ 快速启动（推荐）

> 适用场景：首次部署、本机开发、给他人演示。一条命令启动全部服务。

### 前置要求

- Docker Desktop >= 4.25（包含 Docker Compose V2）
- 磁盘空间 >= 5GB

### ⚠️ Docker 镜像源

项目默认通过 `docker.1ms.run/library` 拉取 Docker Hub 官方基础镜像，避免 Docker Hub
直连超时或单个镜像加速服务临时返回 502。无需额外修改 Docker Desktop 的
`registry-mirrors`。

如需使用其他镜像仓库，在项目根目录的 `.env` 中覆盖：

```bash
# 官方 Docker Hub（网络能够直连时使用）
DOCKER_IMAGE_REGISTRY=docker.io/library
```

修改后重新运行 `bash start.sh`。如果 Docker Desktop 中配置了已经失效的全局镜像
加速地址，建议同时从 Docker Engine 配置中移除并重启 Docker Desktop。

### 一键启动

```bash
# 克隆/解压代码后，在项目根目录执行
bash start.sh
```

脚本会自动完成：
1. 检查 Docker 环境
2. 从 `api/.env.example` 复制生成 `.env`（如不存在）
3. 构建所有 Docker 镜像（首次约 5-10 分钟）
4. 启动 PostgreSQL / Redis / API / UI / Nginx
5. 自动执行数据库表结构迁移

启动成功后访问：**http://localhost:8090**

### 配置 LLM API Key（必需）

一键启动前，编辑项目根目录的 `.env` 文件，填入 API Key：

```bash
LLM_API_KEY=sk-your-real-key-here
```

> 首次运行 `start.sh` 会自动从 `api/.env.example` 复制 `.env`，如果检测到 Key 未填写会提示你先编辑。

**支持哪些模型？** 默认连接 DeepSeek，修改 `api/config.yaml` 中的 `llm_config` 可切换其他 OpenAI 兼容模型：

```yaml
llm_config:
  base_url: https://api.deepseek.com/   # 或其他 OpenAI 兼容接口
  model_name: deepseek-chat              # 模型名称
  # api_key 不需要改，已通过 LLM_API_KEY 环境变量注入
```

### 停止服务

```bash
bash stop.sh
```

> 数据卷（PostgreSQL / Redis）不会被删除，下次 `start.sh` 数据仍在。

---

## 项目结构

```
people-agents/
├── api/              # 后端 API 服务（FastAPI）
├── ui/               # 前端服务（Next.js）
├── sandbox/          # 沙箱服务（Ubuntu + Chrome + VNC）
├── nginx/            # Nginx 网关配置
│   ├── nginx.conf
│   └── conf.d/
│       └── default.conf
├── docker-compose.yml
├── .env              # 环境变量配置（需自行创建）
└── README.md
```

## 🚀 线上部署

本地和线上使用同一套 Docker Compose 配置，流程完全一致。

### 部署步骤

1. **配置环境变量**

   ```bash
   # 复制配置模板
   cp api/.env.example .env

   # 编辑 .env，至少修改以下两项：
   LLM_API_KEY=sk-your-real-key-here    # 必填：大模型 API Key
   NGINX_PORT=8090                       # 对外端口，按需修改
   ```

2. **启动服务**

   ```bash
   bash start.sh
   # 或手动：docker compose up -d --build
   ```

3. **访问系统**

   浏览器打开 `http://your-server-ip:8090`（或你配置的端口）

### 线上注意事项

| 项目 | 说明 |
|------|------|
| **防火墙** | 开放 `NGINX_PORT` 端口（默认 8090） |
| **域名** | 将域名 A 记录指向服务器 IP |
| **HTTPS** | 见下方「启用 HTTPS」章节 |
| **数据持久化** | 默认使用 Docker volumes，`docker compose down` 不会丢数据 |
| **SSL 证书** | 推荐使用 [certbot](https://certbot.eff.org/) 免费申请 Let's Encrypt 证书 |

### 服务架构

```
                    ┌─────────────┐
     Port 8090      │   Nginx     │
   ─────────────────►  (Gateway)  │
                    └──────┬──────┘
                           │
              ┌────────────┴────────────┐
              │ /                       │ /api
              ▼                         ▼
       ┌─────────────┐          ┌─────────────┐
       │  Next.js UI │          │  FastAPI     │
       │  (Port 3000)│          │  (Port 8000) │
       └─────────────┘          └──────┬──────┘
                                       │
                    ┌──────────────────┼──────────────────┐
                    │                  │                   │
                    ▼                  ▼                   ▼
             ┌───────────┐     ┌───────────┐       ┌───────────┐
             │ PostgreSQL│     │   Redis   │       │  Sandbox  │
             │(Port 5432)│     │(Port 6379)│       │ (VNC/HTTP)│
             └───────────┘     └───────────┘       └───────────┘
```

### 容器列表

| 容器名称 | 服务 | 说明 |
|---------|------|------|
| people-agent-nginx | Nginx | 反向代理网关，唯一对外暴露端口 |
| people-agent-ui | Next.js | 前端 UI 服务 |
| people-agent-api | FastAPI | 后端 API 服务 |
| people-agent-postgres | PostgreSQL | 数据库 |
| people-agent-redis | Redis | 缓存 |
| people-agent-sandbox | Sandbox | 沙箱环境（Chrome + VNC） |

### 常用命令

```bash
# 启动所有服务（后台运行）
docker compose up -d --build

# 查看所有服务状态
docker compose ps

# 查看服务日志
docker compose logs -f                      # 所有服务
docker compose logs -f people-agent-api     # 仅 API 服务
docker compose logs -f people-agent-ui      # 仅 UI 服务

# 重启单个服务
docker compose restart people-agent-api

# 停止所有服务
docker compose down

# 停止并清除数据卷（谨慎操作）
docker compose down -v
```

### 启用 HTTPS

1. 将 SSL 证书放入 `nginx/ssl/` 目录：
   - `fullchain.pem`（证书链）
   - `privkey.pem`（私钥）

2. 修改 `nginx/conf.d/default.conf`，取消 SSL server 块注释

3. 修改 `docker-compose.yml`，取消 443 端口映射注释

4. 重启 Nginx：
   ```bash
   docker compose restart people-agent-nginx
   ```

## 本地开发

各子项目的本地开发说明请参考对应目录下的 README：

- [API 服务](./api/README.md)
- [前端 UI](./ui/README.md)
- [沙箱服务](./sandbox/README.md)
