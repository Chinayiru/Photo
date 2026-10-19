# PhotoBridge 云端 Relay
#
# 构建上下文是**仓库根目录**，不是 `cloud/`：
#   docker build -f cloud/Dockerfile .
#
# 为什么不在 `cloud/` 里构建：`cloud/relay/*` 依赖 `shared/protocol.py`
# 与 `shared/mux.py`，而这两个文件是**云端与电脑端共享的同一份物理文件**
# （docs/09 §9.6 第 4 条 —— 共享才不会有帧格式漂移）。把 `shared/`
# 复制进 `cloud/` 会造成两份副本，那正是设计要避免的事。
#
# `.dockerignore` 把除 cloud/、shared/、frontend/dist/、requirements.txt
# 以外的全部排除在外，所以"上下文是仓库根"不会把 frontend/node_modules、
# data/、模拟照片这些几百 MB 的东西塞进构建缓存。

FROM python:3.12-slim

# 环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    TZ=Asia/Shanghai

WORKDIR /app

# 先装依赖（利用层缓存：requirements.txt 不变就不重装）
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 再拷源码（改代码不会让依赖层失效）
COPY shared/ ./shared/
COPY cloud/ ./cloud/
COPY scripts/cloud_init.py ./scripts/cloud_init.py

# ★ 手机端构建产物。没有它，`https://域名/` 就是一个 404 —— 手机上
#   出门只有域名，于是整条隧道在产品上不可达（实测见 `cloud/relay/main.py`
#   的模块文档）。`pages.StaticPages` 从 `/app/frontend/dist` 读，
#   与 `DEFAULT_DIST`（`cloud/relay/pages.py`）一致。
#   构建前必须先 `cd frontend && npm run build`；`deploy.sh` 的 `--check`
#   会验证这个目录存在，否则构建出来的镜像里没有页面。
COPY frontend/dist/ ./frontend/dist/

# 数据目录（会被 compose 的卷覆盖）
RUN mkdir -p /data

# 以非 root 运行。容器被攻破时不能直接拿到宿主机 root，
# 也不能顺手改掉 /app 里的代码。
RUN useradd -m -u 1000 relay && chown -R relay:relay /app /data
USER relay

EXPOSE 8000

# 健康检查。路径用实现里的 `/api/health`（docs/04 §3.7）。
# 部署文档里的 `/healthz` 是给 Caddy 用的对外别名，Caddyfile 里 rewrite 过来。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

# 启动。
# ★ `--workers 1` 不可改：设备注册表是**内存 map**，多 worker 会让
#   "电脑连在 worker A、手机请求打到 worker B" 变成常态，表现是随机 503。
#   `cloud/relay/main.py::main()` 里已经写死单进程，这里再显式写一遍，
#   因为绕过 main() 直接调 uvicorn 是运维最容易做的事。
CMD ["python", "-m", "uvicorn", "cloud.relay.main:create_app", \
     "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--ws", "websockets", \
     "--proxy-headers", "--forwarded-allow-ips", "172.16.0.0/12"]
