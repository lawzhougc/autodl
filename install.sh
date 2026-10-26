#!/usr/bin/env bash
# 影视资源自动追踪控制台 —— 一键安装脚本
# 用法: ./install.sh [安装目录]
set -euo pipefail

DEST="${1:-/opt/autodl}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
