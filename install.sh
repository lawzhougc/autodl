#!/usr/bin/env bash
# 影视资源自动追踪控制台 —— 一键安装脚本
# 用法: ./install.sh [安装目录] [--skip-check]
#   --skip-check  跳过前置依赖检查强制安装
set -euo pipefail

DEST="${1:-/opt/autodl}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------- 前置依赖检查 ----------
# 本脚本只负责放置程序文件，真正的搜索/下载依赖下面的组件。
MISSING=()

command -v python3 >/dev/null 2>&1 || MISSING+=("python3（必需，应用本体）")
command -v aria2c  >/dev/null 2>&1 || MISSING+=("aria2c（必需，下载到 NAS）")
command -v ffmpeg  >/dev/null 2>&1 || MISSING+=("ffmpeg（必需，字幕提取转换）")
command -v ffprobe >/dev/null 2>&1 || MISSING+=("ffprobe（必需，字幕提取）")

# 光鸭云盘（云下载 / 下载到 NAS 的执行端）
GY_URL_CHECK="${GY_URL:-http://127.0.0.1:8998}"
if ! curl -s -m 5 -o /dev/null "${GY_URL_CHECK}/api/user" 2>/dev/null; then
    MISSING+=("光鸭云盘 App（必需，${GY_URL_CHECK} 无响应）")
fi

# Jackett（资源搜索）
JT_URL_CHECK="${JACKETT_URL:-http://127.0.0.1:9117}"
if ! curl -s -m 5 -o /dev/null "${JT_URL_CHECK}/" 2>/dev/null; then
    MISSING+=("Jackett（必需，${JT_URL_CHECK} 无响应）")
fi

if [ ${#MISSING[@]} -gt 0 ]; then
    cat <<EOF

⚠️  以下依赖尚未就绪：

EOF
    for m in "${MISSING[@]}"; do echo "   - ${m}"; done
    cat <<EOF

请先准备好再继续，否则应用能启动但无法真正下载。

fnOS 上可直接安装：
   trim-cli app install guangyapan --yes    # 光鸭云盘
   trim-cli app install jackett --yes       # Jackett

命令行工具（aria2 / ffmpeg）：
   apt install aria2 ffmpeg

可选（仅 uindex.org 需要）：
   FlareSolverr —— 见 README「FlareSolverr」章节

若确认继续安装，请加 --skip-check 参数。

EOF
    if [ "${2:-}" != "--skip-check" ] && [ "${1:-}" != "--skip-check" ]; then
        exit 1
    fi
fi

echo "==> 依赖检查通过"
echo "==> 安装到 ${DEST}"
mkdir -p "${DEST}"/{logs,web,flaresolverr}

cp "${SRC}/gy_autodl.py"           "${DEST}/"
cp "${SRC}/web/app.py"             "${DEST}/web/"
cp "${SRC}/web/index.html"         "${DEST}/web/"
cp "${SRC}/flaresolverr/docker-compose.yml" "${DEST}/flaresolverr/"
cp "${SRC}/README.md"              "${DEST}/"

# 配置文件只在不存在时创建，避免覆盖已有的凭据设置
if [ ! -f "${DEST}/config.json" ]; then
  cp "${SRC}/config.example.json" "${DEST}/config.json"
  chmod 600 "${DEST}/config.json"
  echo "==> 已生成 ${DEST}/config.json，请编辑以下项后使用："
  echo "    media_root       你的媒体库路径"
  echo "    subhd_email      SubHD 账号（可选，用于中文字幕下载）"
  echo "    subhd_password   SubHD 密码"
else
  echo "==> 检测到已有 config.json，跳过（如需重置请手动删除）"
fi

cat <<EOF

==> 安装完成。接下来：

1. 编辑配置：
     vi ${DEST}/config.json

2. 确认前置服务在运行：
     - 光鸭云盘 App（本地端口 8998）
     - Jackett（本地端口 9117）
   如端口或路径不同，用环境变量覆盖：
     export GY_URL=http://127.0.0.1:8998
     export JACKETT_URL=http://127.0.0.1:9117
     export JACKETT_CONFIG=/path/to/jackett/data/ServerConfig.json

3. 测试搜索：
     cd ${DEST} && python3 gy_autodl.py search "Oppenheimer 2023"

4. 加定时任务（crontab -e）：
     13 * * * *  /usr/bin/python3 ${DEST}/gy_autodl.py run >> ${DEST}/logs/cron.log 2>&1
     */5 * * * * /usr/bin/python3 ${DEST}/web/app.py    >> ${DEST}/logs/web.log 2>&1

5. 打开 Web 控制台：
     http://<你的NAS地址>:8770

EOF
