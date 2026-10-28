#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gy_autodl — fnOS 自动追剧/电影下载工作流
链路: Jackett(:9117) 搜索 → 光鸭网盘(:8998) 云下载 → 下载到 NAS(/vol3/1000/媒体库2) → 字幕
用法:
  search <关键词>              搜索资源(展示,不下载)
  add <标题> [--allow1080]     一次性下载(剧集单集或电影), 无4K时退出码2并列出候选
  addshow <剧名> --last SxxEyy 登记追剧
  rmshow <剧名> / shows        管理/列出追剧
  run [--dry] [--search-only]  cron 主循环: 云任务推进→NAS下载→字幕→搜新集
  poll                         只推进已有任务, 不搜新集
  status                       总览
  sub <视频文件>               手动补字幕
配置: config.json / shows.json / state.json (与本文件同目录)
"""
import argparse, fcntl, html as html_mod, io, json, os, re, socket, subprocess, sys, time, zipfile
import urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
# fpk 安装时: 代码在 ${TRIM_APPDEST}/server, 用户数据在 ${TRIM_PKGVAR}
# 脚本模式时: 配置与状态就在脚本同目录
_VAR = os.environ.get("TRIM_PKGVAR", "")
DATA = _VAR if (_VAR and os.path.isdir(_VAR)) else BASE
GY = "http://127.0.0.1:8998"
JT = "http://127.0.0.1:9117"
LOG = os.path.join(DATA, "logs", "run.log")
EV = os.path.join(DATA, "logs", "events.log")
MB = 1024 * 1024

DEFAULT_CFG = {
    "jackett_url": "http://127.0.0.1:9117",
    "jackett_apikey": "",
    "jackett_config": "",
    "indexers": ["thepiratebay", "knaben", "therarbg", "limetorrents", "torrentgalaxyclone", "uindex"],
    "native_sources": ["bt4g"],   # 不走 Jackett 的原生源: bt4g(RSS接口)
    "media_root": "/vol3/1000/媒体库2",
    "cloud_root": "自动下载",
    "min_video_mb": 300,
    "max_submit_per_run": 3,
    # ---- 下载节奏(针对已上线/批量补集) ----
    "batch_size": 2,           # 每次最多连续追下几集
    "batch_interval_hours": 6, # 攒批间隔: 这批下完后隔多久再下下一批
    "backfill_enabled": True,  # 是否允许连续补集(关闭=每次只追最新已上线的一集)
    "auto_download_new": True, # 未上线资源: 抓到符合条件即自动下载
    # ---- 追剧搜索周期 ----
    "search_interval_hours": 24,  # 多少小时搜一次新集(0=每次 cron 都搜)
    "search_at_hour": 13,         # 每天几点开始(仅 search_interval_hours=24 且整数天时生效)
    # ---- 资源筛选偏好 ----
    "wanted_quality": "2160p",       # 首选清晰度: 2160p / 1080p / any
    "quality_fallback": [],          # 首选没有时的降级顺序, 如 ["1080p"]; 空=不降级, 询问用户
    "prefer_format": ["mkv"],        # 首选容器格式(高到低), 空=不限
    "allow_format": [],              # 允许的格式白名单, 空=不限制; 如 ["mkv","mp4"]
    "size_min_gb": 0,                # 单片/单集大小下限(GB), 0=不限
    "size_max_gb": 0,                # 单片/单集大小上限(GB), 0=不限
    "prefer_codec": [],              # 编码偏好, 如 ["hevc","av1","h264"]; 空=不限
    "prefer_source": [],             # 片源偏好, 如 ["remux","bluray","web"]; 空=用默认权重
    "hdr_prefer": True,              # 是否优先 HDR/DV 版本
    # ---- 语种偏好 ----
    "wanted_lang": "en_prefer",      # en_prefer=只用英语版(外语版仅兜底) / en_only=排除含外语标签 / any=不限
    "search_timeout": 45,
    "cloud_stale_hours": 72,
    "subtitle_langs": ["zh"],
}

def log(msg):
    line = f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f: f.write(line + "\n")
    except Exception: pass

def event(msg):
    log("EVENT " + msg)
    try:
        with open(EV, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{msg}\n")
    except Exception: pass

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except Exception: return default

def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)

CFG_F = os.path.join(DATA, "config.json")
CFG = dict(DEFAULT_CFG, **load_json(CFG_F, {}))

def save_cfg(updates):
    """更新并落盘 config.json, 同时刷新内存 CFG"""
    cur = load_json(CFG_F, {})
    cur.update(updates or {})
    with open(CFG_F, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=1)
    CFG.update(updates or {})
    return CFG
SHOWS_F = os.path.join(DATA, "shows.json")
STATE_F = os.path.join(DATA, "state.json")

# ---------------------------------------------------------------- HTTP
class MagnetRedirectHandler(urllib.request.HTTPRedirectHandler):
    """当上游 301/302 重定向到 magnet: 协议时阻止抛出异常，以便捕获磁链"""
    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        if newurl and newurl.startswith("magnet:"):
            return None
        return super().redirect_request(req, fp, code, msg, hdrs, newurl)

def http(url, data=None, headers=None, timeout=30, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def http_json(url, data=None, headers=None, timeout=30, method=None):
    return json.loads(http(url, data, headers, timeout, method).decode("utf-8"))

def gy_get(path, timeout=30):  return http_json(GY + path, timeout=timeout)
def gy_post(path, body, timeout=60):
    return http_json(GY + path, json.dumps(body).encode(), {"Content-Type": "application/json"}, timeout)

# ---------------------------------------------------------------- 光鸭云盘
def gy_list(pid):
    d = gy_get(f"/api/files?parentId={urllib.parse.quote(str(pid))}")
    items = d.get("list") or (d.get("data") or {}).get("list") or []
    return [i for i in items if i.get("fileName")]

def is_file_entry(e):  # dirType=1/有ext或gcid → 文件; 否则目录
    return bool(e.get("dirType") == 1 or e.get("ext") or e.get("gcid"))

def gy_find(name, pid):
    """按名找目录/文件。注意: 云盘目录条目 dirType 也=1, 不能用 is_file_entry 区分"""
    for e in gy_list(pid):
        if e.get("fileName", "").strip().lower() == name.strip().lower() and not (e.get("ext") or e.get("gcid")):
            return e["fileId"]
    return None

def gy_ensure_path(names, root=""):
    pid = root
    for n in names:
        fid = gy_find(n, pid)
        if not fid:
            gy_post("/api/create-folder", {"name": n, "parentId": pid}, timeout=30)
            fid = gy_find(n, pid)
            if not fid: raise RuntimeError(f"网盘建目录失败: {n}")
        pid = fid
    return pid

def gy_resolve(url):
    return gy_post("/api/cloud-add/resolve", {"url": url}, timeout=90)

def gy_resolve_torrent(tbytes, filename="t.torrent"):
    b = "----gyautodl" + str(int(time.time()))
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"torrent\"; filename=\"{filename}\"\r\n"
            f"Content-Type: application/x-bittorrent\r\n\r\n").encode() + tbytes + f"\r\n--{b}--\r\n".encode()
    return http_json(GY + "/api/cloud-add/resolve", body,
                     {"Content-Type": f"multipart/form-data; boundary={b}"}, timeout=90)

def gy_add_magnet(url, parent_id, file_indexes=None, new_name=None):
    body = {"url": url, "parentId": parent_id or ""}
    if file_indexes: body["fileIndexes"] = file_indexes
    if new_name: body["newName"] = new_name[:80]
    r = gy_post("/api/cloud-add/magnet", body, timeout=90)
    tid = (r.get("data") or {}).get("taskId")
    if not tid:
        raise RuntimeError(f"云任务创建失败: {json.dumps(r, ensure_ascii=False)[:300]}")
    return tid

def gy_cloud_tasks(pages=2):
    out = []
    for p in range(pages):
        try:
            d = gy_get(f"/api/cloud-add/tasks?page={p}&pageSize=100", timeout=60)
            lst = d.get("list") or (d.get("data") or {}).get("list") or []
            out += lst
            if len(lst) < 100: break
        except Exception as e:
            log(f"获取云任务列表第 {p} 页异常: {e}")
            break
    return out

def gy_download_url(fid): return gy_get(f"/api/download-url?fileId={fid}", timeout=30).get("signedURL")

def gy_nas_start(fid, fname, target_dir):
    return gy_post("/api/download-task", {"fileId": fid, "fileName": fname, "targetDir": target_dir}, timeout=60)["taskId"]

def gy_cloud_task_delete(task_id, delete_file=False):
    """从光鸭云任务列表中删除离线任务"""
    try:
        return gy_post("/api/cloud-add/delete", {"taskId": str(task_id), "deleteFile": bool(delete_file)}, timeout=15)
    except Exception as e:
        log(f"删除光鸭云任务 {task_id} 异常: {e}")
        return None

def gy_transfer_delete(task_id, delete_file=False):
    """从光鸭 NAS 下载传输列表中删除记录"""
    try:
        return gy_post("/api/transfers/delete", {"taskId": str(task_id), "deleteFile": bool(delete_file)}, timeout=15)
    except Exception as e:
        log(f"删除光鸭传输任务 {task_id} 异常: {e}")
        return None

def gy_transfers(): return gy_get("/api/transfers", timeout=30)

# ---------------------------------------------------------------- Jackett
def find_jackett_config():
    """多路径自动发现 Jackett 的 ServerConfig.json 文件"""
    env_cfg = os.environ.get("JACKETT_CONFIG")
    if env_cfg and os.path.isfile(env_cfg):
        return env_cfg

    cfg_val = CFG.get("jackett_config", "")
    if cfg_val and os.path.isfile(cfg_val):
        return cfg_val

    # 1. 扫描各存储卷 /vol*/@appdata/jackett/data/ServerConfig.json
    import glob
    for c in sorted(glob.glob("/vol*/@appdata/jackett/data/ServerConfig.json")):
        if os.path.isfile(c):
            return c

    # 2. 扫描 /volume*/@appdata/jackett/data/ServerConfig.json
    for c in sorted(glob.glob("/volume*/@appdata/jackett/data/ServerConfig.json")):
        if os.path.isfile(c):
            return c

    # 3. 常见 Linux 路径
    for p in [
        "/var/lib/jackett/data/ServerConfig.json",
        "/var/lib/jackett/ServerConfig.json",
        os.path.expanduser("~/.config/Jackett/ServerConfig.json"),
        "/root/.config/Jackett/ServerConfig.json",
        "/home/jackett/.config/Jackett/ServerConfig.json",
    ]:
        if os.path.isfile(p):
            return p

    # 4. 从运行中进程动态查找
    try:
        out = subprocess.check_output(["ps", "-eo", "args"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            if "jackett" in line.lower() and "--DataFolder" in line:
                parts = line.split()
                for i, arg in enumerate(parts):
                    if arg == "--DataFolder" and i + 1 < len(parts):
                        cfg = os.path.join(parts[i+1], "ServerConfig.json")
                        if os.path.isfile(cfg):
                            return cfg
    except Exception:
        pass

    return None

def jt_key():
    """获取 Jackett APIKey：配置/环境变量优先，否则从 ServerConfig.json 自动读取"""
    val = (CFG.get("jackett_apikey") or "").strip()
    if val:
        return val

    env_key = os.environ.get("JACKETT_APIKEY", "").strip()
    if env_key:
        return env_key

    cfg_file = find_jackett_config()
    if cfg_file and os.path.isfile(cfg_file):
        try:
            with open(cfg_file, encoding="utf-8") as f:
                k = json.load(f).get("APIKey")
                if k:
                    return k.strip()
        except Exception as e:
            log(f"读取 Jackett 配置文件 {cfg_file} 失败: {e}")

    raise RuntimeError(
        "未找到 Jackett APIKey。请确认 Jackett 已安装并启动，"
        "或在控制台「设置」中填写 Jackett APIKey，"
        "或设置环境变量 JACKETT_CONFIG 指向 ServerConfig.json"
    )

def jackett_search(query, category=None):
    """并行搜索5索引器 → 标准化结果列表"""
    key = jt_key()
    jt_url = (CFG.get("jackett_url") or JT).rstrip("/")
    def one(idx):
        url = (f"{jt_url}/api/v2.0/indexers/{idx}/results?apikey={key}"
               + (f"&Category={category}" if category else "")
               + "&Query=" + urllib.parse.quote(query))
        try:
            with urllib.request.urlopen(url, timeout=CFG["search_timeout"] + 15) as r:
                return idx, json.load(r).get("Results") or []
        except Exception as e:
            return idx, f"{type(e).__name__}: {e}"
    out = []
    with ThreadPoolExecutor(min(5, len(CFG["indexers"]))) as ex:
        for idx, res in ex.map(one, CFG["indexers"]):
            if isinstance(res, str):
                log(f"  索引器 {idx} 失败: {res}"); continue
            for r in res:
                out.append({
                    "idx": idx, "title": r.get("Title") or "",
                    "size": int(r.get("Size") or 0), "seeders": int(r.get("Seeders") or 0),
                    "magnet": r.get("MagnetUri") or "", "link": r.get("Link") or "",
                    "infohash": (r.get("InfoHash") or "").upper(),
                    "pub": r.get("PublishDate") or "",
                })
    # 原生源(不走 Jackett): bt4g(走 CF 豁免的 RSS 接口)
    if "bt4g" in CFG.get("native_sources", []):
        try:
            out += bt4g_search(query)
        except Exception as e:
            log(f"  原生源 bt4g 失败: {type(e).__name__}: {e}")
    return out

# ---------------------------------------------------------------- 原生源
BT4G = "https://bt4gprx.com"

def bt4g_search(query):
    """bt4g 官方 RSS 接口: /search?q=X&page=rss (该路径不受 Cloudflare 挑战限制)"""
    url = f"{BT4G}/search?q={urllib.parse.quote(query)}&page=rss"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"})
    with urllib.request.urlopen(req, timeout=CFG["search_timeout"]) as r:
        xml = r.read().decode("utf-8", "ignore")
    out = []
    for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
        def tag(t):
            m = re.search(rf"<{t}[^>]*>(.*?)</{t}>", item, re.S)
            return html_mod.unescape(m.group(1).strip()) if m else ""
        title = tag("title")
        link = tag("link")
        m = re.search(r"btih:([A-Fa-f0-9]{40})", link)
        if not title or not m:
            continue
        desc = tag("description")
        dm = re.search(r"<br\s*/?>([0-9.]+)\s*(GB|MB|KB)", desc, re.I)
        size = 0
        if dm:
            mult = {"GB": 1024**3, "MB": 1024**2, "KB": 1024}[dm.group(2).upper()]
            size = int(float(dm.group(1)) * mult)
        # RSS 不含做种数 → 给 0, 排序时靠其他索引器补充
        out.append({"idx": "bt4g", "title": title, "size": size, "seeders": 0,
                    "magnet": link, "link": "", "infohash": m.group(1).upper(),
                    "pub": tag("pubDate")})
    return out

def to_magnet(res):
    """结果 → (magnet, files_hint) ; 仅有种子的先下载字节再resolve"""
    if res.get("magnet"):
        return res["magnet"], None
    if res.get("link"):
        opener = urllib.request.build_opener(MagnetRedirectHandler)
        try:
            req = urllib.request.Request(res["link"], headers={"User-Agent": "Mozilla/5.0"})
            with opener.open(req, timeout=40) as resp:
                raw = resp.read()
                d = gy_resolve_torrent(raw)
                ih = d.get("infohash") or res.get("infohash")
                if not ih: return None, None
                return f"magnet:?xt=urn:btih:{ih.upper()}", None
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location") or ""
            if e.code in (301, 302, 303, 307, 308) and loc.startswith("magnet:"):
                return loc, None
            raise
    return None, None

# ---------------------------------------------------------------- 解析
RE_EP = re.compile(r"\bS(\d{1,2})\s?E(\d{1,3})(?:\s?[-~]\s?E?(\d{1,3}))?\b", re.I)
RE_SEASON = re.compile(r"\bS(\d{1,2})(?![E\d])", re.I)
RE_BAD = re.compile(r"\b(cam|telesync|\bts\b|tc\b|screener|dvdscr|hdts|hdcam)\b", re.I)
RE_Q2160 = re.compile(r"2160|4k|uhd", re.I)
RE_Q1080 = re.compile(r"1080", re.I)
RE_ITA = re.compile(r"\b(ita|italian)\b|\bmultip[le]*\b", re.I)
# 语种识别: 明确标注的语种标签(资源名常见形态)
LANG_TAGS = {
    "eng": r"\b(eng|english)\b",
    "ita": r"\b(ita|italian)\b", "ger": r"\b(ger|german|deutsch)\b",
    "fre": r"\b(fre|french|vff|vfq)\b", "spa": r"\b(spa|spanish|castellano)\b",
    "rus": r"\b(rus|russian)\b", "jpn": r"\b(jpn|japanese)\b",
    "kor": r"\b(kor|korean)\b", "chi": r"\b(chi|chinese|mandarin|cantonese)\b",
    "multi": r"\b(multi|multiple|multip)\b",
}
RE_LANG = {k: re.compile(v, re.I) for k, v in LANG_TAGS.items()}

def parse_langs(t):
    """从标题解析语种标签集合; 无标签返回空集合(大多数英文资源不标语种)"""
    return {k for k, pat in RE_LANG.items() if pat.search(t)}

def lang_ok(langs):
    """语种过滤: 按配置决定是否只保留英语资源"""
    mode = (CFG.get("wanted_lang") or "en_prefer").lower()
    if mode in ("any", ""):
        return True
    others = langs - {"eng", "multi"}
    if mode == "en_only":
        # 严格: 有任何非英语语种标签就排除(未标语种视为英语资源, 放行)
        return not others
    if mode == "en_prefer":   # 默认: 只用英语版本, 非英语资源仅在无英语版时兜底
        return True
    return True

def lang_score(langs):
    """语种排序权重: 越小越优先"""
    mode = (CFG.get("wanted_lang") or "en_prefer").lower()
    if mode in ("any", ""):
        return 0
    others = langs - {"eng", "multi"}
    if not others:
        return 0        # 纯英语 / 未标语种 / 多语 → 第一梯队
    if mode == "en_only":
        return 9        # 严格模式下已被 lang_ok 挡掉
    return 1            # 含外语标签 → 降权(仅兜底)

RE_FMT = {"mkv": r"\b(mkv|matroska)\b", "mp4": r"\b(mp4|m4v)\b",
         "avi": r"\bavi\b", "ts": r"\b(m2ts|ts)\b", "iso": r"\biso\b"}
RE_CODEC = {"hevc": r"\b(h\.?265|hevc|x265)\b", "h264": r"\b(h\.?264|avc|x264)\b",
            "av1": r"\bav1\b", "xvid": r"\b(xvid|divx)\b"}

def parse_format(t):
    """从标题判断容器格式; 标题未标注时返回 None(需在 resolve 后用扩展名确认)"""
    for fmt, pat in RE_FMT.items():
        if re.search(pat, t, re.I): return fmt
    return None

def parse_codec(t):
    for c, pat in RE_CODEC.items():
        if re.search(pat, t, re.I): return c
    return None

def size_ok(gb):
    """单集/单片的体积(GB)是否在用户设定区间内"""
    if not gb: return True
    lo, hi = float(CFG.get("size_min_gb") or 0), float(CFG.get("size_max_gb") or 0)
    if lo and gb < lo: return False
    if hi and gb > hi: return False
    return True

def format_allowed(p):
    """格式白名单检查; 标题没标格式的先放行, 到 resolve 后再用扩展名确认"""
    allow = [x.lower() for x in (CFG.get("allow_format") or [])]
    if not allow: return True
    return p.get("fmt") is None or p["fmt"] in allow

def pref_score(p):
    """偏好加权: 返回越小越优先的元组"""
    fmts = [x.lower() for x in (CFG.get("prefer_format") or [])]
    fmt_rank = fmts.index(p["fmt"]) if (p.get("fmt") in fmts) else len(fmts)
    codecs = [x.lower() for x in (CFG.get("prefer_codec") or [])]
    codec_rank = codecs.index(p["codec"]) if (p.get("codec") in codecs) else len(codecs)
    srcs = [x.lower() for x in (CFG.get("prefer_source") or [])]
    if srcs:
        src = 0 if p["remux"] else (1 if p["bluray"] else (2 if p["web"] else 3))
        names = {0: "remux", 1: "bluray", 2: "web", 3: "other"}
        src_rank = srcs.index(names[src]) if names[src] in srcs else len(srcs)
        return (fmt_rank, codec_rank, src_rank)
    return (fmt_rank, codec_rank)

def parse_title(t):
    d = {"title": t, "ep": None, "ep_end": None, "season": None, "season_pack": None,
         "quality": None, "bad": bool(RE_BAD.search(t)), "ita": bool(RE_ITA.search(t)),
         "langs": parse_langs(t),
         "proper": bool(re.search(r"\b(repack|proper)\b", t, re.I)),
         "dv": bool(re.search(r"\b(dv|dovi|dolby.?vision)\b", t, re.I)),
         "hdr": bool(re.search(r"hdr", t, re.I)), "remux": bool(re.search(r"remux", t, re.I)),
         "bluray": bool(re.search(r"blu.?ray|bdrip|bdremux", t, re.I)),
         "web": bool(re.search(r"web[\s.\-]?(dl|rip)?", t, re.I)),
         "fmt": parse_format(t), "codec": parse_codec(t)}
    m = RE_EP.search(t)
    if m:
        d["season"], d["ep"] = int(m.group(1)), int(m.group(2))
        if m.group(3): d["ep_end"] = int(m.group(3))
    else:
        ms = RE_SEASON.search(t)
        if ms: d["season_pack"] = int(ms.group(1))
    if RE_Q2160.search(t): d["quality"] = "2160p"
    elif RE_Q1080.search(t): d["quality"] = "1080p"
    else: d["quality"] = "other"
    return d

def rank_key(r):
    p = r["_p"]
    src = 0 if p["remux"] else (1 if p["bluray"] else (2 if p["web"] else 3))
    dv = 0 if ((p["dv"] or p["hdr"]) and CFG.get("hdr_prefer", True)) else (1 if not (p["dv"] or p["hdr"]) else 2)
    return (lang_score(p.get("langs") or set()), not p["proper"], src) + \
           pref_score(p) + (dv, -r["seeders"], -r["size"])

def show_key(name): return re.sub(r"\s+", " ", name).strip().title()

def wanted_qualities():
    """本轮可接受的清晰度集合: 首选 + 显式允许的降级档; any=不限"""
    q = CFG.get("wanted_quality") or "2160p"
    if q == "any": return ("2160p", "1080p", "other")
    out = [q] + [x for x in (CFG.get("quality_fallback") or []) if x != q]
    return tuple(out)

# ---------------------------------------------------------------- 中文名→英文名(豆瓣)
RE_CN = re.compile(r"[\u4e00-\u9fff]")
RE_SEASON_CN = re.compile(r"第\s*([一二三四五六七八九十\d]+)\s*季")
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

def cn_season_num(s):
    """中文/阿拉伯数字 → int; 失败 None"""
    if s.isdigit(): return int(s)
    return CN_NUM.get(s)

def douban_lookup(q):
    """豆瓣 subject_suggest → [{title_cn, title_en, year, is_tv, season}]"""
    try:
        raw = http("https://movie.douban.com/j/subject_suggest?q=" + urllib.parse.quote(q),
                   headers={"User-Agent": SUB_UA["User-Agent"]}, timeout=12)
        out = []
        for x in json.loads(raw.decode("utf-8")):
            t = x.get("title") or ""
            m = RE_SEASON_CN.search(t)
            out.append({"title_cn": t, "title_en": x.get("sub_title") or "",
                        "year": x.get("year") or "", "id": x.get("id") or "",
                        "season": cn_season_num(m.group(1)) if m else None,
                        "is_tv": bool(m) or bool(re.search(r"第[一二三四五六七八九十\d]+季", t))})
        return out
    except Exception:
        return []

def cn2en(title):
    """中文标题 → 英文标题(带季号转换); 无中文或查不到时原样返回。
    '黑帮领地 第二季' → 'MobLand S02'; '奥本海默' → 'Oppenheimer'"""
    if not RE_CN.search(title):
        return title, None
    cands = douban_lookup(title)
    # 先精确匹配完整标题(含季号), 再退化到去季号查询
    if not cands:
        base = RE_SEASON_CN.sub("", title).strip()
        if base != title:
            cands = douban_lookup(base)
    if not cands:
        return title, None
    m = RE_SEASON_CN.search(title)
    want_season = m.group(1) if m else None
    pick = None
    if want_season:
        sn = cn_season_num(want_season)
        for c in cands:
            if c["season"] == sn and c["title_en"]:
                pick = c; break
    if not pick:
        pick = cands[0]
    en = pick["title_en"]
    if not en:
        return title, pick
    if want_season:
        sn = cn_season_num(want_season) or 1
        return f"{en} S{sn:02d}", pick
    return en, pick

def clean_release(title):
    t = re.sub(r"\[.*?\]|\(.*?\)", " ", title)
    return re.sub(r"\s+", " ", t).strip(" .-")[:70]

def sanitize(name):
    return re.sub(r'[\\/:*?"<>|]+', " ", name).strip()[:120]

def is_video(name):
    return name.lower().endswith((".mkv", ".mp4", ".ts", ".m2ts", ".avi", ".mov", ".wmv"))

def ep_from_name(name):
    m = RE_EP.search(name)
    if m: return int(m.group(1)), int(m.group(2))
    return None

# ---------------------------------------------------------------- 字幕
SUB_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}

def ffprobe_streams(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "stream=index,codec_name,codec_type:stream_tags=language,title",
                            "-of", "json", path], capture_output=True, timeout=120)
        return json.loads(r.stdout or b"{}").get("streams") or []
    except Exception:
        return []

def pick_embedded_sub(video_path):
    """选最佳内嵌字幕流: zh > en > 其他; 返回 {index, lang, codec} 或 None"""
    streams = ffprobe_streams(video_path)
    subs = [s for s in streams if s.get("codec_type") == "subtitle"
            and s.get("codec_name") not in ("hdmv_pgs_subtitle", "dvd_subtitle", "subrip_png")]
    if not subs:
        return None
    def score(s):
        lang = (s.get("tags") or {}).get("language", "").lower()
        title = ((s.get("tags") or {}).get("title") or "").lower()
        zh = lang in ("zh", "chi", "zho", "chs", "cht") or "简" in title or "繁" in title or "中" in title
        en = lang in ("en", "eng")
        return (0 if zh else (1 if en else 2), "sdh" in title, 0 if s.get("codec_name") == "subrip" else 1)
    subs.sort(key=score)
    best = subs[0]
    lang = (best.get("tags") or {}).get("language", "unk").lower()
    lang = {"zh": "zh", "chi": "zh", "zho": "zh", "chs": "zh", "cht": "zh",
            "en": "en", "eng": "en"}.get(lang, "unk")
    return {"index": best["index"], "lang": lang, "codec": best.get("codec_name")}

def extract_embedded_sub(video_path, stream_index, dst_path):
    codec = "ass" if dst_path.lower().endswith(".ass") else "srt"
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", video_path,
                        "-map", f"0:{stream_index}", "-c", codec, dst_path],
                       capture_output=True, timeout=600)
    return r.returncode == 0 and os.path.exists(dst_path) and os.path.getsize(dst_path) > 200

def subhd_session():
    """SubHD 登录会话(config.json 里 subhd_email/subhd_password 可选配置)"""
    email, pwd = CFG.get("subhd_email"), CFG.get("subhd_password")
    if not email or not pwd: return None
    try:
        import http.cookiejar
        cj = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        op.open(urllib.request.Request("https://subhd.tv/set/login", headers=SUB_UA), timeout=20).read()
        r = op.open(urllib.request.Request("https://subhd.tv/api/set/login",
            json.dumps({"email": email, "pwd": pwd}).encode(),
            {**SUB_UA, "Content-Type": "application/json; charset=utf-8",
             "Referer": "https://subhd.tv/set/login", "X-Requested-With": "XMLHttpRequest",
             "Origin": "https://subhd.tv"}), timeout=20).read().decode()
        if not json.loads(r).get("success"):
            log(f"SubHD 登录被拒: {r[:120]}"); return None
        return op
    except Exception as e:
        log(f"SubHD 登录失败: {e}")
        return None

def subhd_fetch(opener, kw, limit=3):
    """SubHD 全链路: 搜索→/a/版本页→prepare→/down/中转页→/api/sub/down→直链ZIP。403随机, 整体重试。
    返回下载成功的原始字节列表(最多 limit 个, 供调用方逐个解析选优)。"""
    raws = []
    for attempt in range(3):
        try:
            html = opener.open(urllib.request.Request(
                "https://subhd.tv/search/" + urllib.parse.quote(kw), headers=SUB_UA), timeout=20).read().decode("utf-8", "ignore")
            codes = list(dict.fromkeys(re.findall(r"href=['\"]/a/([A-Za-z0-9]{4,12})['\"]", html)))
            if not codes:
                log("SubHD 搜索无结果"); return raws
            for code in codes:
                if len(raws) >= limit: return raws
                raw = subhd_download_one(opener, code)
                if raw: raws.append(raw)
        except Exception as e:
            log(f"SubHD 查询失败(第{attempt+1}次): {e}")
        time.sleep(3)
    return raws

def subhd_download_one(opener, code):
    """单个版本短码: /a/{code} 详情页 → prepare-download → 中转页 → /api/sub/down → 文件直链"""
    try:
        ahtml = opener.open(urllib.request.Request("https://subhd.tv/a/" + code, headers=SUB_UA), timeout=20).read().decode("utf-8", "ignore")
        if "subtitle-prepare-download" not in ahtml: return None  # 未登录或无下载权
        pr = json.loads(opener.open(urllib.request.Request(
            "https://subhd.tv/api/sub/prepare-download", json.dumps({"sid": code}).encode(),
            {**SUB_UA, "Content-Type": "application/json; charset=utf-8",
             "Referer": "https://subhd.tv/a/" + code, "X-Requested-With": "XMLHttpRequest",
             "Origin": "https://subhd.tv"}), timeout=30).read().decode())
        if not (pr.get("success") and pr.get("url", "").startswith("/down/")): return None
        # 中转页: 间歇403, 快速重试(prepare→down 间隔要短, 拖久临时页失效)
        page = None
        for _ in range(4):
            try:
                page = opener.open(urllib.request.Request("https://subhd.tv" + pr["url"],
                    headers={**SUB_UA, "Referer": "https://subhd.tv/a/" + code,
                             "X-Requested-With": "XMLHttpRequest"}), timeout=30).read().decode("utf-8", "ignore")
                if len(page) > 5000: break
                page = None
            except Exception: pass
            time.sleep(2)
        if not page: return None
        dn = json.loads(opener.open(urllib.request.Request(
            "https://subhd.tv/api/sub/down", json.dumps({"sid": code}).encode(),
            {**SUB_UA, "Content-Type": "application/json; charset=utf-8",
             "Referer": "https://subhd.tv" + pr["url"], "X-Requested-With": "XMLHttpRequest",
             "Origin": "https://subhd.tv"}), timeout=30).read().decode())
        if not (dn.get("success") and dn.get("url")):
            log(f"SubHD down 未通过: {json.dumps(dn, ensure_ascii=False)[:120]}")
            return None
        return opener.open(urllib.request.Request(dn["url"],
            headers={**SUB_UA, "Referer": "https://subhd.tv" + pr["url"]}), timeout=120).read()
    except Exception as e:
        log(f"SubHD 下载 {code} 失败: {e}")
        return None

def extract_sub_archive(raw):
    """zip/rar → (bytes, ext)；也接受裸 srt/ass"""
    if raw[:2] == b"PK" or raw[:6].startswith(b"Rar!"):
        cands = []  # (name, size, read_fn)
        if raw[:2] == b"PK":
            zf = zipfile.ZipFile(io.BytesIO(raw))
            for i in zf.infolist():
                if not i.is_dir() and i.filename.lower().endswith((".srt", ".ass", ".ssa")):
                    cands.append((i.filename, i.file_size, lambda i=i: zf.read(i)))
        else:  # RAR (含 RAR5): 用 7z 解(p7zip-rar)
            tmp = f"/tmp/gy_sub_{int(time.time()*1000)}.rar"
            with open(tmp, "wb") as f: f.write(raw)
            try:
                d = tmp + ".d"
                subprocess.run(["7z", "x", "-y", f"-o{d}", tmp], capture_output=True, timeout=120)
                for root, _, files in os.walk(d):
                    for fn in files:
                        if fn.lower().endswith((".srt", ".ass", ".ssa")):
                            p = os.path.join(root, fn)
                            cands.append((fn, os.path.getsize(p), lambda p=p: open(p, "rb").read()))
            finally:
                subprocess.run(["rm", "-rf", tmp, tmp + ".d"], capture_output=True)
        if not cands: return None, None
        def sub_score(c):  # 双语(中英) > 中文单语 > 其他; 同级 ass > srt; 大者优
            n = c[0].upper()
            bilingual = bool(re.search(r"EN[&+]?(CH|CT|CN)|[&+](CH|CT|CN)|\bGB&EN|ENG.{0,4}CH[ST]", n))
            chinese = bool(re.search(r"CHS|CHT|CHN|\bCN\b|\.GB|BIG5|简|繁|中英|双语", n))
            lang = 0 if bilingual else (1 if chinese else 2)
            ext = 0 if n.endswith(".ass") else 1   # ass 优先
            return (lang, ext, -c[1])
        best = min(cands, key=sub_score)
        return best[2](), best[0].rsplit(".", 1)[-1].lower()
    if raw.lstrip()[:12].lower().startswith(b"[script info"): return raw, "ass"
    if raw.lstrip().startswith((b"1", b"\xef\xbb\xbf1")) and b"-->" in raw[:2000]: return raw, "srt"
    return None, None

def ass_to_srt(data, workdir):
    tmp = os.path.join(workdir, f".{int(time.time()*1000)}.cvt.ass")
    open(tmp, "wb").write(data)
    srt = tmp[:-4] + ".srt"
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", tmp, srt], capture_output=True, timeout=120)
    os.unlink(tmp)
    if r.returncode == 0 and os.path.exists(srt) and os.path.getsize(srt) > 200:
        return open(srt, "rb").read()
    if os.path.exists(srt): os.unlink(srt)
    return None

def find_subtitle(video_path, hint):
    """字幕策略: 1)已有 2)内嵌zh提取 3)SubHD中文(配置账号) 4)内嵌en兜底"""
    base = os.path.splitext(video_path)[0]
    if any(os.path.exists(p) for p in [f"{base}.zh.ass", f"{base}.zh.srt", f"{base}.chs.srt",
                                        f"{base}.cht.srt", f"{base}.srt", f"{base}.ass"]):
        return ["已有"]
    # 内嵌中文字幕优先(快且准); 输出 ass(保留样式), 失败再退 srt
    pick = pick_embedded_sub(video_path)
    if pick and pick["lang"] == "zh":
        for ext_out in ("ass", "srt"):
            dst = f"{base}.zh.{ext_out}"
            if extract_embedded_sub(video_path, pick["index"], dst):
                os.chmod(dst, 0o777)
                return [f"内嵌提取(zh) → {os.path.basename(dst)}"]
    # SubHD 下载中文字幕(账号配置后); 多候选逐个试(有些包是 .sup 图形字幕无文本)
    op = subhd_session()
    if op:
        for raw in subhd_fetch(op, hint, limit=3):
            data, ext = extract_sub_archive(raw)
            if data:
                dst = f"{base}.zh.{ext}"
                open(dst, "wb").write(data); os.chmod(dst, 0o777)
                return [f"SubHD → {os.path.basename(dst)}"]
    # 兜底: 内嵌英文字幕
    if pick and pick["lang"] == "en":
        dst = f"{base}.en.srt"
        if extract_embedded_sub(video_path, pick["index"], dst):
            os.chmod(dst, 0o777)
            return [f"内嵌提取(en) → {os.path.basename(dst)}"]
    return ["未找到"]

# ---------------------------------------------------------------- 状态
def state(): return load_json(STATE_F, {"tasks": {}, "processed": [], "waiting": {}})
def save_state(s): save_json(STATE_F, s)
def shows(): return load_json(SHOWS_F, [])
def save_shows(x): save_json(SHOWS_F, x)

def already_have_video(show, s, e, quality_prefix="2160"):
    """NAS 上已有该集(任意 2160p 版本)则跳过"""
    d = os.path.join(CFG["media_root"], show)
    if not os.path.isdir(d): return False
    for f in os.listdir(d):
        m = ep_from_name(f)
        if m == (s, e) and quality_prefix in f: return True
    return False

# ---------------------------------------------------------------- 提交
def best_for_episode(results, s, e, quality="2160p"):
    cands = []
    for r in results:
        p = parse_title(r["title"])
        if p["bad"] or p["quality"] != quality: continue
        if not format_allowed(p) or not size_ok(r["size"] / (1024 ** 3)): continue
        if not lang_ok(p.get("langs") or set()): continue
        if p["ep"] == e and p["season"] == s: cands.append(r)          # 单集
        elif p["ep"] and p["season"] == s and p["ep"] <= e <= (p["ep_end"] or p["ep"]): cands.append(r)  # 范围包
        elif p["season_pack"] == s and p["ep"] is None: cands.append(r)  # 整季包
        else: continue
        cands[-1]["_p"] = p
    return min(cands, key=rank_key, default=None)

def best_for_movie(results, quality="2160p"):
    cands = []
    for r in results:
        p = parse_title(r["title"])
        if p["bad"] or p["quality"] != quality or p["ep"] is not None: continue
        if not format_allowed(p) or not size_ok(r["size"] / (1024 ** 3)): continue
        if not lang_ok(p.get("langs") or set()): continue
        r["_p"] = p; cands.append(r)
    return min(cands, key=rank_key, default=None)

def submit(res, label, parent_path, wanted_eps=None, min_mb=None):
    """resolve → 选文件 → 建云任务。返回 (taskId, magnet, picked_files)"""
    magnet, _ = to_magnet(res)
    if not magnet: raise RuntimeError("无法获得磁链")
    resolved = gy_resolve(magnet)
    files = resolved.get("files") or []

    # 单文件磁力兜底: 光鸭 resolve 后 files 可能为空，但 title/totalSize 明确代表单文件
    if not files and resolved.get("totalSize", 0) >= (min_mb or CFG["min_video_mb"]) * MB:
        single_title = resolved.get("title") or label
        if is_video(single_title) or (not os.path.splitext(single_title)[1] and is_video(res.get("title") or "")):
            files = [{
                "fileIndex": 0,
                "fileName": single_title,
                "fileSize": resolved.get("totalSize", 0),
                "selected": True
            }]

    vids = [f for f in files if is_video(f.get("fileName") or "")
            and (f.get("fileSize") or 0) >= (min_mb or CFG["min_video_mb"]) * MB
            and "sample" not in (f.get("fileName") or "").lower()]
    # 格式白名单: 用真实文件扩展名做二次确认(标题可能没标格式)
    allow = [x.lower() for x in (CFG.get("allow_format") or [])]
    if allow:
        kept = [f for f in vids
                if os.path.splitext(f.get("fileName") or "")[1].lstrip(".").lower() in allow]
        if not kept:
            raise RuntimeError(f"资源内无允许格式({','.join(allow)})的视频: "
                               f"{[f.get('fileName') for f in vids][:5]}")
        vids = kept
    # 单文件体积区间: 用真实文件大小复核
    hi = float(CFG.get("size_max_gb") or 0)
    if hi:
        kept = [f for f in vids if (f.get("fileSize") or 0) <= hi * 1024 ** 3]
        if kept: vids = kept
    if wanted_eps:
        picked = [f for f in vids if ep_from_name(f.get("fileName") or "") in wanted_eps]
        if picked: vids = picked
    if not vids:
        raise RuntimeError(f"资源中无可选视频文件(>={CFG['min_video_mb']}MB): {[f.get('fileName') for f in files][:5]}")
    # 注意: 若原始 resolve 结果中 files 为空 (单文件磁力)，光鸭 API 不接受 fileIndexes 参数 (传 [0] 会报错误码 112: 参数错误)
    idxs = [f["fileIndex"] for f in vids] if (resolved.get("files") and len(resolved["files"]) > 0) else None
    parent_id = gy_ensure_path(parent_path)
    tid = gy_add_magnet(magnet, parent_id, idxs, sanitize(label))
    return tid, magnet, vids

# ---------------------------------------------------------------- 推进任务
def phase_cloud(st):
    """stage=cloud: 查云任务, 完成则启动 NAS 下载"""
    tasks = {t.get("taskId"): t for t in gy_cloud_tasks()}
    for k, t in list(st["tasks"].items()):
        if t["stage"] != "cloud": continue
        ct = tasks.get(str(t.get("cloudTaskId")))
        if not ct:
            found = [c for c in tasks.values()
                     if t.get("hash") and t["hash"] in (c.get("res") or "").upper()]
            ct = found[0] if found else None
            if ct: t["cloudTaskId"] = ct.get("taskId")
        if not ct:
            if time.time() - t.get("added", 0) > 86400:
                t["stage"] = "failed"; t["error"] = "云任务列表中未找到(24h)"
                event(f"失败 {t['label']}: 云任务丢失")
            continue
        # 异常状态判定: status=3 代表云端出错(如 errCode 112: 参数错误, 或离线下载失败)
        if ct.get("status") == 3:
            err_msg = ct.get("errMsg") or f"错误码 {ct.get('errCode')}"
            t["stage"] = "failed"; t["error"] = f"云端任务失败: {err_msg}"
            event(f"失败 {t['label']}: 云端任务失败({err_msg})")
            continue
        # 完成判定: progress 可能是 None(新版API不下发) → 以目录内或文件自身是否可下载为准
        prog = ct.get("progress")
        done = (int(prog) >= 100) if prog is not None else False
        fid = ct.get("fileId")
        if not done and fid:
            try:
                # 若任务本身为单文件直落，或者目录下已有完成的视频文件
                vids = walk_videos(fid, default_name=ct.get("fileName") or t.get("label"), default_size=ct.get("totalSize") or 0)
                done = bool(vids)
            except Exception:
                done = False
        if done and fid:
            t["cloudFileId"] = fid
            start_nas_for_task(st, k, t, fallback_name=ct.get("fileName") or t.get("label"), fallback_size=ct.get("totalSize") or 0)
        elif time.time() - t.get("added", 0) > CFG["cloud_stale_hours"] * 3600:
            t["stage"] = "failed"; t["error"] = f"云任务超时 progress={prog}"
            event(f"失败 {t['label']}: 云任务超时")

def walk_videos(fid, depth=0, default_name=None, default_size=0):
    """递归云盘目录, 返回 [(fileId, fileName, size)]。若 fid 本身即为单文件直链，直接返回单文件元组。"""
    if depth > 4: return []
    out = []
    entries = gy_list(fid)
    if not entries:
        # 如果列不出子项，探测该 fid 是否自身就是可下载的文件直链
        try:
            if gy_download_url(fid):
                name = default_name or f"file_{fid}.mkv"
                return [(fid, name, default_size)]
        except Exception:
            pass
        return []

    for e in entries:
        if is_file_entry(e):
            if is_video(e.get("fileName") or ""): out.append((e["fileId"], e["fileName"], e.get("fileSize") or 0))
        else:
            out += walk_videos(e["fileId"], depth + 1)
    return out

def start_nas_for_task(st, k, t, fallback_name=None, fallback_size=0):
    vids = walk_videos(t["cloudFileId"], default_name=fallback_name, default_size=fallback_size)
    if not vids:
        t["stage"] = "failed"; t["error"] = "云目录中无视频文件"
        event(f"失败 {t['label']}: 云目录无视频"); return
    # 选片: 单集任务取最大文件; 整季/多文件取全部>min
    vids.sort(key=lambda v: -v[2])
    if t["kind"] == "tv" and len(vids) > 1 and t.get("wanted"):
        picked = [v for v in vids if ep_from_name(v[1]) in t["wanted"]] or vids
    else:
        picked = [vids[0]]
    if t["kind"] == "movie" and len(vids) > 1:
        picked = [vids[0]]
    target = t["nas_dir"]
    os.makedirs(target, exist_ok=True)
    os.chmod(target, 0o777)
    t["nas"] = []
    for fid, fname, size in picked:
        out = os.path.join(target, sanitize(fname))
        if (os.path.exists(out) and not os.path.exists(out + ".aria2")
                and os.path.getsize(out) >= size * 0.98 and size > 0):
            continue  # 已完整存在
        # 中断残留(prealloc 全尺寸但无下载进程): 删掉让 aria2 从头下, 否则可能秒完但内容是空的
        if os.path.exists(out + ".aria2"):
            try: os.remove(out); os.remove(out + ".aria2")
            except Exception: pass
        tid = gy_nas_start(fid, sanitize(fname), target)
        t["nas"].append({"nasTaskId": tid, "file": out, "size": size, "fileId": fid})
    if not t["nas"]:
        finish_task(st, k, t)
        return
    t["stage"] = "nas"
    event(f"开始NAS下载 {t['label']}: {len(t['nas'])}个文件 → {target}")

def phase_nas(st):
    """stage=nas: 查 aria2 传输, 全部完成→字幕→收尾"""
    transfers = gy_transfers()
    for k, t in list(st["tasks"].items()):
        if t["stage"] != "nas": continue
        done, failed = True, False
        for n in t.get("nas", []):
            tr = transfers.get(str(n["nasTaskId"])) or {}
            st_status = tr.get("status")
            # aria2 默认 prealloc: 文件瞬间预分配到全尺寸, size 判断不可靠 → 以 status 为准
            if st_status == "completed": continue
            if st_status in ("failed", "cancelled"):
                failed = True; done = False; continue
            done = False
            if st_status in ("downloading", "getting_url", "pending"): continue
            # 无状态(服务重启丢任务): 无 .aria2 控制文件且大小达标才算完成
            path = n["file"]
            if (os.path.exists(path) and not os.path.exists(path + ".aria2")
                    and (not n["size"] or os.path.getsize(path) >= n["size"] * 0.98)):
                continue
            failed = True  # 状态丢失且文件不完整/仍在下 → 触发重试
        if failed:
            t["retries"] = t.get("retries", 0) + 1
            if t["retries"] <= 2:
                log(f"重试 {t['label']} (第{t['retries']}次)")
                start_nas_for_task(st, k, t)
            else:
                t["stage"] = "failed"; t["error"] = "NAS下载失败(重试2次)"
                event(f"失败 {t['label']}: NAS下载失败")
            continue
        if done:
            finish_task(st, k, t)

def finish_task(st, k, t):
    t["stage"] = "done"
    subs = []
    for n in t.get("nas", []):
        if not os.path.exists(n["file"]): continue
        hint = t.get("sub_hint") or os.path.splitext(os.path.basename(n["file"]))[0][:40]
        r = find_subtitle(n["file"], hint)
        subs.append(f"{os.path.basename(n['file'])}: {','.join(r)}")
    if t["kind"] == "tv" and t.get("show"):
        for sh in shows():
            if show_key(sh["name"]) == t["show"]:
                s, e = t.get("ep", (0, 0))
                if (s, e) > tuple(sh.get("last", (0, 0))):
                    sh["last"] = [s, e]
                    save_shows(shows())
                break
    st["waiting"].pop(t.get("show") or "", None)
    t["done_at"] = time.time()
    event(f"完成 {t['label']} | 字幕: {'; '.join(subs) or '无视频'}")
    if CFG.get("cleanup_cloud") and t.get("cloudTaskId"):
        try: gy_post("/api/cloud-add/delete", {"taskId": t["cloudTaskId"], "deleteFile": True})
        except Exception: pass

# ---------------------------------------------------------------- 搜新集
def search_new_episodes(st, dry=False):
    submitted = 0
    for sh in shows():
        if not sh.get("enabled", True): continue
        name = show_key(sh["name"])
        last_s, last_e = tuple(sh.get("last", [0, 0]))
        try:
            results = jackett_search(f"{sh['name']} S{last_s:02d}", category=5000)
        except Exception as e:
            log(f"搜索失败 {name}: {e}"); continue
        by_ep = {}
        for r in results:
            p = parse_title(r["title"])
            if p["bad"] or p["season"] != last_s or not p["ep"]:
                continue
            if p["quality"] not in wanted_qualities(): continue
            if not format_allowed(p) or not size_ok(r["size"] / (1024 ** 3)): continue
            if not lang_ok(p.get("langs") or set()): continue
            if (p["ep"],) <= (last_e,): continue
            r["_p"] = p
            if p["ep"] not in by_ep or rank_key(r) < rank_key(by_ep[p["ep"]]):
                by_ep[p["ep"]] = r
        wanted = [e for e in sorted(by_ep) if e > last_e and not already_have_video(name, last_s, e)]
        # 排除已提交
        queued = {t.get("ep", (0, 0))[1] for t in st["tasks"].values()
                  if t.get("show") == name and t.get("stage") in ("cloud", "nas")}
        wanted = [e for e in wanted if e not in queued]
        if not wanted:
            st["waiting"].pop(name, None)
            continue
        # ---- 下载节奏控制 ----
        # (1) 未上线资源自动下载开关: 若用户关闭, 仅记录不提交
        if not CFG.get("auto_download_new", True):
            st["waiting"][name] = {f"S{last_s:02d}E{e:02d}": "已就绪, 自动下载已关闭" for e in wanted}
            continue
        # (2) 仍在下载中的集数计入本批配额, 避免一次性全吐出去
        inflight = sum(1 for t in st["tasks"].values()
                       if t.get("show") == name and t.get("stage") in ("cloud", "nas"))
        quota = max(0, int(CFG.get("batch_size", 2)) - inflight)
        if quota <= 0:
            log(f"{name}: 本批已满({inflight}集在下载, batch_size={CFG.get('batch_size', 2)}), 等待完成")
            st["waiting"][name] = {f"S{last_s:02d}E{e:02d}": "等待本批完成" for e in wanted}
            continue
        # (3) 攒批间隔: 上一批刚下完不到 N 小时, 先不下
        gap_h = float(CFG.get("batch_interval_hours", 0) or 0)
        if gap_h > 0:
            last_done = max([t.get("done_at", 0) for t in st["tasks"].values()
                             if t.get("show") == name] or [0])
            if last_done and time.time() - last_done < gap_h * 3600:
                nxt = datetime.fromtimestamp(last_done + gap_h * 3600).strftime("%m-%d %H:%M")
                log(f"{name}: 攒批间隔未到(上次完成 {datetime.fromtimestamp(last_done).strftime('%m-%d %H:%M')}, 下次 {nxt})")
                st["waiting"][name] = {f"S{last_s:02d}E{e:02d}": f"攒批中, {nxt} 后开始" for e in wanted}
                continue
        # (4) 补集模式: 关闭时每次只追最新一集
        if not CFG.get("backfill_enabled", True):
            wanted = wanted[-1:]
        wanted = wanted[:quota]
        if dry:
            print(f"[dry] {name}: 待下 {['S%02dE%02d' % (last_s, e) for e in wanted]}")
            for e in wanted:
                r = by_ep[e]
                print(f"   S{last_s:02d}E{e:02d} ← {r['title'][:60]} ({r['seeders']}seeds, {r['size']//MB}MB, {r['idx']})")
            continue
        for e in wanted:
            if submitted >= CFG["max_submit_per_run"]:
                log(f"{name}: 本轮提交已达上限({CFG['max_submit_per_run']}), E{e:02d} 等下轮")
                break
            r = by_ep[e]
            label = f"{name} S{last_s:02d}E{e:02d}"
            try:
                tid, magnet, vids = submit(r, label, [CFG["cloud_root"], name], wanted_eps={(last_s, e)})
                k = f"t{int(time.time()*1000)}-{e}"
                st["tasks"][k] = {"kind": "tv", "show": name, "season": last_s, "ep": [last_s, e],
                                  "label": label, "cloudTaskId": str(tid), "hash": magnet.upper().split("btih:")[-1].split("&")[0],
                                  "nas_dir": os.path.join(CFG["media_root"], name),
                                  "sub_hint": f"{sh['name']} S{last_s:02d}E{e:02d}",
                                  "added": time.time(), "stage": "cloud", "wanted": [(last_s, e)]}
                st["processed"].append(magnet.upper().split("btih:")[-1].split("&")[0])
                submitted += 1
                event(f"已提交 {label} ← {r['title'][:60]} ({r['idx']}, {r['seeders']}seeds)")
                time.sleep(2)
            except Exception as ex:
                log(f"提交失败 {label}: {ex}")
                st["waiting"].setdefault(name, {})[f"S{last_s:02d}E{e:02d}"] = f"提交失败: {ex}"

def search_due(st):
    """判断本轮是否该跑「搜新集」。返回 (是否该跑, 说明)"""
    if not st.get("last_search"):
        return True, "首次搜索"
    gap_h = float(CFG.get("search_interval_hours", 24) or 0)
    if gap_h <= 0:
        return True, "每次循环都搜(间隔=0)"
    elapsed = (time.time() - st["last_search"]) / 3600.0
    if elapsed >= gap_h:
        return True, f"距上次 {elapsed:.1f}h ≥ {gap_h}h"
    nxt = datetime.fromtimestamp(st["last_search"] + gap_h * 3600).strftime("%m-%d %H:%M")
    return False, f"距上次 {elapsed:.1f}h < {gap_h}h, 下次 {nxt}"

def cycle(st, dry=False, search=True):
    # 云/NAS 推进不受搜索周期限制, 每轮都做
    phase_cloud(st)
    phase_nas(st)
    if search:
        due, why = search_due(st)
        if due:
            log(f"开始搜新集({why})")
            search_new_episodes(st, dry=dry)
            if not dry:
                st["last_search"] = time.time()
        else:
            log(f"跳过搜新集({why})")
    # 清理 done>30天
    now = time.time()
    for k in [k for k, t in st["tasks"].items() if t["stage"] in ("done", "failed")
              and now - t.get("added", 0) > 30 * 86400]:
        st["tasks"].pop(k, None)

# ---------------------------------------------------------------- CLI
def is_tv_query(t): return bool(RE_EP.search(t) or RE_SEASON.search(t))

def cmd_search(args):
    trans, meta = cn2en(args.title)
    if trans != args.title: print(f"(识别: {args.title} → {trans})")
    args.title = trans
    kind = "tv" if is_tv_query(args.title) else "movie"
    cat = 5000 if kind == "tv" else 2000
    results = jackett_search(args.title, category=cat)
    pick = best_for_episode if kind == "tv" else best_for_movie
    p0 = parse_title(args.title)
    if kind == "tv":
        m = RE_EP.search(args.title)
        best = pick(results, int(m.group(1)), int(m.group(2)), CFG["wanted_quality"]) if m else None
    else:
        best = pick(results, CFG["wanted_quality"])
    print(f"共 {len(results)} 条 (类别={'tv' if kind=='tv' else 'movie'})")
    q4k = []
    for r in results:
        p = parse_title(r["title"])
        if p["quality"] == "2160p" and not p["bad"]:
            r["_p"] = p; q4k.append(r)
    q4k.sort(key=rank_key)
    seen, uniq = set(), []
    for r in q4k:
        sig = re.sub(r"[^a-z0-9]", "", r["title"].lower())[:35]
        if sig in seen: continue
        seen.add(sig); uniq.append(r)
    for r in uniq[:12]:
        p = parse_title(r["title"])
        print(f"  2160p | {r['title'][:62]} | {r['seeders']}seeds | {r['size']//MB}MB | {r['idx']}"
              f"{(' | ' + ','.join(sorted(l.upper() for l in p['langs']))) if p['langs'] else ''}"
              f"{' | magnet' if r['magnet'] else ' | 种子'}")
    if best:
        print(f"\n★ 推荐: {best['title']}\n  {best['seeders']}seeds {best['size']//MB}MB {best['idx']}")
    elif q4k:
        print("\n(有2160p但无精确匹配)")
    else:
        lo = sorted([r for r in results if parse_title(r["title"])["quality"] == "1080p"],
                    key=lambda r: -r["seeders"])[:5]
        if lo:
            print("\n无2160p资源。1080p候选:")
            for r in lo: print(f"  1080p | {r['title'][:62]} | {r['seeders']}seeds | {r['idx']}")
        else:
            print("\n无可用资源")

def cmd_add(args):
    title, meta = cn2en(args.title)
    if title != args.title: print(f"(识别: {args.title} → {title})")
    args.title = title
    m = RE_EP.search(title)
    st = state()
    if m:  # 单集

        s, e = int(m.group(1)), int(m.group(2))
        show_guess = re.split(r"\bS\d{1,2}E\d{1,3}\b", title, flags=re.I)[0]
        name = show_key(show_guess)
        results = jackett_search(title, category=5000)
        best = best_for_episode(results, s, e, CFG["wanted_quality"])
        if not best:
            no4k_exit(results)
        print(f"提交: {name} S{s:02d}E{e:02d} ← {best['title'][:70]}")
        tid, magnet, vids = submit(best, f"{name} S{s:02d}E{e:02d}", [CFG["cloud_root"], name],
                                   wanted_eps={(s, e)})
        k = f"t{int(time.time()*1000)}"
        st["tasks"][k] = {"kind": "tv", "show": name, "season": s, "ep": [s, e],
                          "label": f"{name} S{s:02d}E{e:02d}", "cloudTaskId": str(tid),
                          "hash": magnet.upper().split("btih:")[-1].split("&")[0],
                          "nas_dir": os.path.join(CFG["media_root"], name),
                          "sub_hint": f"{name} S{s:02d}E{e:02d}",
                          "added": time.time(), "stage": "cloud", "wanted": [(s, e)]}
        st["processed"].append(magnet.upper().split("btih:")[-1].split("&")[0])
        save_state(st)
        print(f"云任务 {tid} 已创建, 之后 run/poll 会自动推进到NAS并补字幕")
    elif is_tv_query(title):
        sys.exit("暂不支持整季包下载, 请用单集标题(如 剧名 S02E07)或 addshow 登记追踪")
    else:
        name = show_key(title)
        results = jackett_search(title, category=2000)
        best = best_for_movie(results, CFG["wanted_quality"])
        if not best:
            no4k_exit(results)
        print(f"提交电影: {name} ← {best['title'][:70]}")
        label = clean_release(best["title"])
        tid, magnet, vids = submit(best, label, [CFG["cloud_root"], "电影", name])
        k = f"t{int(time.time()*1000)}"
        st["tasks"][k] = {"kind": "movie", "label": label, "cloudTaskId": str(tid),
                          "hash": magnet.upper().split("btih:")[-1].split("&")[0],
                          "nas_dir": os.path.join(CFG["media_root"], "电影", name), "sub_hint": title[:40],
                          "added": time.time(), "stage": "cloud"}
        st["processed"].append(magnet.upper().split("btih:")[-1].split("&")[0])
        save_state(st)
        print(f"云任务 {tid} 已创建, 之后 run/poll 会自动推进到NAS并补字幕")

def no4k_exit(results):
    lo = sorted([r for r in results if parse_title(r["title"])["quality"] == "1080p"],
                key=lambda r: -r["seeders"])[:8]
    print("NO4K: 未找到2160p资源。", file=sys.stderr)
    if lo:
        print("1080p候选:", file=sys.stderr)
        for r in lo: print(f"  1080p | {r['title'][:66]} | {r['seeders']}seeds", file=sys.stderr)
    sys.exit(2)

def cmd_addshow(args):
    lst = shows()
    trans, meta = cn2en(args.name)
    if trans != args.name:
        print(f"(识别: {args.name} → {trans})")
        args.name = trans
    name = show_key(args.name)
    last = [0, 0]
    if not args.last and meta and meta.get("season"):
        last = [meta["season"], 0]
    if args.last:
        m = RE_EP.search(args.last)
        if not m: sys.exit("--last 格式应为 SxxEyy")
        last = [int(m.group(1)), int(m.group(2))]
    for sh in lst:
        if show_key(sh["name"]) == name:
            sh.update({"last": last} if args.last else {})
            save_shows(lst); print(f"已更新 {name} last=S{last[0]:02d}E{last[1]:02d}"); return
    lst.append({"name": args.name, "last": last, "enabled": True, "added": datetime.now().isoformat(timespec="seconds")})
    save_shows(lst)
    print(f"已登记追剧: {name} (从 S{last[0]:02d}E{last[1]+1:02d} 开始追踪)")

def cmd_rmshow(args):
    lst = [s for s in shows() if show_key(s["name"]) != show_key(args.name)]
    save_shows(lst); print(f"已移除 {args.name}")

def cmd_shows(args):
    for s in shows():
        print(f"  {show_key(s['name'])} | 追至 S{s['last'][0]:02d}E{s['last'][1]:02d} | {'启用' if s.get('enabled',True) else '停用'}")

def cmd_status(args):
    st = state()
    print("== 追剧 ==")
    cmd_shows(args)
    due, why = search_due(st)
    print(f"  搜索周期: 每 {CFG.get('search_interval_hours', 24)} 小时 | {'🔍 现在会搜' if due else '⏳ 等待中'}({why})")
    print("\n== 进行中任务 ==")
    n = 0
    for k, t in st["tasks"].items():
        if t["stage"] in ("cloud", "nas"):
            n += 1
            extra = ""
            if t["stage"] == "nas":
                trs = gy_transfers()
                pct = [trs.get(str(x["nasTaskId"]), {}).get("percent", "?") for x in t.get("nas", [])]
                extra = f" NAS进度{pct}"
            print(f"  [{t['stage']}] {t['label']}{extra}")
    if not n: print("  (无)")
    print("\n== 近期事件 ==")
    try:
        with open(EV, encoding="utf-8") as f: lines = f.readlines()[-10:]
        for l in lines: print("  " + l.strip())
    except Exception: pass

def cmd_run(args):
    st = state()
    cycle(st, dry=args.dry, search=not args.search_only)
    save_state(st)
    if args.dry: print("(dry-run: 未提交任何任务)")

def cmd_sub(args):
    r = find_subtitle(args.file, os.path.splitext(os.path.basename(args.file))[0][:40])
    print(r)

def delete_task(task_key):
    """从 state.json 中删除任务，并尽可能取消/删除光鸭云任务与传输任务"""
    st = state()
    if task_key not in st.get("tasks", {}):
        return False, "任务不存在"
    t = st["tasks"].pop(task_key)
    # 尝试删除光鸭端对应的离线任务
    cid = t.get("cloudTaskId")
    if cid:
        gy_cloud_task_delete(cid)
    # 尝试删除光鸭端的下载传输
    for n in t.get("nas", []):
        nid = n.get("nasTaskId")
        if nid:
            gy_transfer_delete(nid)
    save_state(st)
    log(f"已手动删除任务: {task_key} ({t.get('label')})")
    return True, f"已删除任务 {t.get('label')}"

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("search"); p.add_argument("title"); p.set_defaults(fn=cmd_search)
    p = sub.add_parser("add"); p.add_argument("title"); p.add_argument("--allow1080", action="store_true"); p.set_defaults(fn=cmd_add)
    p = sub.add_parser("addshow"); p.add_argument("name"); p.add_argument("--last", default=""); p.set_defaults(fn=cmd_addshow)
    p = sub.add_parser("rmshow"); p.add_argument("name"); p.set_defaults(fn=cmd_rmshow)
    p = sub.add_parser("shows"); p.set_defaults(fn=cmd_shows)
    p = sub.add_parser("status"); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("run"); p.add_argument("--dry", action="store_true"); p.add_argument("--search-only", action="store_true"); p.set_defaults(fn=cmd_run)
    p = sub.add_parser("poll"); p.set_defaults(fn=lambda a: (save_state((lambda s: (cycle(s, search=False), s)[1])(state()))))
    p = sub.add_parser("sub"); p.add_argument("file"); p.set_defaults(fn=cmd_sub)
    args = ap.parse_args()
    if args.cmd in ("run", "poll"):
        lock = open("/tmp/gy_autodl.lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            log("已有 run 在执行, 跳过"); return
        args.fn(args)
    else:
        args.fn(args)

if __name__ == "__main__":
    main()
