#!/usr/bin/env bash
#
# Reuters 爬虫 + LLM 分析服务 —— 一键部署脚本（Debian 12/13、Ubuntu 20.04+）
#
# 用法：
#   sudo bash deploy/setup.sh                 # 标准部署
#   sudo bash deploy/setup.sh --with-playwright   # 同时安装 playwright 浏览器内核
#   sudo bash deploy/setup.sh --no-chrome         # 跳过 Chrome 安装（已装时用）
#   sudo bash deploy/setup.sh --no-systemd        # 不安装 systemd 服务
#   SWAP_SIZE=4G sudo bash deploy/setup.sh        # 自定义 swap 大小
#
# 环境变量：
#   APP_USER      运行服务的系统用户（默认 www-data）
#   SWAP_SIZE     swap 大小（默认 2G；设为 0 可跳过）
#   INSTALL_CHROME=yes|no
#   SETUP_SYSTEMD=yes|no
#
set -euo pipefail

# ---------- 基础配置 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${APP_DIR:-$SCRIPT_DIR}"
APP_USER="${APP_USER:-www-data}"
SWAP_SIZE="${SWAP_SIZE:-2G}"
INSTALL_CHROME="${INSTALL_CHROME:-yes}"
SETUP_SYSTEMD="${SETUP_SYSTEMD:-yes}"
INSTALL_PLAYWRIGHT="${INSTALL_PLAYWRIGHT:-no}"

# 解析命令行开关
for arg in "$@"; do
  case "$arg" in
    --with-playwright) INSTALL_PLAYWRIGHT=yes ;;
    --no-chrome) INSTALL_CHROME=no ;;
    --no-systemd) SETUP_SYSTEMD=no ;;
    --no-swap) SWAP_SIZE=0 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg（用 --help 查看用法）"; exit 1 ;;
  esac
done

# ---------- 输出辅助 ----------
c_reset='\033[0m'; c_green='\033[32m'; c_yellow='\033[33m'; c_red='\033[31m'; c_blue='\033[34m'
log()  { echo -e "${c_blue}[INFO]${c_reset} $*"; }
ok()   { echo -e "${c_green}[ OK ]${c_reset} $*"; }
warn() { echo -e "${c_yellow}[WARN]${c_reset} $*"; }
err()  { echo -e "${c_red}[FAIL]${c_reset} $*" >&2; }
step() { echo -e "\n${c_blue}==== $* ====${c_reset}"; }

# ---------- 0. 前置检查 ----------
step "0/8 前置检查"

if [[ $EUID -ne 0 ]]; then
  err "请使用 root 或 sudo 运行：sudo bash deploy/setup.sh"
  exit 1
fi

if [[ ! -f "$APP_DIR/main.py" ]]; then
  err "未在 $APP_DIR 找到 main.py，请确认在 crawler 目录下运行本脚本"
  exit 1
fi
ok "应用目录: $APP_DIR"

if [[ -f /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  log "系统: ${PRETTY_NAME:-未知}"
  if [[ "${ID:-}" != "debian" && "${ID:-}" != "ubuntu" ]]; then
    warn "本脚本面向 Debian/Ubuntu，其它发行版请手动安装依赖"
  fi
else
  warn "无法识别系统版本，将继续尝试"
fi

TOTAL_MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
log "内存: ${TOTAL_MEM_MB} MB"
if (( TOTAL_MEM_MB < 2048 )); then
  warn "内存小于 2GB，Chrome 有头模式可能 OOM —— 建议配置 swap（本脚本会自动处理）"
fi

# ---------- 1. 系统依赖 ----------
step "1/8 安装系统依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  ca-certificates curl wget gnupg \
  python3 python3-venv python3-pip \
  xvfb \
  libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 libxfixes3 \
  libgbm1 libasound2 libatk-bridge2.0-0 libatk1.0-0 libnss3 libcups2 \
  libdrm2 libxkbcommon0 libpango-1.0-0 libcairo2
ok "系统依赖就绪（含 xvfb 与 Chrome 运行库）"

# ---------- 2. Swap ----------
step "2/8 配置 swap"
if [[ "$SWAP_SIZE" == "0" ]]; then
  log "已指定 --no-swap，跳过"
elif swapon --show | grep -q '^/swapfile'; then
  ok "swap 已存在，跳过"
else
  log "创建 ${SWAP_SIZE} swap（小内存机器防 OOM）"
  fallocate -l "$SWAP_SIZE" /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  ok "swap 已启用"
fi

# ---------- 3. Chrome ----------
step "3/8 安装 Chrome（nodriver 依赖）"
if [[ "$INSTALL_CHROME" == "no" ]]; then
  log "已指定 --no-chrome，跳过"
elif command -v google-chrome >/dev/null 2>&1; then
  ok "Chrome 已安装: $(google-chrome --version 2>/dev/null || echo 已存在)"
else
  log "下载并安装 Google Chrome"
  TMP_DEB="$(mktemp -d)/chrome.deb"
  wget -q -O "$TMP_DEB" https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  apt-get install -y --no-install-recommends "$TMP_DEB" || {
    warn "Chrome 安装失败（可能缺少依赖），尝试修复依赖后重试"
    apt-get -f install -y
    apt-get install -y --no-install-recommends "$TMP_DEB"
  }
  rm -f "$TMP_DEB"
  ok "Chrome: $(google-chrome --version 2>/dev/null || echo installed)"
fi

# ---------- 4. Python 虚拟环境 ----------
step "4/8 创建 Python 虚拟环境并安装依赖"
VENV_DIR="$APP_DIR/venv"
if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv "$VENV_DIR"
  ok "已创建 venv: $VENV_DIR"
else
  ok "venv 已存在"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip -q
pip install -r "$APP_DIR/requirements.txt"
ok "Python 依赖安装完成"

PY_VER=$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
log "Python 版本: $PY_VER"
if python - <<'EOF'
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
EOF
then
  ok "Python >= 3.10，nodriver 通道可用"
else
  warn "Python < 3.10：nodriver 通道将自动跳过（其余通道不受影响）"
fi

# ---------- 5. Playwright（可选） ----------
step "5/8 Playwright 浏览器内核（可选）"
if [[ "$INSTALL_PLAYWRIGHT" == "yes" ]]; then
  pip install playwright -q
  python -m playwright install --with-deps chromium || warn "playwright 内核安装失败，可稍后手动执行"
  ok "playwright chromium 就绪"
else
  log "跳过（默认走 nodriver，它使用系统 Chrome；如需 playwright 兜底加 --with-playwright）"
fi

# ---------- 6. 环境变量 ----------
step "6/8 生成 .env"
ENV_FILE="$APP_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
  ok ".env 已存在，不覆盖（如需重置请手动删除）"
else
  cp "$APP_DIR/config.example.env" "$ENV_FILE"
  ok "已从 config.example.env 生成 .env"
  warn "请编辑 $ENV_FILE 填入 DB_* 与 DASHSCOPE_API_KEY"
fi

# ---------- 7. systemd 服务 ----------
step "7/8 安装 systemd 服务"
if [[ "$SETUP_SYSTEMD" == "no" ]]; then
  log "已指定 --no-systemd，跳过"
else
  id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER"

  mkdir -p "$APP_DIR/logs"
  chown -R "$APP_USER:$APP_USER" "$APP_DIR"

  for svc in reuters-crawler llm-analyzer; do
    SRC="$APP_DIR/deploy/$svc.service"
    if [[ -f "$SRC" ]]; then
      sed -e "s|/opt/reuters-crawler|$APP_DIR|g" -e "s|^User=.*|User=$APP_USER|" -e "s|^Group=.*|Group=$APP_USER|" \
        "$SRC" > "/etc/systemd/system/$svc.service"
      ok "已安装 $svc.service"
    fi
  done

  systemctl daemon-reload
  log "服务已注册（未自动启动，请先配置 .env）"
fi

# ---------- 8. 完成 ----------
step "8/8 部署完成"
cat <<EOF

${c_green}部署完成！${c_reset}

下一步：
  1) 配置环境变量
     sudo -u $APP_USER nano $ENV_FILE
     必填：DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME
            DASHSCOPE_API_KEY（分析服务用）
     若服务器本机已跑代理（如 http://127.0.0.1:7928），请保留并填 HTTPS_PROXY / HTTP_PROXY；仅直连无障碍时删除

  2) 单轮验证（先不入库，看能否挖到正文）
     cd $APP_DIR && sudo -u $APP_USER xvfb-run -a ./venv/bin/python main.py --once --dry-run

  3) 真实入库验证
     sudo -u $APP_USER xvfb-run -a ./venv/bin/python main.py --once
     sudo -u $APP_USER ./venv/bin/python analyze_main.py --once

  4) 启动常驻服务
     sudo systemctl enable --now reuters-crawler
     sudo systemctl enable --now llm-analyzer
     journalctl -u reuters-crawler -f

小内存（<2GB）建议：
  MAX_PER_ROUND=3 NODRIVER_REQUEST_INTERVAL=8
  观察内存：watch -n 2 free -h

详细文档见：$APP_DIR/DEPLOYMENT.md
EOF
