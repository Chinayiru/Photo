
---

## 4. 云端部署（Docker Compose）

> **前置条件**：§2 云服务器已完成（ufw/fail2ban/SSH 加固）、§3 域名解析**已生效**。
> **本章目标**：`https://你的域名` 打开能返回 200，Let's Encrypt 证书自动签发。

### 4.1 安装 Docker 与 Docker Compose

用 Docker 官方脚本安装（比 `apt install docker.io` 版本新、Compose 插件自带）。

```bash
# 1. 安装依赖
sudo apt update
sudo apt install -y ca-certificates curl gnupg

# 2. 添加 Docker 官方 GPG 密钥
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
  sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# 3. 添加 Docker 软件源
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu \
$(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# 4. 安装 Docker Engine + Compose 插件
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

**验证安装**：

```bash
# 看 Docker 版本（应该 ≥ 26）
docker --version

# 看 Compose 版本（注意是 docker compose，不是 docker-compose）
docker compose version

# 跑一个测试容器
sudo docker run --rm hello-world
```

**预期输出**：

```
Docker version 27.3.1, build ce12230
Docker Compose version v2.30.3
...
Hello from Docker!
This message shows that your installation appears to be working correctly.
```

#### 4.1.1 让 deploy 用户免 sudo 用 Docker

```bash
# 把 deploy 加入 docker 组
sudo usermod -aG docker deploy

# ★ 必须重新登录才生效
exit
```

重新 SSH 登录后验证：

```bash
# 不加 sudo 应该也能跑
docker ps
```

| 结果 | 说明 |
|---|---|
| 输出空表格（表头 CONTAINER ID / IMAGE ...） | ✅ 成功 |
| `permission denied while trying to connect to the Docker daemon socket` | ❌ 组没生效，`exit` 后重新登录，或直接 `newgrp docker` |

> ⚠️ **安全提示**：加入 `docker` 组等价于获得 root 权限（可以挂载宿主机根目录）。
> 在本项目"单人单机"场景下可以接受；如果你以后要给别人开账号，需要重新评估。

#### 4.1.2 配置 Docker 日志轮转（★ 重要，防止磁盘写满）

Docker 容器日志**默认无限增长**，小硬盘 VPS 上很容易被日志撑满（§10 场景 7）。

```bash
sudo tee /etc/docker/daemon.json > /dev/null <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
EOF

# 重启 Docker 使配置生效
sudo systemctl restart docker

# 验证配置已加载
sudo docker info --format '{{.LoggingDriver}}'
```

**预期输出**：`json-file`

> 这条配置的含义：每个容器日志最多 10 MB，最多保留 3 个文件（即单容器最多占 30 MB）。
> **不做这一步，运行几个月后日志可能占满整个 20 GB 硬盘。**

### 4.2 目录结构

```bash
# 创建部署目录
sudo mkdir -p /opt/photobridge-cloud
sudo chown -R deploy:deploy /opt/photobridge-cloud
cd /opt/photobridge-cloud

# 创建子目录
mkdir -p data caddy_data caddy_config
```

**最终目录结构**：

```
/opt/photobridge-cloud/
├── docker-compose.yml      # 服务编排（relay + caddy）
├── Caddyfile               # Caddy 配置（自动 HTTPS + 反代）
├── .env                    # 环境变量（★ 含密钥，权限 600）
├── relay/                  # relay 源码（如果用 build 方式）
│   ├── Dockerfile
│   ├── main.py
│   ├── auth.py
│   ├── devices.py
│   ├── pairing.py
│   ├── tunnel.py
│   ├── protocol.py
│   └── requirements.txt
├── data/                   # relay 数据（cloud.db 在这里）
│   └── cloud.db
├── caddy_data/             # ★ 证书在这里，别删！
└── caddy_config/           # Caddy 运行时配置
```

| 目录 | 内容 | 备份 | 说明 |
|---|---|---|---|
| `data/` | `cloud.db`（账号/设备/配对码/流量） | ✅ **必须备份** | 丢了要重新注册所有设备、手机要重新配对 |
| `caddy_data/` | Let's Encrypt 证书与私钥 | ⚠️ 建议备份 | 丢了会自动重新签发，但**频繁重签会触发 Let's Encrypt 限流** |
| `caddy_config/` | Caddy 运行时状态 | ❌ 可重建 | — |
| `relay/` | 源码 | ❌ 可重建 | 从代码仓库重新拉取 |

### 4.3 docker-compose.yml（完整，可直接用）

创建文件：

```bash
cd /opt/photobridge-cloud
nano docker-compose.yml
```

**内容**（完整复制）：

```yaml
# PhotoBridge 云端编排：relay（隧道中继）+ caddy（HTTPS 终止）
# 部署位置：/opt/photobridge-cloud/
# 用法：docker compose up -d

services:
  # ── 隧道中继服务 ────────────────────────────────────────────────
  relay:
    build:
      context: ./relay
      dockerfile: Dockerfile
    # 或者用预构建镜像（二选一）：
    # image: photobridge/relay:0.1
    container_name: pb-relay
    restart: unless-stopped
    env_file:
      - .env
    environment:
      # 容器内数据目录
      DATA_DIR: /data
      # 只监听 Docker 内部网络，不对外
      HOST: 0.0.0.0
      PORT: 8000
    volumes:
      # 数据持久化（cloud.db）
      - ./data:/data
      # 源码挂载（开发期方便改；生产可注释掉）
      # - ./relay:/app
    # ★★ 关键：用 expose 而不是 ports ★★
    # ports 会让 Docker 直接写 iptables 绕过 ufw，把 8000 暴露到公网
    # expose 只在 Docker 内部网络可见，只有 caddy 能访问
    expose:
      - "8000"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    networks:
      - pb-net

  # ── Caddy：自动 HTTPS + 反向代理 ─────────────────────────────────
  caddy:
    image: caddy:2
    container_name: pb-caddy
    restart: unless-stopped
    # Caddy 是唯一对外暴露端口的服务
    ports:
      - "80:80"        # HTTP（Let's Encrypt 验证 + 跳转 HTTPS）
      - "443:443"      # HTTPS
      - "443:443/udp"  # HTTP/3 (QUIC)，可选但推荐
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - ./caddy_data:/data          # ★ 证书存这里
      - ./caddy_config:/config
      - ./web:/srv/web:ro           # 前端静态产物（§7.3）
    environment:
      # 让 Caddyfile 能用 {$DOMAIN} 引用
      DOMAIN: ${DOMAIN}
    depends_on:
      - relay
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    networks:
      - pb-net

networks:
  pb-net:
    driver: bridge
```

**几个必须理解的点**：

| 配置 | 为什么这么写 |
|---|---|
| `relay` 用 `expose` 不用 `ports` | Docker 写 iptables 会**绕过 ufw**。用 `ports` 会把 8000 暴露到公网（§2.8.2） |
| `restart: unless-stopped` | 服务器重启、容器崩溃后自动拉起。**没有这条，重启后服务不会自己起来** |
| `env_file: .env` | 密钥不写在 compose 文件里（[00](00-文档索引与开发流程.md) §3.3 硬约束④） |
| `logging` 限制 | 防止日志撑满磁盘（§4.1.2） |
| `./caddy_data:/data` | 证书持久化。**不挂这个卷，每次重建容器都会重新申请证书，很快触发限流** |
| `depends_on: relay` | Caddy 在 relay 之后启动（注意：`depends_on` 只保证启动顺序，不保证 relay 已就绪） |
| `healthcheck` | 让 Docker 能判断 relay 是否真的活着（`docker compose ps` 会显示 healthy） |

### 4.4 Caddyfile（完整，含 WebSocket 转发的正确配置）

创建文件：

```bash
cd /opt/photobridge-cloud
nano Caddyfile
```

**内容**（完整复制，★ **第一行改成你的域名**）：

```caddyfile
# PhotoBridge Caddyfile
# 自动申请并续期 Let's Encrypt 证书
# 部署位置：/opt/photobridge-cloud/Caddyfile

# ★ 把下面这行换成你的真实域名（不要带 https://）
photos.example.com {

    # ── 编码 ────────────────────────────────────────────────────
    encode zstd gzip

    # ── 安全响应头 ──────────────────────────────────────────────
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        X-Frame-Options "SAMEORIGIN"
        Referrer-Policy "strict-origin-when-cross-origin"
        -Server
    }

    # ── ★★ WebSocket 隧道（必须写在前面）★★ ──────────────────────
    # 电脑端 Agent 反向连到这里，走 WSS 长连接
    #
    # Caddy 的 reverse_proxy 会自动识别 Upgrade: websocket 并升级连接，
    # 不需要像 Nginx 那样手写 proxy_set_header Upgrade/Connection。
    # 但有三个要点必须处理：
    #   1. 路径必须精确匹配 /agent/ws，且这条规则要在通配规则之前
    #   2. 必须关闭响应缓冲（flush_interval -1），否则帧被攒着不发
    #   3. 必须放宽超时，否则长连接会被 Caddy 主动断开
    handle /agent/ws {
        reverse_proxy relay:8000 {
            # 关闭响应缓冲：WebSocket 帧立即转发，不攒批
            flush_interval -1

            # 超时设置：长连接不能被超时杀掉
            transport http {
                read_timeout 0
                write_timeout 0
                dial_timeout 10s
            }

            # 透传真实客户端 IP（relay 记日志用）
            header_up X-Real-IP {remote_host}
            header_up X-Forwarded-For {remote_host}
            header_up X-Forwarded-Proto {scheme}

            # 禁用 Caddy 自己的重试（WebSocket 不能重试）
            lb_try_duration 0
        }
    }

    # ── 手机端 / 浏览器 API（转发到 relay）───────────────────────
    handle /d/* {
        reverse_proxy relay:8000 {
            flush_interval -1
            transport http {
                # 上传大文件需要长超时
                read_timeout 0
                write_timeout 0
                dial_timeout 10s
            }
            header_up X-Real-IP {remote_host}
            header_up X-Forwarded-Proto {scheme}
        }
    }

    # ── 云端自身 API（登录、设备列表、配对）──────────────────────
    handle /api/* {
        reverse_proxy relay:8000 {
            flush_interval -1
            transport http {
                read_timeout 0
                write_timeout 0
                dial_timeout 10s
            }
            header_up X-Real-IP {remote_host}
            header_up X-Forwarded-Proto {scheme}
        }
    }

    # ── 健康检查（给监控脚本用）─────────────────────────────────
    handle /healthz {
        reverse_proxy relay:8000
    }

    # ── 前端静态资源（PWA 产物，见 §7.3）────────────────────────
    # 如果前端由 relay 托管，把这段注释掉，让默认规则转发到 relay
    handle {
        root * /srv/web
        try_files {path} /index.html
        file_server
    }

    # ── 日志 ────────────────────────────────────────────────────
    log {
        output file /data/access.log {
            roll_size 10mb
            roll_keep 5
        }
        format console
    }
}
```

#### 4.4.1 ★ Caddy 转发 WebSocket 的三个要点（重点）

很多人从 Nginx 换到 Caddy 时会困惑，这里说清楚：

| # | 要点 | 说明 |
|---|---|---|
| **1** | **Caddy 自动处理 Upgrade 头** | Nginx 需要手写 `proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade";`。**Caddy 的 `reverse_proxy` 自动检测并升级**，不需要写。如果照抄 Nginx 配置反而可能出错 |
| **2** | **必须关响应缓冲** | 默认 Caddy 会缓冲响应。WebSocket 是长连接、小帧，被缓冲会导致**消息延迟或卡住**。用 `flush_interval -1` 关闭 |
| **3** | **必须放宽超时** | 默认 `read_timeout`/`write_timeout` 有值，长连接空闲时会被断开。隧道有心跳（25 秒 PING），但网络抖动时可能超时。设为 `0`（不超时）最稳 |

**其他容易踩的坑**：

| 坑 | 现象 | 解决 |
|---|---|---|
| `handle /agent/ws` 写在 `handle` 通配规则之后 | WebSocket 请求被静态文件规则截走，返回 404 或 index.html | **精确路径的 handle 必须写在前面**（Caddy 的 handle 是互斥匹配，第一个匹配的生效） |
| 用了 `handle_path` 而不是 `handle` | 路径前缀被剥掉，relay 收到 `/ws` 而不是 `/agent/ws` | 用 `handle`（保留完整路径） |
| 忘记 `relay:8000` 用**服务名** | `dial tcp: lookup relay: connection refused` | compose 里服务名是 `relay`，同一 network 内用服务名互相访问 |
| Caddy 用了 Cloudflare 代理 | WebSocket 连不上或频繁断 | Cloudflare 免费版 WebSocket 有 100 秒空闲超时。见 §10 场景 6 |
| 443/udp 没开 | HTTP/3 不可用（会回退到 HTTP/2，不影响功能） | `ufw allow 443/udp` |

#### 4.4.2 验证 Caddyfile 语法

```bash
cd /opt/photobridge-cloud

# 用 caddy 容器校验配置（不启动服务）
docker run --rm -v ./Caddyfile:/etc/caddy/Caddyfile:ro caddy:2 caddy validate --config /etc/caddy/Caddyfile
```

**预期输出**：

```
Valid configuration
```

如果报错，根据错误信息修正后再继续。

### 4.5 Dockerfile

创建文件：

```bash
mkdir -p /opt/photobridge-cloud/relay
cd /opt/photobridge-cloud/relay
nano Dockerfile
```

**内容**：

```dockerfile
# PhotoBridge 云端 Relay
# 位置：/opt/photobridge-cloud/relay/Dockerfile
# 构建：docker compose build

FROM python:3.12-slim

# 环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 先装依赖（利用 Docker 层缓存：requirements.txt 不变就不重装）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷源码（改代码不会让依赖层失效）
COPY . .

# 数据目录（会被 compose 的卷覆盖）
RUN mkdir -p /data

# 以非 root 用户运行（★ 安全）
RUN useradd -m -u 1000 relay && chown -R relay:relay /app /data
USER relay

EXPOSE 8000

# 健康检查（与 compose 里的 healthcheck 二选一即可）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

# 启动
# ★ 注意：生产环境不要加 --reload
# ★ 单 worker：隧道连接是有状态的，多 worker 会导致客户端连到不同进程
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
```

**关键点说明**：

| 点 | 为什么 |
|---|---|
| `python:3.12-slim` | 与电脑端 Python 3.12 保持一致，`protocol.py` 共享不会有语法差异 |
| `COPY requirements.txt` 先于 `COPY . .` | 利用 Docker 层缓存，改代码时不用重装依赖（构建从 3 分钟降到 5 秒） |
| `USER relay`（非 root） | 容器被攻破时不能直接拿到宿主机 root |
| `--workers 1` | ★ **隧道是有状态的**：Agent 的 WSS 连接绑定在某个进程上。多 worker 会导致 HTTP 请求被路由到另一个进程，找不到那条隧道。**这是最容易踩的坑** |
| `TZ=Asia/Shanghai` | 日志时间与服务器时区一致，排查方便 |

**requirements.txt 示例**（实际以设计文档 §11 为准）：

```bash
cd /opt/photobridge-cloud/relay
nano requirements.txt
```

```
fastapi==0.115.6
uvicorn[standard]==0.34.0
websockets==14.1
httpx==0.28.1
python-multipart==0.0.20
passlib[bcrypt]==1.7.4
pyjwt==2.10.1
```

### 4.6 .env 环境变量文件

#### 4.6.1 生成密钥

**★ 密钥不要自己编，用 `openssl` 生成**：

```bash
# 生成 SECRET_KEY（32 字节 = 64 位十六进制字符）
openssl rand -hex 32
```

**输出示例**：

```
8f3a1c9e7b2d4f6a0e5c8b1d3f7a9e2c4b6d8f0a2c4e6b8d0f2a4c6e8b0d2f4a
```

```bash
# 生成 JWT_SECRET（如果分开用）
openssl rand -hex 32

# 生成初始管理员密码（16 字节 base64）
openssl rand -base64 16
```

| 密钥 | 长度 | 用途 | 泄露后果 |
|---|---|---|---|
| `SECRET_KEY` | 32 字节 hex | 会话签名、密码盐 | **攻击者可伪造登录态**，等于拿到你的照片 |
| `JWT_SECRET` | 32 字节 hex | JWT 签名（若与上面分开） | 同上 |
| `ADMIN_PASSWORD` | 强密码 | 首次创建账号用 | 直接登录你的账号 |

> ⚠️ **不要用** `secret`、`123456`、`changeme` 这类值。攻击者拿到源码后会先试这些。
> 本项目云端虽然零存储，但**能看到明文照片**（[11](11-风险登记册与决策记录.md) R-003），
> 所以账号安全性 = 照片隐私性。

#### 4.6.2 创建 .env

```bash
cd /opt/photobridge-cloud
nano .env
```

**模板**（把 `<...>` 换成你的实际值）：

```bash
# ── PhotoBridge 云端环境变量 ──────────────────────────────
# ★ 本文件含密钥，权限必须是 600，且绝不能提交到 git

# ── 域名（必须与 Caddyfile 第一行一致）─────────────────────
DOMAIN=photos.example.com

# ── 密钥（用 openssl rand -hex 32 生成）───────────────────
SECRET_KEY=<粘贴 openssl rand -hex 32 的输出>
JWT_SECRET=<粘贴另一个 openssl rand -hex 32 的输出>

# ── 首次创建账号用（创建完可以删掉这两行）─────────────────
ADMIN_EMAIL=you@example.com
ADMIN_PASSWORD=<粘贴 openssl rand -base64 16 的输出>

# ── 数据目录（容器内路径，由 compose 的卷映射）───────────
DATA_DIR=/data

# ── 会话有效期（小时）────────────────────────────────────
SESSION_TTL_HOURS=720

# ── 配对码参数（与设计文档 §3 一致，一般不用改）───────────
PAIR_CODE_TTL_SECONDS=300
PAIR_CODE_MAX_ATTEMPTS=5
PAIR_CODE_LOCK_SECONDS=600

# ── 限流（与设计文档 §8.5 一致）──────────────────────────
RATE_LIMIT_PER_CLIENT=120
MAX_UPLOAD_CONCURRENCY=3

# ── 流控窗口（与设计文档 §4 一致，8 MB）──────────────────
CREDIT_WINDOW_BYTES=8388608

# ── 日志级别 ──────────────────────────────────────────────
LOG_LEVEL=INFO

# ── 时区 ──────────────────────────────────────────────────
TZ=Asia/Shanghai
```

**设置权限**（★ 必做）：

```bash
chmod 600 /opt/photobridge-cloud/.env
ls -l /opt/photobridge-cloud/.env
```

**预期输出**：

```
-rw------- 1 deploy deploy 1234 Feb 15 14:30 /opt/photobridge-cloud/.env
```

> 注意前面的 `-rw-------`：只有 owner 能读写。

**★ 确保 .env 不会进 git**：

```bash
cd /opt/photobridge-cloud
cat > .gitignore <<'EOF'
.env
data/
caddy_data/
caddy_config/
*.log
__pycache__/
*.pyc
EOF
```

### 4.7 启动服务

#### 4.7.1 构建并启动

```bash
cd /opt/photobridge-cloud

# 构建镜像 + 启动（-d 后台运行）
docker compose up -d --build
```

**预期输出**：

```
[+] Building 45.2s (12/12) FINISHED
 => [relay internal] load build definition from Dockerfile
 ...
 => => naming to docker.io/library/photobridge-cloud-relay
[+] Running 3/3
 ✔ Network photobridge-cloud_pb-net  Created
 ✔ Container pb-relay                Started
 ✔ Container pb-caddy                Started
```

#### 4.7.2 查看容器状态

```bash
docker compose ps
```

**预期输出**：

```
NAME       IMAGE                        STATUS                    PORTS
pb-caddy   caddy:2                      Up 30 seconds             0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp, 0.0.0.0:443->443/udp
pb-relay   photobridge-cloud-relay      Up 30 seconds (healthy)   8000/tcp
```

| 看到什么 | 说明 |
|---|---|
| 两个都 `Up`，relay 是 `(healthy)` | ✅ 正常 |
| relay 是 `(unhealthy)` | relay 起来了但 `/healthz` 不通，看 §4.7.3 日志 |
| relay 是 `Restarting` | relay 崩溃重启循环，看日志 |
| caddy 是 `Restarting` | Caddyfile 有语法错误或证书申请失败 |

#### 4.7.3 查看日志

```bash
# 看所有服务日志（实时跟随，Ctrl+C 退出）
docker compose logs -f

# 只看 relay
docker compose logs -f relay

# 只看 caddy（★ 证书申请信息在这里）
docker compose logs -f caddy

# 看最近 100 行
docker compose logs --tail=100 relay

# 带时间戳
docker compose logs -t relay
```

**Caddy 首次启动的日志应该长这样**：

```
{"level":"info","msg":"using config from file","file":"/etc/caddy/Caddyfile"}
{"level":"info","logger":"tls.obtain","msg":"acquiring lock","identifier":"photos.example.com"}
{"level":"info","logger":"tls.obtain","msg":"obtaining certificate","identifier":"photos.example.com"}
{"level":"info","logger":"tls.obtain","msg":"certificate obtained successfully","identifier":"photos.example.com"}
{"level":"info","logger":"tls.obtain","msg":"releasing lock","identifier":"photos.example.com"}
```

| 日志关键字 | 含义 |
|---|---|
| `certificate obtained successfully` | ✅ 证书签发成功 |
| `obtaining certificate` 后卡住很久 | 可能在等 ACME 挑战，检查 80 端口和 DNS |
| `challenge failed` / `could not get certificate` | ❌ 证书失败，见 §10 场景 5 |
| `too many failed authorizations` | ❌ 触发 Let's Encrypt 限流，**必须等一周** |
| `dial tcp: lookup relay` | Caddy 找不到 relay，检查是不是同一 network |

### 4.8 验证部署成功

#### 4.8.1 本地验证（在服务器上）

```bash
# 1. relay 健康检查（容器内部）
docker compose exec relay python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read().decode())"

# 2. 从宿主机通过 Caddy 访问
curl -sS -o /dev/null -w "%{http_code}\n" https://photos.example.com/healthz
```

**预期输出**：`200`

#### 4.8.2 从外部验证（在你自己的电脑上）

```powershell
# 1. 测试 HTTPS 是否通
curl.exe -sS -o NUL -w "HTTP %{http_code} | 证书 %{ssl_verify_result} | 耗时 %{time_total}s`n" https://photos.example.com/healthz
```

**预期输出**：

```
HTTP 200 | 证书 0 | 耗时 0.35s
```

> `ssl_verify_result` 为 `0` 表示证书验证通过。非 0 说明证书有问题。

```powershell
# 2. 查看证书信息（签发者、有效期）
echo | openssl s_client -connect photos.example.com:443 -servername photos.example.com 2>$null | openssl x509 -noout -issuer -subject -dates
```

**预期输出**：

```
issuer=C = US, O = Let's Encrypt, CN = R11
subject=CN = photos.example.com
notBefore=Feb 15 06:00:00 2026 GMT
notAfter=May 16 06:00:00 2026 GMT
```

| 字段 | 说明 |
|---|---|
| `issuer` 含 `Let's Encrypt` | ✅ 证书签发正确 |
| `subject=CN = 你的域名` | ✅ 域名匹配 |
| `notAfter` 约 90 天后 | ✅ 正常（Let's Encrypt 有效期 90 天） |

#### 4.8.3 用浏览器验证

在手机或电脑浏览器打开 `https://photos.example.com/healthz`：

| 现象 | 说明 |
|---|---|
| 显示 `{"status":"ok",...}` | ✅ 完美 |
| 地址栏有 🔒 锁标志 | ✅ 证书受信任 |
| 提示"您的连接不是私密连接" | ❌ 证书有问题，见 §10 场景 5 |
| 一直转圈 | ❌ 防火墙/DNS 问题 |

#### 4.8.4 检查证书文件（可选，更底层）

```bash
# 看 Caddy 存证书的目录
sudo ls -R /opt/photobridge-cloud/caddy_data/caddy/certificates/

# 应该能看到类似结构：
# acme-v02.api.letsencrypt.org-directory/photos.example.com/photos.example.com.crt
# acme-v02.api.letsencrypt.org-directory/photos.example.com/photos.example.com.key
```

### 4.9 首次创建账号

云端是**极简单账号**设计（[设计文档.md](../设计文档.md) §15.1 ⑤）：一个邮箱 + 一个密码，**没有注册页、没有找回密码**。

#### 4.9.1 方式一：用 CLI 创建（推荐）

```bash
cd /opt/photobridge-cloud

# 用 relay 自带的 CLI 创建账号（读取 .env 里的 ADMIN_EMAIL / ADMIN_PASSWORD）
docker compose exec relay python -m cli create-account
```

**预期输出**：

```
✓ 账号创建成功
  邮箱: you@example.com
  密码: (已在 .env 中设置，请妥善保存)
  账号 ID: 1
```

**如果 relay 没有 cli 模块**，用下面的一行命令：

```bash
docker compose exec relay python -c "
import os, sqlite3, bcrypt
db = os.environ.get('DATA_DIR', '/data') + '/cloud.db'
email = os.environ['ADMIN_EMAIL']
pwd = os.environ['ADMIN_PASSWORD'].encode()
h = bcrypt.hashpw(pwd, bcrypt.gensalt()).decode()
c = sqlite3.connect(db)
c.execute('INSERT OR REPLACE INTO accounts (id, email, password_hash, created_at) VALUES (1, ?, ?, datetime(\"now\"))', (email, h))
c.commit()
print('✓ 账号已创建:', email)
"
```

#### 4.9.2 方式二：用 API 创建

如果 relay 暴露了初始化接口（仅在无账号时可用）：

```bash
curl -sS -X POST https://photos.example.com/api/auth/setup \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"you@example.com\",\"password\":\"你的强密码\"}"
```

**预期输出**：

```json
{"ok": true, "account_id": 1}
```

> ⚠️ 这个接口**只在账号表为空时可用**，创建成功后会自动关闭。这是安全设计。

#### 4.9.3 验证账号可用

```bash
# 登录测试
curl -sS -X POST https://photos.example.com/api/auth/login \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"you@example.com\",\"password\":\"你的密码\"}" \
  -c /tmp/pb-cookies.txt

# 看是否拿到 cookie
cat /tmp/pb-cookies.txt
```

**预期输出**：

```
{"ok": true, "expires_in": 2592000}
# Netscape HTTP Cookie File
photos.example.com	FALSE	/	FALSE	0	pb_session	eyJhbGciOi...
```

#### 4.9.4 创建成功后加固

```bash
# 1. 从 .env 里删掉明文密码（保留 ADMIN_EMAIL 也行，但密码必须删）
nano /opt/photobridge-cloud/.env
# 删除或注释掉 ADMIN_PASSWORD 那一行

# 2. 重启 relay 让配置生效
docker compose up -d relay

# 3. 确认密码已不在环境变量里
docker compose exec relay env | grep -i admin
```

> **★ 重要**：密码请存到密码管理器（Bitwarden / 1Password / KeePass）。
> **没有找回密码功能**——忘了只能改数据库（§10 场景 20 附带说明）。

### 4.10 §4 完成检查清单

| # | 检查项 | 验证命令 | 预期 |
|---|---|---|---|
| 1 | Docker 装好 | `docker --version` | ≥ 26 |
| 2 | Compose 装好 | `docker compose version` | v2.x |
| 3 | deploy 免 sudo 用 docker | `docker ps` | 不报权限错 |
| 4 | Docker 日志轮转已配 | `docker info --format '{{.LoggingDriver}}'` | `json-file` |
| 5 | 两个容器都在跑 | `docker compose ps` | 都是 `Up` |
| 6 | relay 健康 | `docker compose ps` | `(healthy)` |
| 7 | Caddyfile 语法正确 | `caddy validate` | `Valid configuration` |
| 8 | 证书已签发 | `docker compose logs caddy \| grep "obtained successfully"` | 有该行 |
| 9 | HTTPS 返回 200 | `curl -o /dev/null -w "%{http_code}" https://域名/healthz` | `200` |
| 10 | 证书受信任 | 浏览器打开无警告 | 🔒 |
| 11 | 8000 未暴露公网 | `curl -m 3 http://IP:8000/` | 超时 |
| 12 | .env 权限 600 | `ls -l .env` | `-rw-------` |
| 13 | 账号已创建 | 用 §4.9.3 登录 | 返回 cookie |
| 14 | ADMIN_PASSWORD 已从 .env 删除 | `grep ADMIN_PASSWORD .env` | 无输出 |
