#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞牛 NAS (fnOS) FPK 自动打包工具
优先调用官方原生 fnpack 工具进行打包；若未安装则自动回退至标准 TAR/GZ 构建器。
生成符合飞牛应用中心规范的 autodl-{version}.fpk 安装包。
"""

import os
import shutil
import subprocess
import tarfile
import io
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FPK_DIR = os.path.join(ROOT, "fpk", "autodl")
SERVER_DIR = os.path.join(FPK_DIR, "app", "server")


def sync_files():
    print("==> 1. 同步最新源码到打包目录")
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
    return "0.2.13"


def find_fnpack():
    """查找官方原生 fnpack 可执行文件"""
    candidates = [
        shutil.which("fnpack"),
        shutil.which("fnpack.exe"),
        os.path.join(ROOT, "fnpack.exe"),
        os.path.join(ROOT, "fpk", "fnpack.exe"),
        r"C:\Users\law\.gemini\antigravity\bin\fnpack.exe",
        r"C:\Users\law\AppData\Local\Programs\Git\cmd\fnpack.exe",
        "/usr/local/bin/fnpack",
        "/usr/bin/fnpack"
    ]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def make_tarinfo_filter():
    def filter_func(tarinfo):
        tarinfo.uid = 0
        tarinfo.gid = 0
        tarinfo.uname = "root"
        tarinfo.gname = "root"

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


def build_with_native_fnpack(fnpack_bin, ver):
    print(f"==> 4. 使用飞牛官方原生 fnpack ({fnpack_bin}) 打包...")
    # 在 ROOT 目录下执行 fnpack build -d fpk/autodl
    cmd = [fnpack_bin, "build", "-d", os.path.relpath(FPK_DIR, ROOT)]
    res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"fnpack 执行出错:\n{res.stderr}\n回退至标准 TAR 构建器...")
        return False
    print(res.stdout.strip())

    # fnpack 默认会在工作目录生成 autodl.fpk
    default_out = os.path.join(ROOT, "autodl.fpk")
    target_out = os.path.join(ROOT, f"autodl-{ver}.fpk")
    if os.path.exists(default_out):
        shutil.copy2(default_out, target_out)
        shutil.copy2(default_out, os.path.join(FPK_DIR, "autodl.fpk"))
        file_size = os.path.getsize(target_out)
        print(f"==> 5. 官方原生 fnpack 打包成功!")
        print(f"  安装包路径: {target_out}")
        print(f"  文件大小: {file_size} 字节 ({file_size / 1024:.1f} KB)")
        return True
    return False


def build_with_internal_tar(ver):
    print("==> 4. 使用内置规范构建器生成 app.tgz 与 fpk...")
    app_dir = os.path.join(FPK_DIR, "app")
    app_tgz_io = io.BytesIO()

    with tarfile.open(fileobj=app_tgz_io, mode="w:gz", format=tarfile.PAX_FORMAT) as app_tar:
        entries = sorted(os.listdir(app_dir))
        for entry in entries:
            entry_path = os.path.join(app_dir, entry)
            app_tar.add(entry_path, arcname=entry, filter=make_tarinfo_filter())

    app_tgz_bytes = app_tgz_io.getvalue()
    fpk_name = f"autodl-{ver}.fpk"
    fpk_path = os.path.join(ROOT, fpk_name)
    alt_fpk_path = os.path.join(FPK_DIR, "autodl.fpk")

    with tarfile.open(fpk_path, mode="w:gz", format=tarfile.PAX_FORMAT) as fpk_tar:
        t_app = tarfile.TarInfo(name="app.tgz")
        t_app.size = len(app_tgz_bytes)
        t_app.mode = 0o644
        t_app.uid = 0
        t_app.gid = 0
        t_app.uname = "root"
        t_app.gname = "root"
        fpk_tar.addfile(t_app, io.BytesIO(app_tgz_bytes))

        for sub_dir in ["cmd", "config", "wizard"]:
            d_path = os.path.join(FPK_DIR, sub_dir)
            if os.path.exists(d_path):
                fpk_tar.add(d_path, arcname=sub_dir, filter=make_tarinfo_filter())

        for fn in ["ICON.PNG", "ICON_256.PNG", "manifest"]:
            p = os.path.join(FPK_DIR, fn)
            if os.path.exists(p):
                fpk_tar.add(p, arcname=fn, filter=make_tarinfo_filter())

    shutil.copy2(fpk_path, alt_fpk_path)
    file_size = os.path.getsize(fpk_path)
    print(f"==> 5. 打包完成!")
    print(f"  安装包路径: {fpk_path}")
    print(f"  文件大小: {file_size} 字节 ({file_size / 1024:.1f} KB)")


def build():
    sync_files()

    print("==> 2. 检查与规范换行符 (LF)")
    for root, _, files in os.walk(FPK_DIR):
        for f in files:
            p = os.path.join(root, f)
            ensure_lf(p)

    ver = get_version()
    print(f"==> 3. 读取应用版本: v{ver}")

    fnpack_bin = find_fnpack()
    if fnpack_bin:
        success = build_with_native_fnpack(fnpack_bin, ver)
        if not success:
            build_with_internal_tar(ver)
    else:
        build_with_internal_tar(ver)


if __name__ == "__main__":
    build()
