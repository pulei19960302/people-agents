# 1.为 Ubuntu 极简镜像提供访问 HTTPS 软件源所需的 CA 证书
ARG DOCKER_IMAGE_REGISTRY=docker.1ms.run/library
FROM ${DOCKER_IMAGE_REGISTRY}/python:3.12-slim AS certificates

# 2.使用ubuntu基础镜像来源
FROM ${DOCKER_IMAGE_REGISTRY}/ubuntu:22.04
COPY --from=certificates /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt

# 3.安装过程避免交互式提示并设置主机名
ENV DEBIAN_FRONTEND=noninteractive
ENV HOSTNAME=sandbox

# 4.使用可覆盖的 HTTPS Ubuntu 镜像，避免网络代理拦截或单一镜像同步异常
ARG UBUNTU_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/ubuntu
RUN sed -i "s|http://archive.ubuntu.com/ubuntu|${UBUNTU_MIRROR}|g; s|http://security.ubuntu.com/ubuntu|${UBUNTU_MIRROR}|g" \
        /etc/apt/sources.list
RUN apt-get -o Acquire::Retries=5 update && apt-get install -y \
    sudo bc curl wget gnupg software-properties-common supervisor xz-utils \
    xterm socat xvfb x11vnc websockify\
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 5.创建用户ubuntu并赋予sudo权限
RUN useradd -m -d /home/ubuntu -s /bin/bash ubuntu && \
    echo "ubuntu ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/ubuntu

# 6.安装 Ubuntu 22.04 自带的 Python 3.10
RUN apt-get -o Acquire::Retries=5 update && \
    apt-get install -y python3.10 python3.10-venv python3.10-dev python3-pip && \
    update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.10 1 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 7.默认使用可稳定访问且同步完整的 PyPI 镜像，允许构建时覆盖
ARG PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip3 config set global.index-url "${PYPI_INDEX_URL}"

# 8.安装 Node.js 24 和 npm，避免依赖 NodeSource 仓库和 Docker Hub 镜像代理
ARG NODE_VERSION=24.1.0
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

# 9.配置 npm 软件包源
ARG NPM_REGISTRY=https://registry.npmjs.org
RUN npm config set registry "${NPM_REGISTRY}"

# 10.安装Chromium浏览器（通过Playwright，因为Ubuntu 22.04的chromium是snap过渡包，容器内无snap）
ARG PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
RUN pip3 install --no-cache-dir playwright==1.63.0 && \
    PLAYWRIGHT_DOWNLOAD_HOST="${PLAYWRIGHT_DOWNLOAD_HOST}" playwright install chromium && \
    apt-get -o Acquire::Retries=5 update && \
    apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libxkbcommon0 libatspi2.0-0 libgbm1 libpango-1.0-0 libcairo2 \
        libasound2 libdrm2 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 && \
    CHROME_PATH=$(find /root/.cache/ms-playwright -type f -name chrome | head -1) && \
    test -n "$CHROME_PATH" && \
    ln -sf "$CHROME_PATH" /usr/bin/chromium && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 11.安装中文字体和语言支持
RUN apt-get -o Acquire::Retries=5 update && apt-get install -y \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    language-pack-zh-hans \
    locales \
    && locale-gen zh_CN.UTF-8 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 12.设置默认编码
ENV LANG=zh_CN.UTF-8 \
    LANGUAGE=zh_CN:zh \
    LC_ALL=zh_CN.UTF-8

# 13.设置工作空间
WORKDIR /sandbox

# 14.拷贝项目依赖文件到工作空间并安装依赖
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# 15.拷贝当前整个项目文件
COPY . .

# 16.将supervisord.conf拷贝至supervisor服务配置目录中
COPY supervisord.conf /etc/supervisor/conf.d/sandbox.conf

# 17.暴露端口: 8080(FastAPI) 9222(CDP) 5900(vnc)
EXPOSE 8080 9222 5900 5901

# 18.额外的环境变量配置
ENV CHROME_ARGS=""
ENV UVI_ARGS=""

# 19.使用supervisor管理所有进程
CMD ["supervisord", "-n", "-c", "/sandbox/supervisord.conf"]
