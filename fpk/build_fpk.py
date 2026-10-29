#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Python 版 FPK 打包工具 (跨平台支持 Windows / macOS / Linux)
符合飞牛 NAS (fnOS) 官方应用规范
"""

import os
import shutil
import tarfile
import io
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FPK_DIR = os.path.join(ROOT, "fpk", "autodl")
SERVER_DIR = os.path.join(FPK_DIR, "app", "server")


def sync_files():
    print("==> 1. 同步最新源码到 fpk 目录")
    os.makedirs(SERVER_DIR, exist_ok=True)
    os.makedirs(os.path.join(SERVER_DIR, "web"), exist_ok=True)
    os.makedirs(os.path.join(SERVER_DIR, "flaresolverr"), exist_ok=True)
    os.makedirs(os.path.join(FPK_DIR, "app", "www"), exist_ok=True)
    os.makedirs(os.path.join(FPK_DIR, "app", "config"), exist_ok=True)

    # 同步核心文件
    files_to_sync = [
        (os.path.join(ROOT, "gy_autodl.py"), os.path.join(SERVER_DIR, "gy_autodl.py")),
        (os.path.join(ROOT, "web", "app.py"), os.path.join(SERVER_DIR, "web", "app.py")),
        (os.path.join(ROOT, "web", "index.html"), os.path.join(SERVER_DIR, "web", "index.html")),
        (os.path.join(ROOT, "config.example.json"), os.path.join(SERVER_DIR, "config.example.json")),
    ]

    flare = os.path.join(ROOT, "flaresolverr", "docker-compose.yml")
    if os.path.exists(flare):
        files_to_sync.append((flare, os.path.join(SERVER_DIR, "flaresolverr", "docker-compose.yml")))

    # 同步 config/privilege 与 config/resource 到 app/config
    for cfg in ["privilege", "resource"]:
        src_cfg = os.path.join(FPK_DIR, "config", cfg)
        dst_cfg = os.path.join(FPK_DIR, "app", "config", cfg)
        if os.path.exists(src_cfg):
            files_to_sync.append((src_cfg, dst_cfg))

    for src, dst in files_to_sync:
        if os.path.exists(src):
            shutil.copy2(src, dst)
            print(f"  已同步: {os.path.relpath(src, ROOT)} -> {os.path.relpath(dst, ROOT)}")

    # 清理 pycache 避免无用的 pyc 污染包
    for root, dirs, _ in os.walk(FPK_DIR):
        for d in dirs:
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)


def ensure_lf(path):
    """确保文本及脚本文件换行符为 LF"""
    try:
        with open(path, "rb") as f:
            content = f.read()
        if b"\x00" in content:  # 二进制文件跳过
            return
        if b"\r\n" in content:
            new_content = content.replace(b"\r\n", b"\n")
            with open(path, "wb") as f:
                f.write(new_content)
    except Exception:
        pass


def get_version():
    manifest_path = os.path.join(FPK_DIR, "manifest")
    with open(manifest_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = re.match(r'^\s*version\s*=\s*["\']?([^"\'\s]+)', line)
            if m:
                return m.group(1)
    return "0.2.12"


def make_tarinfo_filter(is_root_level=False):
    def filter_func(tarinfo):
        # 飞牛 NAS 要求所有权为 root:root (0:0)
        tarinfo.uid = 0
        tarinfo.gid = 0
        tarinfo.uname = "root"
        tarinfo.gname = "root"

        # 设置权限
        name = tarinfo.name.replace("\\", "/")
        if tarinfo.isdir():
            tarinfo.mode = 0o755
        elif name.startswith("cmd/") or "/bin/" in name:
            tarinfo.mode = 0o755
        elif name.endswith("/resource") or name == "wizard/install":
            tarinfo.mode = 0o600
        else:
            tarinfo.mode = 0o644
        return tarinfo
    return filter_func


def build():
    sync_files()

    print("==> 2. 检查与规范换行符 (LF)")
    for root, _, files in os.walk(FPK_DIR):
        for f in files:
            p = os.path.join(root, f)
            ensure_lf(p)

    ver = get_version()
    print(f"==> 3. 读取应用版本: v{ver}")

    # 打包 app.tgz
    print("==> 4. 生成内部应用包 app.tgz")
    app_dir = os.path.join(FPK_DIR, "app")
    app_tgz_io = io.BytesIO()

    with tarfile.open(fileobj=app_tgz_io, mode="w:gz", format=tarfile.PAX_FORMAT) as app_tar:
        # 添加 server, ui, www, config
        entries = sorted(os.listdir(app_dir))
        for entry in entries:
            entry_path = os.path.join(app_dir, entry)
            app_tar.add(entry_path, arcname=entry, filter=make_tarinfo_filter())

    app_tgz_bytes = app_tgz_io.getvalue()
    print(f"  app.tgz 大小: {len(app_tgz_bytes)} 字节")

    # 打包最终 fpk
    fpk_name = f"autodl-{ver}.fpk"
    fpk_path = os.path.join(ROOT, fpk_name)
    alt_fpk_path = os.path.join(FPK_DIR, "autodl.fpk")

    print(f"==> 5. 构建最终安装包 {fpk_name}")

    with tarfile.open(fpk_path, mode="w:gz", format=tarfile.PAX_FORMAT) as fpk_tar:
        # 1. app.tgz
        t_app = tarfile.TarInfo(name="app.tgz")
        t_app.size = len(app_tgz_bytes)
        t_app.mode = 0o644
        t_app.uid = 0
        t_app.gid = 0
        t_app.uname = "root"
        t_app.gname = "root"
        fpk_tar.addfile(t_app, io.BytesIO(app_tgz_bytes))

        # 2. cmd 目录
        cmd_dir = os.path.join(FPK_DIR, "cmd")
        if os.path.exists(cmd_dir):
            fpk_tar.add(cmd_dir, arcname="cmd", filter=make_tarinfo_filter())

        # 3. config 目录
        cfg_dir = os.path.join(FPK_DIR, "config")
        if os.path.exists(cfg_dir):
            fpk_tar.add(cfg_dir, arcname="config", filter=make_tarinfo_filter())

        # 4. 图标与描述文件
        for fn in ["ICON.PNG", "ICON_256.PNG", "manifest"]:
            p = os.path.join(FPK_DIR, fn)
            if os.path.exists(p):
                fpk_tar.add(p, arcname=fn, filter=make_tarinfo_filter())

        # 5. wizard 目录
        wiz_dir = os.path.join(FPK_DIR, "wizard")
        if os.path.exists(wiz_dir):
            fpk_tar.add(wiz_dir, arcname="wizard", filter=make_tarinfo_filter())

    shutil.copy2(fpk_path, alt_fpk_path)
    file_size = os.path.getsize(fpk_path)
    print(f"==> 6. 打包完成!")
    print(f"  安装包路径: {fpk_path}")
    print(f"  文件大小: {file_size} 字节 ({file_size / 1024:.1f} KB)")


if __name__ == "__main__":
    build()
