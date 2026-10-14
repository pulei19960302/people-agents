# 1.确认基础镜像源（可通过构建参数切换为 docker.io/library）
ARG DOCKER_IMAGE_REGISTRY=docker.1ms.run/library
FROM ${DOCKER_IMAGE_REGISTRY}/python:3.12-slim

# 2.配置容器工作空间
WORKDIR /app

# 3.通过稳定的 HTTPS 镜像安装系统依赖
ARG DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian
ARG DEBIAN_SECURITY_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian-security
RUN sed -i "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g; \
            s|https://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g; \
            s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g; \
            s|https://deb.debian.org/debian|${DEBIAN_MIRROR}|g; \
            s|http://security.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g; \
            s|https://security.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
        /etc/apt/sources.list.d/*.sources /etc/apt/sources.list 2>/dev/null || true
RUN apt-get -o Acquire::Retries=5 update && \
    apt-get install -y --no-install-recommends curl xz-utils && \
    rm -rf /var/lib/apt/lists/*

# 4.安装 Node.js 22，避免依赖 NodeSource 仓库
ARG NODE_VERSION=22.16.0
ARG NODE_DIST_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/nodejs-release
ARG TARGETARCH
RUN case "${TARGETARCH}" in \
        amd64) NODE_ARCH=x64 ;; \
        arm64) NODE_ARCH=arm64 ;; \
        *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac && \
    NODE_PACKAGE="node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz" && \
    curl -fsSLO --retry 5 --retry-all-errors \
        "${NODE_DIST_MIRROR}/v${NODE_VERSION}/${NODE_PACKAGE}" && \
    curl -fsSLO --retry 5 --retry-all-errors \
        "${NODE_DIST_MIRROR}/v${NODE_VERSION}/SHASUMS256.txt" && \
    grep " ${NODE_PACKAGE}$" SHASUMS256.txt | sha256sum -c - && \
    tar -xJf "${NODE_PACKAGE}" -C /usr/local --strip-components=1 && \
    rm -f "${NODE_PACKAGE}" SHASUMS256.txt && \
    node --version && npm --version

# 5.安装uv, 使用uv来同步依赖环境
ARG PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir --index-url "${PYPI_INDEX_URL}" uv

# 6.默认使用可稳定访问且同步完整的 PyPI 镜像
ENV UV_INDEX_URL=${PYPI_INDEX_URL}

# 7.拷贝requirements.txt并安装依赖
COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

# 8.拷贝项目文件(忽略开发环境的库)
COPY . .

# 9.为运行脚本配置权限
RUN chmod +x entrypoint.sh run.sh

# 10.设置Python路径变量
ENV PYTHONPATH=/app

# 10.1 禁用Python输出缓冲，确保日志实时输出到docker logs
ENV PYTHONUNBUFFERED=1

# 11.暴露端口号
EXPOSE 8000

# 12.启动命令脚本
ENTRYPOINT ["./entrypoint.sh"]
