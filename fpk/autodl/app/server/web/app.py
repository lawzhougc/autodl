#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gy_autodl Web 控制台 — 登记追剧/电影, 查看任务进度, 手动操作。
依赖 gy_autodl.py (上一级目录), 复用其 Jackett/光鸭/字幕链路。
端口 8770, 仅监听局域网。"""
import json, os, re, sys, time, threading, subprocess, traceback, glob
import urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
# fpk 安装时用户数据在 TRIM_PKGVAR, 脚本模式在代码目录
_VAR = os.environ.get("TRIM_PKGVAR", "")
DATA = _VAR if (_VAR and os.path.isdir(_VAR)) else BASE
import gy_autodl as g  # noqa: E402

CONFIG_KEYS = ["batch_size", "batch_interval_hours", "backfill_enabled",
               "auto_download_new", "max_submit_per_run", "wanted_quality",
               "search_interval_hours",
               # 路径设置
               "media_root", "cloud_root",
               # 资源筛选偏好
               "quality_fallback", "prefer_format", "allow_format",
               "size_min_gb", "size_max_gb", "prefer_codec", "prefer_source", "hdr_prefer",
               "wanted_lang",
               # 服务与依赖配置
               "jackett_url", "jackett_apikey", "jackett_config"]

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 8770
WISH_F = os.path.join(DATA, "wishlist.json")   # {key: {name,kind,added,note}}

# ----------------- 后台搜索任务管理 (避免浏览器关闭/断连导致搜索失效) -----------------
_SEARCH_LOCK = threading.Lock()
_SEARCH_TASK = {
    "id": 0,
    "q": "",
    "status": "idle",  # idle | running | done | error
    "start_time": 0,
    "finish_time": 0,
    "data": None,
    "error": None
}

def _bg_search_worker(task_id, q_orig):
    global _SEARCH_TASK
    try:
        q = q_orig
        orig = q
        trans, meta = g.cn2en(q)
        if trans != q: q = trans
        kind = "tv" if g.is_tv_query(q) else "movie"
        cat = 5000 if kind == "tv" else 2000
        results = g.jackett_search(q, category=cat)
        pick = g.best_for_episode if kind == "tv" else g.best_for_movie
        # 2160p 去重列表 + 推荐
        q4k = []
        for r in results:
            p = g.parse_title(r["title"])
            if p["quality"] == "2160p" and not p["bad"]:
                r["_p"] = p; q4k.append(r)
        q4k.sort(key=g.rank_key)
        seen, uniq = set(), []
        for r in q4k:
            sig = re.sub(r"[^a-z0-9]", "", r["title"].lower())[:35]
            if sig in seen: continue
            seen.add(sig); uniq.append(r)
        items = [{"title": r["title"], "seeders": r["seeders"], "size": r["size"],
                  "size_gb": round(r["size"] / (g.MB * 1024), 1), "idx": r["idx"],
                  "magnet_url": r.get("magnet") or r.get("link") or "",
                  "has_magnet": bool(r.get("magnet")),
                  "infohash": r.get("infohash") or "",
                  "hdr": bool(r.get("_p", {}).get("hdr")),
                  "codec": r.get("_p", {}).get("codec") or "",
                  "quality": r.get("_p", {}).get("quality") or "2160p",
                  "langs": sorted(r["_p"].get("langs") or [])}
                 for r in uniq[:25]]
        best = pick(results, g.CFG["wanted_quality"]) if kind == "movie" else \
            (pick(results, int(re.search(r"S(\d+)E(\d+)", q, re.I).group(1)),
                  int(re.search(r"S(\d+)E(\d+)", q, re.I).group(2)),
                  g.CFG["wanted_quality"]) if re.search(r"S(\d+)E(\d+)", q, re.I) else None)
        lo = sorted([r for r in results if g.parse_title(r["title"])["quality"] == "1080p" and not g.parse_title(r["title"])["bad"]],
                    key=lambda r: -r["seeders"])[:15]
        lo_items = [{"title": r["title"], "seeders": r["seeders"], "size": r["size"],
                     "size_gb": round(r["size"] / (g.MB * 1024), 1), "idx": r["idx"],
                     "magnet_url": r.get("magnet") or r.get("link") or "",
                     "has_magnet": bool(r.get("magnet")),
                     "infohash": r.get("infohash") or "",
                     "hdr": bool(g.parse_title(r["title"]).get("hdr")),
                     "codec": g.parse_title(r["title"]).get("codec") or "",
                     "quality": "1080p",
                     "langs": sorted(g.parse_title(r["title"]).get("langs") or [])} for r in lo]

        payload = {
            "kind": kind, "total": len(results), "q4k": items,
            "has_4k": bool(items),
            "best": best["title"] if best else None,
            "best_magnet": (best.get("magnet") or best.get("link") or "") if best else None,
            "best_infohash": (best.get("infohash") or "") if best else None,
            "lo1080": lo_items, "recognized": trans if trans != orig else None, "orig": orig,
            "meta": meta
        }
        with _SEARCH_LOCK:
            if _SEARCH_TASK["id"] == task_id:
                _SEARCH_TASK["status"] = "done"
                _SEARCH_TASK["finish_time"] = time.time()
                _SEARCH_TASK["data"] = payload
    except Exception as ex:
        traceback.print_exc()
        with _SEARCH_LOCK:
            if _SEARCH_TASK["id"] == task_id:
                _SEARCH_TASK["status"] = "error"
                _SEARCH_TASK["finish_time"] = time.time()
                _SEARCH_TASK["error"] = str(ex)

def start_search_task(q):
    global _SEARCH_TASK
    q = q.strip()
    with _SEARCH_LOCK:
        # 如果当前相同的词正在搜索中或刚搜索完(<5分钟)，直接复用
        now = time.time()
        if _SEARCH_TASK["q"] == q and _SEARCH_TASK["status"] == "running":
            return dict(_SEARCH_TASK)
        if _SEARCH_TASK["q"] == q and _SEARCH_TASK["status"] == "done" and (now - _SEARCH_TASK["finish_time"] < 300):
            return dict(_SEARCH_TASK)

        new_id = _SEARCH_TASK["id"] + 1
        _SEARCH_TASK = {
            "id": new_id,
            "q": q,
            "status": "running",
            "start_time": now,
            "finish_time": 0,
            "data": None,
            "error": None
        }
    t = threading.Thread(target=_bg_search_worker, args=(new_id, q), daemon=True)
    t.start()
    return dict(_SEARCH_TASK)

def get_search_task_status():
    with _SEARCH_LOCK:
        d = dict(_SEARCH_TASK)
        if d.get("data"):
            d["data"] = dict(d["data"])
        return d

_HEALTH_CACHE = {"time": 0, "data": None}

def check_service_health():
    global _HEALTH_CACHE
    now = time.time()
    if _HEALTH_CACHE["data"] and (now - _HEALTH_CACHE["time"] < 6):
        return _HEALTH_CACHE["data"]

    try:
        g.CFG.update(g.load_json(g.CFG_F, {}))
    except Exception:
        pass

    gy_ok, gy_msg = False, "未连接"
    try:
        gy_url = (g.CFG.get("guangya_url") or g.GY).rstrip("/") + "/api/user"
        d = g.http_json(gy_url, timeout=3)
        if d.get("is_logged_in") is True or not d.get("error"):
            gy_ok, gy_msg = True, "正常"
        else:
            gy_msg = "未登录"
    except Exception:
        gy_msg = "未连接"

    jt_ok, jt_msg = False, "未连接"
    jt_url = (g.CFG.get("jackett_url") or g.JT).rstrip("/")

    # 优先尝试使用 Jackett APIKey 探测官方 API
    key = None
    try:
        key = g.jt_key()
    except Exception:
        pass

    # 1. 如果有 APIKey，优先探测 Jackett API 接口 (轻量且直接检验 API 权限)
    if key:
        api_candidates = [
            f"{jt_url}/api/v2.0/server/config?apikey={urllib.parse.quote(key)}",
            f"{jt_url}/api/v2.0/indexers?configured=true&apikey={urllib.parse.quote(key)}"
        ]
        for test_url in api_candidates:
            try:
                req = urllib.request.Request(test_url, headers={"User-Agent": "autodl/health"})
                with urllib.request.urlopen(req, timeout=4) as r:
                    if r.status in (200, 204):
                        jt_ok, jt_msg = True, "正常"
                        break
            except urllib.error.HTTPError as he:
                if he.code in (200, 204):
                    jt_ok, jt_msg = True, "正常"
                    break
                elif he.code == 401:
                    jt_ok = False
                    jt_msg = "APIKey错误"
                    break
                elif he.code in (301, 302, 303, 307, 308, 403):
                    jt_ok = True
                    jt_msg = "正常"
                    break
            except Exception:
                pass

    # 2. 如果无 APIKey 或 API 探测未果，直接探测服务端口或 Web 页面
    if not jt_ok and jt_msg != "APIKey错误":
        web_candidates = [
            jt_url,
            f"{jt_url}/UI/Dashboard",
            f"{jt_url}/api/v2.0/server/config"
        ]
        for test_url in web_candidates:
            try:
                req = urllib.request.Request(test_url, headers={"User-Agent": "autodl/health"})
                with urllib.request.urlopen(req, timeout=4) as r:
                    if r.status in (200, 204, 301, 302, 303, 307, 308):
                        jt_ok, jt_msg = True, "正常"
                        break
            except urllib.error.HTTPError as he:
                # 即使返回 401/403/302 也说明端口已联通、服务正常响应中
                if he.code in (200, 204, 301, 302, 303, 307, 308, 401, 403):
                    jt_ok, jt_msg = True, "正常"
                    break
            except Exception:
                pass

    status = "normal" if (gy_ok and jt_ok) else "retry"
    summary = "Jackett & 云盘心跳正常" if (gy_ok and jt_ok) else "服务重试中..."
    if not jt_ok and gy_ok:
        summary = f"Jackett 连接异常: {jt_msg}"
    elif jt_ok and not gy_ok:
        summary = f"云盘连接异常: {gy_msg}"
    elif not jt_ok and not gy_ok:
        summary = "Jackett 与云盘均未连接"

    space_info = "空间充裕"
    try:
        import shutil
        mr = g.CFG.get("media_root")
        if mr and os.path.exists(mr):
            total, used, free = shutil.disk_usage(mr)
            free_gb = round(free / (1024**3), 1)
            space_info = f"剩余 {free_gb} GB"
    except Exception:
        pass

    data = {
        "ok": gy_ok and jt_ok,
        "status": status,
        "summary": summary,
        "storage": space_info,
        "guangya": {"ok": gy_ok, "msg": gy_msg},
        "jackett": {"ok": jt_ok, "msg": jt_msg},
        "daemon": {"ok": True, "msg": "运行中"},
        "time": now
    }
    _HEALTH_CACHE = {"time": now, "data": data}
    return data

def get_root_volumes():
    vols = []
    if sys.platform == "win32":
        import string
        from ctypes import windll
        try:
            bitmask = windll.kernel32.GetLogicalDrives()
            for letter in string.ascii_uppercase:
                if bitmask & 1:
                    p = f"{letter}:\\"
                    vols.append({"name": f"{letter}: 盘", "path": p})
                bitmask >>= 1
        except Exception:
            vols.append({"name": "C: 盘", "path": "C:\\"})
    else:
        # 飞牛 NAS / Linux 常见挂载卷
        candidates = sorted(glob.glob("/vol*") + glob.glob("/volume*"))
        for c in candidates:
            if os.path.isdir(c) and not os.path.basename(c).startswith("."):
                name = os.path.basename(c)
                vols.append({"name": f"存储空间 ({name})", "path": c})
        for m in ["/mnt", "/media", "/data", "/home"]:
            if os.path.isdir(m):
                vols.append({"name": m, "path": m})
        if not vols:
            vols.append({"name": "系统根目录 (/)", "path": "/"})
    return vols

def list_directories(req_path=""):
    req_path = (req_path or "").strip()
    curr_mr = (g.CFG.get("media_root") or "").strip()

    # 默认路径：优先当前配置的 media_root 或其父目录
    if not req_path:
        if curr_mr and os.path.isdir(curr_mr):
            req_path = os.path.abspath(curr_mr)
        elif curr_mr and os.path.isdir(os.path.dirname(curr_mr)):
            req_path = os.path.abspath(os.path.dirname(curr_mr))
        else:
            vols = get_root_volumes()
            req_path = vols[0]["path"] if vols else ("/" if sys.platform != "win32" else "C:\\")

    req_path = os.path.abspath(req_path)
    if not os.path.exists(req_path):
        return {
            "ok": False,
            "error": f"路径不存在: {req_path}",
            "current": req_path.replace("\\", "/") if sys.platform != "win32" else req_path,
            "dirs": [],
            "vols": get_root_volumes(),
            "configured": curr_mr
        }

    if not os.path.isdir(req_path):
        req_path = os.path.dirname(req_path)

    parent = os.path.dirname(req_path)
    if parent == req_path:
        parent = None

    dirs = []
    IGNORED = {
        "@appdata", "@eadir", "@snapshot", "@tmp", "lost+found",
        "proc", "sys", "dev", "run", "$recycle.bin", "system volume information"
    }

    try:
        with os.scandir(req_path) as it:
            for entry in it:
                try:
                    if entry.name.startswith("."):
                        continue
                    if entry.name.lower() in IGNORED:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        p = entry.path.replace("\\", "/") if sys.platform != "win32" else entry.path
                        dirs.append({"name": entry.name, "path": p})
                except (PermissionError, OSError):
                    continue
    except PermissionError:
        return {
            "ok": False,
            "error": f"无权限访问: {req_path}",
            "current": req_path.replace("\\", "/") if sys.platform != "win32" else req_path,
            "parent": (parent.replace("\\", "/") if sys.platform != "win32" else parent) if parent else None,
            "dirs": [],
            "vols": get_root_volumes(),
            "configured": curr_mr
        }
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "current": req_path.replace("\\", "/") if sys.platform != "win32" else req_path,
            "parent": (parent.replace("\\", "/") if sys.platform != "win32" else parent) if parent else None,
            "dirs": [],
            "vols": get_root_volumes(),
            "configured": curr_mr
        }

    dirs.sort(key=lambda x: x["name"].lower())

    norm_current = req_path.replace("\\", "/") if sys.platform != "win32" else req_path
    norm_parent = (parent.replace("\\", "/") if sys.platform != "win32" else parent) if parent else None

    return {
        "ok": True,
        "current": norm_current,
        "parent": norm_parent,
        "dirs": dirs,
        "vols": get_root_volumes(),
        "configured": curr_mr
    }

def create_directory(parent, name):
    parent = (parent or "").strip()
    name = (name or "").strip()
    if not parent or not name:
        return {"ok": False, "error": "父路径和文件夹名称不能为空"}
    if "/" in name or "\\" in name or name in (".", ".."):
        return {"ok": False, "error": "文件夹名称不能包含特殊路径字符"}
    target = os.path.join(parent, name)
    if os.path.exists(target):
        return {"ok": False, "error": "该文件夹已存在"}
    try:
        os.makedirs(target, exist_ok=True)
        return {"ok": True, "path": target.replace("\\", "/") if sys.platform != "win32" else target}
    except Exception as e:
        return {"ok": False, "error": f"创建失败: {e}"}

def wishes():
    return g.load_json(WISH_F, {})

def save_wishes(d):
    g.save_json(WISH_F, d)

def run_script(args, timeout=300):
    """跑 gy_autodl.py 子命令, 返回 (rc, output)"""
    r = subprocess.run([sys.executable, os.path.join(BASE, "gy_autodl.py")] + args,
                       capture_output=True, timeout=timeout, cwd=BASE)
    out = (r.stdout + b"\n" + r.stderr).decode("utf-8", "ignore")
    return r.returncode, out

def async_run(args, tag):
    """后台线程跑脚本(搜索/添加可能耗时1-2分钟), 结果写 jobs.log"""
    def worker():
        try:
            rc, out = run_script(args, timeout=600)
            line = f"{time.strftime('%m-%d %H:%M:%S')}\t{tag}\trc={rc}\t{out[-1500:]}"
        except Exception as e:
            line = f"{time.strftime('%m-%d %H:%M:%S')}\t{tag}\tERROR\t{e}"
        with open(os.path.join(DATA, "logs", "webjobs.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n" + ("-" * 60) + "\n")
    threading.Thread(target=worker, daemon=True).start()

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    # ---------- API ----------
    def do_GET(self):
        try:
            if self.path == "/" or self.path.startswith("/index"):
                return self._file("index.html", "text/html; charset=utf-8")
            if self.path == "/api/wishes":
                st = g.state()
                wl = wishes()
                # 补充每条心愿的进行中任务信息与等待状态
                for k, w in wl.items():
                    w["tasks"] = [{"label": t["label"], "stage": t["stage"]}
                                  for t in st["tasks"].values()
                                  if (t.get("kind") == "tv" and g.show_key(t.get("show") or "") == k)
                                  or (t.get("kind") == "movie" and (k in g.show_key(t.get("label") or "") or g.show_key(t.get("label") or "") in k))]
                    if k in st.get("waiting", {}):
                        w["waiting_note"] = st["waiting"][k]
                return self._json(200, wl)
            if self.path == "/api/shows":
                return self._json(200, g.shows())
            if self.path == "/api/config":
                st = g.state()
                due, why = g.search_due(st)
                d = {k: g.CFG.get(k) for k in CONFIG_KEYS}
                d["_search_due"] = due
                d["_search_note"] = why
                d["_last_search"] = st.get("last_search")
                return self._json(200, d)
            if self.path == "/api/waiting":
                return self._json(200, g.state().get("waiting", {}))
            if self.path == "/api/tasks":
                st = g.state()
                tasks = []
                trs = g.gy_transfers() if any(t.get("stage") == "nas" for t in st.get("tasks", {}).values()) else {}
                for k, t in st["tasks"].items():
                    e = {"key": k, "label": t.get("label"), "stage": t.get("stage"),
                         "kind": t.get("kind"), "added": t.get("added"), "error": t.get("error")}
                    if t.get("stage") == "nas":
                        nas_list = []
                        for x in t.get("nas", []):
                            tr = trs.get(str(x.get("nasTaskId")), {})
                            pct = tr.get("percent")
                            sp = tr.get("speed") or tr.get("downloadSpeed") or tr.get("rate")
                            sp_str = ""
                            if sp:
                                try:
                                    sp_val = float(sp)
                                    if sp_val > 1024 * 1024:
                                        sp_str = f"{round(sp_val / (1024 * 1024), 1)} MB/s"
                                    elif sp_val > 1024:
                                        sp_str = f"{round(sp_val / 1024, 1)} KB/s"
                                    elif sp_val > 0:
                                        sp_str = f"{int(sp_val)} B/s"
                                except Exception:
                                    sp_str = str(sp)
                            nas_list.append({
                                "file": os.path.basename(x["file"]),
                                "percent": pct,
                                "speed": sp_str
                            })
                        e["nas"] = nas_list
                    tasks.append(e)
                tasks.sort(key=lambda x: -(x.get("added") or 0))
                return self._json(200, tasks)
            if self.path == "/api/events":
                try:
                    with open(g.EV, encoding="utf-8") as f:
                        return self._json(200, f.readlines()[-40:])
                except Exception:
                    return self._json(200, [])
            if self.path.startswith("/api/logs"):
                # /api/logs?src=events|run|cron|web&lines=200
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                src = (qs.get("src", ["events"])[0] or "events").lower()
                try:
                    lines = max(10, min(2000, int(qs.get("lines", ["200"])[0])))
                except ValueError:
                    lines = 200
                files = {
                    "events": g.EV,
                    "run": g.LOG,
                    "cron": os.path.join(g.DATA, "logs", "cron.log"),
                    "web": os.path.join(g.DATA, "logs", "webjobs.log"),
                }
                path = files.get(src)
                if not path:
                    return self._json(400, {"error": f"unknown src: {src}"})
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        tail = f.readlines()[-lines:]
                    return self._json(200, {"src": src, "lines": len(tail),
                                            "file": path, "tail": [l.rstrip("\n") for l in tail]})
                except FileNotFoundError:
                    return self._json(200, {"src": src, "lines": 0, "file": path, "tail": []})
                except Exception as ex:
                    return self._json(200, {"src": src, "lines": 0, "file": path,
                                            "tail": [], "error": str(ex)})
            if self.path == "/api/search/status":
                return self._json(200, get_search_task_status())
            if self.path == "/api/health":
                return self._json(200, check_service_health())
            if self.path.startswith("/api/fs/dirs"):
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                req_path = (qs.get("path", [""])[0] or "").strip()
                return self._json(200, list_directories(req_path))
            if self.path.startswith("/api/search?"):
                q = urllib.parse.unquote(self.path.split("q=", 1)[1])
                t = start_search_task(q)
                # 兼容旧逻辑：如果直接完成(缓存)，直接给 data；否则返回当前状态
                return self._json(200, t)
            return self._json(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            return self._json(500, {"error": str(e)})

    def do_POST(self):
        try:
            body = self._body()
            if self.path == "/api/wish":          # 登记心愿
                name = (body.get("name") or "").strip()
                kind = body.get("kind") or "auto"
                if not name: return self._json(400, {"error": "名称不能为空"})
                key = g.show_key(name)
                wl = wishes()
                if key in wl: return self._json(400, {"error": "已在清单中"})
                wl[key] = {"name": name, "kind": kind, "added": time.time(),
                           "note": body.get("note") or ""}
                save_wishes(wl)
                return self._json(200, {"ok": True, "key": key})
            elif self.path == "/api/wish/delete":
                wl = wishes(); wl.pop(body.get("key"), None); save_wishes(wl)
                return self._json(200, {"ok": True})
            elif self.path == "/api/addshow":     # 登记追剧
                args = ["addshow", body["name"]]
                if body.get("last"): args += ["--last", body["last"]]
                rc, out = run_script(args)
                return self._json(200, {"rc": rc, "out": out.strip()})
            elif self.path == "/api/add":         # 立即下载(单集/电影)
                name = body["name"]
                cmd = ["add", name]
                if body.get("allow1080"):
                    cmd.append("--allow1080")
                if body.get("exact_title"):
                    cmd.extend(["--exact-title", body["exact_title"]])
                if body.get("magnet"):
                    cmd.extend(["--magnet", body["magnet"]])
                if body.get("infohash"):
                    cmd.extend(["--infohash", body["infohash"]])
                async_run(cmd, f"add {name}")
                return self._json(200, {"ok": True, "msg": "已提交后台下载"})
            elif self.path == "/api/search":
                q = (body.get("q") or "").strip()
                if not q: return self._json(400, {"error": "empty"})
                t = start_search_task(q)
                return self._json(200, t)
            elif self.path == "/api/config":      # 保存下载节奏设置(用已读的 body)
                updates = {}
                for k, v in body.items():
                    if k not in CONFIG_KEYS: continue
                    if k in ("batch_size", "max_submit_per_run"):
                        v = max(1, min(20, int(v)))
                    elif k in ("batch_interval_hours", "search_interval_hours"):
                        v = max(0.0, min(720.0, float(v)))
                    elif k in ("backfill_enabled", "auto_download_new", "hdr_prefer"):
                        v = bool(v)
                    elif k in ("size_min_gb", "size_max_gb"):
                        v = max(0.0, min(2000.0, float(v)))
                    elif k in ("quality_fallback", "prefer_format", "allow_format",
                               "prefer_codec", "prefer_source"):
                        # 前端传逗号分隔字符串或数组
                        if isinstance(v, str):
                            v = [x.strip().lower() for x in v.split(",") if x.strip()]
                        else:
                            v = [str(x).strip().lower() for x in (v or []) if str(x).strip()]
                        if k == "quality_fallback":
                            v = [x for x in v if x in ("2160p", "1080p", "other")]
                    elif k in ("media_root", "cloud_root"):
                        v = str(v).strip()
                        if k == "media_root" and v and not v.startswith("/") and not (len(v) > 1 and v[1] == ":"):
                            v = "/" + v
                        if not v: continue
                    updates[k] = v
                g.save_cfg(updates)
                return self._json(200, {"ok": True, "cfg": {k: g.CFG.get(k) for k in CONFIG_KEYS}})
            elif self.path == "/api/fs/mkdir":
                parent = (body.get("parent") or "").strip()
                name = (body.get("name") or "").strip()
                res = create_directory(parent, name)
                return self._json(200 if res.get("ok") else 400, res)
            elif self.path == "/api/task/delete":
                key = (body.get("key") or "").strip()
                if not key: return self._json(400, {"error": "缺少 key"})
                ok, msg = g.delete_task(key)
                return self._json(200 if ok else 400, {"ok": ok, "msg": msg})
            elif self.path == "/api/rmshow":
                rc, out = run_script(["rmshow", body["name"]])
                return self._json(200, {"rc": rc, "out": out.strip()})
            elif self.path == "/api/run":         # 手动触发主循环
                async_run(["run"], "manual run")
                return self._json(200, {"ok": True, "msg": "已触发 run"})
            elif self.path == "/api/poll":
                async_run(["poll"], "manual poll")
                return self._json(200, {"ok": True, "msg": "已触发 poll"})
            else:
                return self._json(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            return self._json(500, {"error": str(e)})

    def _file(self, name, ctype):
        p = os.path.join(WEB_DIR, name)
        with open(p, "rb") as f:
            b = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

if __name__ == "__main__":
    import socket
    s = socket.socket()
    try:  # 已有实例在跑则退出(cron 保活幂等)
        s.connect(("127.0.0.1", PORT)); sys.exit(0)
    except Exception:
        pass
    finally:
        s.close()
    os.makedirs(os.path.join(DATA, "logs"), exist_ok=True)

    # 启动后台推进轮询线程: 每 30 秒自动执行一次 gy_autodl poll (推进 cloud -> nas -> 字幕/完成)
    def _bg_poll_worker():
        while True:
            try:
                time.sleep(30)
                run_script(["poll"], timeout=120)
            except Exception:
                pass
    t_poll = threading.Thread(target=_bg_poll_worker, daemon=True)
    t_poll.start()

    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print(f"gy_autodl web on :{PORT}", flush=True)
    srv.serve_forever()
