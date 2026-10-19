#!/usr/bin/env bash
# PhotoBridge 云端一键部署（P1-10）
#
# 用法（在服务器上，仓库根目录）：
#   bash cloud/deploy.sh              # 部署/升级
#   bash cloud/deploy.sh --check      # 只体检，不改动
#   bash cloud/deploy.sh --account    # 部署后再建一个账号（交互式问邮箱口令）
#
# 设计原则：**幂等 + 可预检**。同一个脚本既能首装也能升级，
# 而 `--check` 让"先看看会不会坏事"成为默认手艺。
#
# ★ 这个脚本不碰任何照片。它只起两个容器、建一个空的 cloud.db，
#   然后打印"下一步该把哪三行配置抄到电脑端"。

set -Eeuo pipefail

# ── 常量 ───────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CLOUD_DIR="${SCRIPT_DIR}"
COMPOSE="docker compose -f ${CLOUD_DIR}/docker-compose.yml --project-directory ${CLOUD_DIR}"

# 部署位置按 docs/02 §7.1 是 /opt/photobridge-cloud。
# 但脚本是从仓库里跑起来的，所以两种布局都支持：
#   * 仓库已经在 /opt/photobridge-cloud —— 直接用
#   * 仓库在别处 —— 提示先 rsync 过去，不擅自 cp（几百 MB）
TARGET_DIR="/opt/photobridge-cloud"

MODE="deploy"
WANT_ACCOUNT=0

# ── 输出helper ────────────────────────────────────────────────
if [ -t 1 ] && command -v tput >/dev/null 2>&1; then
  C_RED="$(tput setaf 1)"; C_GRN="$(tput setaf 2)"
  C_YEL="$(tput setaf 3)"; C_BLU="$(tput setaf 4)"; C_RST="$(tput sgr0)"
else
  C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_RST=""
fi

step() { printf '\n%s==> %s%s\n' "${C_BLU}" "$*" "${C_RST}"; }
ok()   { printf '  %s✓%s %s\n' "${C_GRN}" "${C_RST}" "$*"; }
warn() { printf '  %s!%s %s\n' "${C_YEL}" "${C_RST}" "$*"; }
die()  { printf '  %s✗%s %s\n' "${C_RED}" "${C_RST}" "$*" >&2; exit 1; }

trap 'die "第 ${LINENO} 行失败（退出码 $?）。容器状态：$(docker compose ps 2>/dev/null | tr "\n" " ")"' ERR

usage() {
  sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
}

# ── 参数 ───────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --check)   MODE="check" ;;
    --account) WANT_ACCOUNT=1 ;;
    -h|--help) usage ;;
    *) die "未知参数：$1（用 --help 看用法）" ;;
  esac
  shift
done

# ── 1. 前置条件 ────────────────────────────────────────────────
step "检查前置条件"

command -v docker >/dev/null 2>&1 || die "没有 docker。先装：https://docs.docker.com/engine/install/"
ok "docker $(docker --version | awk '{print $3}' | tr -d ,)"

docker compose version >/dev/null 2>&1 || die "没有 docker compose 插件（v2）。装 docker-compose-plugin。"
ok "docker compose $(docker compose version --short 2>/dev/null || echo '?')"

if ! docker info >/dev/null 2>&1; then
  die "当前用户无权访问 docker daemon。加组后重新登录：sudo usermod -aG docker \$USER"
fi
ok "daemon 可访问"

[ -f "${CLOUD_DIR}/docker-compose.yml" ] || die "缺 ${CLOUD_DIR}/docker-compose.yml"
[ -f "${CLOUD_DIR}/Caddyfile" ]         || die "缺 ${CLOUD_DIR}/Caddyfile"
[ -f "${CLOUD_DIR}/Dockerfile" ]        || die "缺 ${CLOUD_DIR}/Dockerfile"
[ -f "${REPO_ROOT}/shared/protocol.py" ] || die "缺 ${REPO_ROOT}/shared/protocol.py —— 构建上下文必须是仓库根"
# 手机端页面由 relay 伺服（`cloud/relay/pages.py`），镜像里没有它就等于
# 手机打开域名得到 404 —— 而隧道本身一切正常，排查会从错误的方向开始。
# (`frontend/dist` 被 gitignore，克隆下来必须自己构建一次。)
[ -f "${REPO_ROOT}/frontend/dist/index.html" ] || die "缺 ${REPO_ROOT}/frontend/dist/index.html —— 先在仓库根跑 (cd frontend && npm ci && npm run build)"
ok "部署文件齐全（上下文 = ${REPO_ROOT}）"

# .env
if [ ! -f "${CLOUD_DIR}/.env" ]; then
  warn "没有 .env，从 .env.example 复制一份"
  cp "${CLOUD_DIR}/.env.example" "${CLOUD_DIR}/.env"
  die "请先编辑 ${CLOUD_DIR}/.env，把 DOMAIN 与 ACME_EMAIL 改成真实值"
fi
# shellcheck disable=SC1091
set -a; . "${CLOUD_DIR}/.env"; set +a
[ -n "${DOMAIN:-}" ] || die ".env 里 DOMAIN 是空的"
[ -n "${ACME_EMAIL:-}" ] || die ".env 里 ACME_EMAIL 是空的"
case "${DOMAIN}" in
  *://*|*/*) die "DOMAIN 不要带协议或路径，只要主机名：${DOMAIN}" ;;
esac
ok "DOMAIN=${DOMAIN}"

# 端口占用（Caddy 要 80/443）
for p in 80 443; do
  if command -v ss >/dev/null 2>&1; then
    if ss -lntH "sport = :${p}" 2>/dev/null | grep -q .; then
      if docker ps --format '{{.Ports}}' 2>/dev/null | grep -q ":${p}->"; then
        warn "端口 ${p} 已被本项目之外的容器占用"
      else
        warn "端口 ${p} 已被别的进程占用（nginx/apache？）—— Caddy 会起不来"
      fi
    fi
  fi
done

# 数据目录
mkdir -p "${CLOUD_DIR}/data"
ok "数据目录 ${CLOUD_DIR}/data（cloud.db 会落在这里）"

# 体检模式到这儿就够
if [ "${MODE}" = "check" ]; then
  step "校验 Caddyfile 语法"
  if docker image inspect caddy:2 >/dev/null 2>&1; then
    docker run --rm -e DOMAIN="${DOMAIN}" -e ACME_EMAIL="${ACME_EMAIL}" \
      -v "${CLOUD_DIR}/Caddyfile:/etc/caddy/Caddyfile:ro" \
      caddy:2 caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1 \
      | tail -5
  else
    warn "本地还没有 caddy:2 镜像，跳过语法校验（部署时会自动拉）"
  fi
  step "体检结论"
  ok "前置条件全部满足。跑 bash cloud/deploy.sh 开始部署。"
  exit 0
fi

# ── 2. 位置提醒 ────────────────────────────────────────────────
if [ "${REPO_ROOT}" != "${TARGET_DIR}" ]; then
  step "部署位置"
  warn "当前仓库在 ${REPO_ROOT}，而文档约定是 ${TARGET_DIR}"
  warn "继续也可以（compose 用的是绝对路径），但升级脚本/备份路径按 ${TARGET_DIR} 写的。"
  warn "搬过去：sudo rsync -a --exclude .git --exclude data --exclude dist ./ ${TARGET_DIR}/"
  printf '  继续？[y/N] '
  read -r reply
  case "${reply}" in y|Y|yes) ;; *) die "已取消" ;; esac
fi

# ── 3. 构建并启动 ──────────────────────────────────────────────
step "构建镜像"
# relay 用单进程（注册表是内存 map），所以构建参数里没有任何 workers 变量
${COMPOSE} build --pull relay
ok "镜像构建完成"

step "启动"
${COMPOSE} up -d
ok "容器已启动"

# ── 4. 等健康 ─────────────────────────────────────────────────
step "等待 relay 健康"
for i in $(seq 1 40); do
  state="$(docker inspect -f '{{.State.Health.Status}}' pb-relay 2>/dev/null || echo missing)"
  case "${state}" in
    healthy) ok "relay healthy（${i} 次探测）"; break ;;
    unhealthy) docker logs --tail 40 pb-relay || true; die "relay 不健康" ;;
    missing) die "pb-relay 容器不存在，看 ${COMPOSE} ps" ;;
  esac
  [ "${i}" -eq 40 ] && { docker logs --tail 40 pb-relay || true; die "relay 60 秒内没有 healthy"; }
  sleep 1.5
done

# ── 5. 端到端自检 ─────────────────────────────────────────────
step "自检：云端健康接口"
health=$(docker exec pb-relay python -c \
  "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=5).read().decode())" 2>&1) \
  || die "relay 内部 /api/health 不通：${health}"
ok "relay 内部：${health}"

step "自检：HTTPS 对外"
# 证书刚申请时 Caddy 可能还在等 ACME，最多等 60 秒
http_code="000"
for i in $(seq 1 20); do
  http_code="$(curl -sS -o /dev/null -w '%{http_code}' -m 10 "https://${DOMAIN}/healthz" 2>/dev/null || echo 000)"
  [ "${http_code}" = "200" ] && break
  sleep 3
done
if [ "${http_code}" = "200" ]; then
  ok "https://${DOMAIN}/healthz → 200"
else
  warn "https://${DOMAIN}/healthz → ${http_code}（证书可能还在申请）"
  warn "排查：docker logs pb-caddy | tail -30"
  warn "确认 ${DOMAIN} 的 A/AAAA 记录已指向本机，且 80/443 放行"
fi

step "自检：仪表盘不泄漏"
# 未匹配路径必须是 404，不能是 200（否则扫描器会把 /.env 当成存在）
code_env="$(curl -sS -o /dev/null -w '%{http_code}' -m 10 "https://${DOMAIN}/.env" 2>/dev/null || echo 000)"
if [ "${code_env}" = "404" ]; then
  ok "/.env → 404（未匹配路径不返回 200）"
else
  warn "/.env → ${code_env}，期望 404"
fi

# ── 6. 建账号 ─────────────────────────────────────────────────
step "账号"
accounts=$(${COMPOSE} exec -T relay python - <<'PY' 2>/dev/null || echo 0
import os
from cloud.relay.db import CloudDB
db = CloudDB(os.environ.get("PB_CLOUD_DB", "/data/cloud.db"))
db.init()
row = db.query_one("SELECT COUNT(*) AS n FROM accounts")
print(int(row["n"]) if row else 0)
PY
)
if [ "${accounts:-0}" -gt 0 ] 2>/dev/null; then
  ok "已有 ${accounts} 个账号"
elif [ "${WANT_ACCOUNT}" = "1" ]; then
  printf '  邮箱：'; read -r acct_email
  printf '  口令（至少 8 位，输入不显示）：'; read -rs acct_pass; printf '\n'
  ${COMPOSE} exec -T relay python /app/scripts/cloud_init.py \
    --email "${acct_email}" --password "${acct_pass}" \
    --device-name "$(hostname)" --base-url "https://${DOMAIN}"
  ok "账号与设备已建。★ agent_token 只打印过这一次，立刻抄走"
else
  warn "还没有账号。下一步："
  warn "  bash cloud/deploy.sh --account"
  warn "  或 docker compose exec relay python /app/scripts/cloud_init.py --email ... --password ..."
fi

# ── 7. 收尾 ───────────────────────────────────────────────────
step "部署完成"
${COMPOSE} ps
cat <<EOF

下一步（在**电脑端**做）：
  1. 把 cloud_init 打印的三行写进 data/config.json 的 "cloud" 段：
       "base_url":  "https://${DOMAIN}/d/<device_id>"
       "device_id": "<32 位云端 id>"
       "agent_token": "<明文令牌>"
  2. 把 server.transport 改成 "cloud_tunnel"（它会同时保留局域网监听）
  3. 重启电脑端，然后：
       curl -s https://${DOMAIN}/d/<device_id>/api/system/ping

常用命令：
  查看状态   ${COMPOSE} ps
  看日志     docker logs -f pb-relay
  备份库     docker exec pb-relay python -c "import sqlite3;sqlite3.connect('/data/cloud.db').execute('VACUUM INTO \\'/data/backup.db\\'')"
  升级       git pull && bash cloud/deploy.sh
EOF
