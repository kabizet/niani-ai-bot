#!/bin/bash
# 自动探测设备配置，生成 machine.env，并初始化配置模板
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "======================================"
echo " QQ AI Ops - 自动配置"
echo "======================================"

# ---------- 探测 ----------
OS_NAME=$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || uname -s)
ARCH=$(uname -m)
KERNEL=$(uname -r)
CPU_MODEL=$(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2 | sed 's/^ *//' || echo unknown)
CPU_CORES=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)
MEM_MB=$(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
MEM_GB=$(( (MEM_MB + 1023) / 1024 ))
DISK_TOTAL=$(df -h / | awk 'NR==2{print $2}')
DISK_FREE=$(df -h / | awk 'NR==2{print $4}')
TZ_NAME=$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo UTC)
PY_VER=$(python3 -V 2>/dev/null | awk '{print $2}' || echo missing)
HOST=$(hostname)

cat > machine.env <<EOF
# 由 setup.sh 自动生成（$(date '+%Y-%m-%d %H:%M:%S')）
OS_NAME="$OS_NAME"
ARCH="$ARCH"
KERNEL="$KERNEL"
CPU_MODEL="$CPU_MODEL"
CPU_CORES=$CPU_CORES
MEM_MB=$MEM_MB
MEM_GB=$MEM_GB
DISK_TOTAL="$DISK_TOTAL"
DISK_FREE="$DISK_FREE"
TIMEZONE="$TZ_NAME"
PYTHON_VERSION="$PY_VER"
HOSTNAME="$HOST"
EOF

echo
echo "检测到设备配置（已写入 machine.env）："
echo "  系统    : $OS_NAME"
echo "  架构    : $ARCH"
echo "  内核    : $KERNEL"
echo "  CPU     : $CPU_MODEL（$CPU_CORES 核）"
echo "  内存    : ${MEM_GB}G"
echo "  磁盘    : $DISK_TOTAL（剩余 $DISK_FREE）"
echo "  时区    : $TZ_NAME"
echo "  Python  : $PY_VER"
echo

# ---------- 初始化配置 ----------
init() {
  if [ ! -f "$2" ]; then
    cp "$1" "$2"
    echo "  生成 $2"
  else
    echo "  已存在 $2（跳过）"
  fi
}
echo "初始化配置文件："
init digest/llm.example.json digest/llm.json

# ---------- 依赖 ----------
if [ "$PY_VER" = "missing" ]; then
  echo "!! 未检测到 python3，请先安装"
else
  echo "检查 Python 依赖…"
  python3 - <<'PY'
try:
    import importlib.util
    mods = ["requests", "aiohttp", "websockets", "psutil", "Pillow"]
    missing = [m for m in mods if importlib.util.find_spec(m) is None]
    print("缺少：" + ", ".join(missing) if missing else "依赖齐全")
except Exception as e:
    print("检查失败:", e)
PY
fi

echo
echo "下一步："
echo "  1) 编辑 digest/llm.json 填 api_key / persona / 你的 QQ"
echo "  2) 编辑 ops/config.json 填 api_key / owners / web_token"
echo "  3) python3 digest/bot_server.py 启动机器人"
