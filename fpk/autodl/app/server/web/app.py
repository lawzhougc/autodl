#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gy_autodl Web 控制台 — 登记追剧/电影, 查看任务进度, 手动操作。
依赖 gy_autodl.py (上一级目录), 复用其 Jackett/光鸭/字幕链路。
端口 8770, 仅监听局域网。"""
import json, os, re, sys, time, threading, subprocess, traceback
import urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import gy_autodl as g  # noqa: E402

CONFIG_KEYS = ["batch_size", "batch_interval_hours", "backfill_enabled",
               "auto_download_new", "max_submit_per_run", "wanted_quality",
               "search_interval_hours",
               # 资源筛选偏好
               "quality_fallback", "prefer_format", "allow_format",
               "size_min_gb", "size_max_gb", "prefer_codec", "prefer_source", "hdr_prefer",
               "wanted_lang"]

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 8770
WISH_F = os.path.join(BASE, "wishlist.json")   # {key: {name,kind,added,note}}

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
        with open(os.path.join(BASE, "logs", "webjobs.log"), "a", encoding="utf-8") as f:
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
                # 补充每条心愿的进行中任务信息
                for k, w in wl.items():
                    w["tasks"] = [{"label": t["label"], "stage": t["stage"]}
                                  for t in st["tasks"].values() if t.get("kind") == "tv"
                                  and g.show_key(t.get("show") or "") == k]
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
                for k, t in st["tasks"].items():
                    e = {"key": k, "label": t.get("label"), "stage": t.get("stage"),
                         "kind": t.get("kind"), "added": t.get("added"), "error": t.get("error")}
                    if t.get("stage") == "nas":
                        trs = g.gy_transfers()
                        e["nas"] = [{"file": os.path.basename(x["file"]),
                                     "percent": trs.get(str(x.get("nasTaskId")), {}).get("percent")}
                                    for x in t.get("nas", [])]
                    tasks.append(e)
                tasks.sort(key=lambda x: -(x.get("added") or 0))
                return self._json(200, tasks)
            if self.path == "/api/events":
                try:
                    with open(g.EV, encoding="utf-8") as f:
                        return self._json(200, f.readlines()[-40:])
                except Exception:
                    return self._json(200, [])
            if self.path.startswith("/api/search?"):
                q = urllib.parse.unquote(self.path.split("q=", 1)[1])
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
                          "magnet": bool(r["magnet"]),
                          "langs": sorted(r["_p"].get("langs") or [])}
                         for r in uniq[:20]]
                best = pick(results, g.CFG["wanted_quality"]) if kind == "movie" else \
                    (pick(results, int(re.search(r"S(\d+)E(\d+)", q, re.I).group(1)),
                          int(re.search(r"S(\d+)E(\d+)", q, re.I).group(2)),
                          g.CFG["wanted_quality"]) if re.search(r"S(\d+)E(\d+)", q, re.I) else None)
                lo = sorted([r for r in results if g.parse_title(r["title"])["quality"] == "1080p" and not g.parse_title(r["title"])["bad"]],
                            key=lambda r: -r["seeders"])[:8]
                lo_items = [{"title": r["title"], "seeders": r["seeders"],
                             "size_gb": round(r["size"] / (g.MB * 1024), 1), "idx": r["idx"],
                             "langs": sorted(g.parse_title(r["title"]).get("langs") or [])} for r in lo]
                return self._json(200, {"kind": kind, "total": len(results), "q4k": items,
                                        "best": best["title"] if best else None,
                                        "lo1080": lo_items, "recognized": trans if trans != orig else None, "orig": orig,
                                        "meta": meta})
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
                async_run(["add", name] + (["--allow1080"] if body.get("allow1080") else []), f"add {name}")
                return self._json(200, {"ok": True, "msg": "已后台提交, 1-2分钟后在任务里查看"})
            elif self.path == "/api/search":
                q = body.get("q") or ""
                if not q: return self._json(400, {"error": "empty"})
                # 搜索同步返回(前端有 loading)
                self._json(200, {"ok": True})  # 先占位, 真正搜索走 GET /api/search
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
                    updates[k] = v
                g.save_cfg(updates)
                return self._json(200, {"ok": True, "cfg": {k: g.CFG.get(k) for k in CONFIG_KEYS}})
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
    os.makedirs(os.path.join(BASE, "logs"), exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print(f"gy_autodl web on :{PORT}", flush=True)
    srv.serve_forever()
