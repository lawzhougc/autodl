#!/bin/bash
# 构建 fpk 安装包
# 用法: ./build.sh   （需要已安装 fnpack: https://developer.fnnas.com/docs/cli/fnpack/）
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="${SRC}/autodl"
ROOT="$(dirname "${SRC}")"

command -v fnpack >/dev/null 2>&1 || {
  echo "未找到 fnpack。请先下载安装：https://developer.fnnas.com/docs/cli/fnpack/"
  exit 1
}

echo "==> 同步最新源码到打包目录"
cp "${ROOT}/gy_autodl.py"        "${PKG_DIR}/app/server/gy_autodl.py"
cp "${ROOT}/web/app.py"          "${PKG_DIR}/app/server/web/app.py"
cp "${ROOT}/web/index.html"      "${PKG_DIR}/app/server/web/index.html"
cp "${ROOT}/config.example.json" "${PKG_DIR}/app/server/config.example.json"

chmod 644 "${PKG_DIR}"/app/server/gy_autodl.py \
          "${PKG_DIR}"/app/server/config.example.json \
          "${PKG_DIR}"/app/server/web/app.py \
          "${PKG_DIR}"/app/server/web/index.html
chmod 755 "${PKG_DIR}"/cmd/*

echo "==> 打包"
cd "${PKG_DIR}"
fnpack build

echo "==> 完成: ${PKG_DIR}/autodl.fpk"
