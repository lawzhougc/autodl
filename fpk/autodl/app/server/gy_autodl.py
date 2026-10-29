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
import argparse, html as html_mod, io, json, os, re, socket, subprocess, sys, time, zipfile
import hashlib, gzip
try:
    import fcntl
except ImportError:
    fcntl = None
import urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
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
WISH_F = os.path.join(DATA, "wishlist.json")

def extract_infohash(s):
    """从磁链或任意字符串中提取 40位 Hex 或 32位 Base32 的 BitTorrent infohash"""
    if not s: return ""
    m = re.search(r"\b([A-Fa-f0-9]{40})\b", s)
    if m: return m.group(1).upper()
    m2 = re.search(r"btih:([A-Za-z2-7]{32})\b", s, re.I)
    if m2: return m2.group(1).upper()
    if "btih:" in s.lower():
        try:
            return s.split("btih:", 1)[1].split("&")[0].split("/")[0].upper()
        except Exception: pass
    return ""

# ---------------------------------------------------------------- HTTP
class MagnetRedirectException(Exception):
    def __init__(self, url):
        self.url = url

class MagnetRedirectHandler(urllib.request.HTTPRedirectHandler):
    """当上游 301/302 重定向到 magnet: 协议时阻止抛出异常，以便捕获磁链"""
    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        if newurl and newurl.startswith("magnet:"):
            raise MagnetRedirectException(newurl)
        return super().redirect_request(req, fp, code, msg, hdrs, newurl)

def http(url, data=None, headers=None, timeout=30, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        err_msg = ""
        try:
            err_body = e.read().decode("utf-8", errors="ignore")
            # 尝试提取 json 里的 error 或 msg
            try:
                ej = json.loads(err_body)
                err_msg = ej.get("error") or ej.get("msg") or err_body
            except Exception:
                err_msg = err_body
        except Exception:
            pass
        if err_msg:
            raise RuntimeError(f"HTTP {e.code} 接口调用失败 ({url}): {err_msg}") from None
        raise RuntimeError(f"HTTP {e.code} 接口调用失败 ({url})") from None

def http_json(url, data=None, headers=None, timeout=30, method=None):
    raw = http(url, data, headers, timeout, method)
    return json.loads(raw.decode("utf-8"))

def gy_check_login():
    """检查光鸭云盘登录状态，若未登录或未启动则直接给出友好提示"""
    try:
        d = http_json(GY + "/api/user", timeout=5)
    except Exception as e:
        raise RuntimeError(f"无法连接光鸭云盘 App (端口 8998): {e}。请确保已在 fnOS 安装并启动光鸭网盘") from None
    if d.get("is_logged_in") is False or d.get("error"):
        raise RuntimeError("光鸭云盘当前未登录或登录态已失效！请先打开 fnOS 应用中心的「光鸭网盘」，扫码登录后再试")
    return d

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
    pid = root or ""
    for raw_n in names:
        n = sanitize(raw_n)
        if not n: continue
        fid = gy_find(n, pid)
        if not fid:
            created = False
            err_msg = ""
            for payload in [{"name": n, "dirName": n, "parentId": pid}, {"dirName": n, "parentId": pid}, {"name": n, "parentId": pid}]:
                try:
                    res = gy_post("/api/create-folder", payload, timeout=30)
                    if isinstance(res, dict):
                        if res.get("code") in (0, None):
                            created = True
                            new_fid = (res.get("data") or {}).get("fileId") or res.get("fileId")
                            if new_fid:
                                fid = new_fid
                                break
                        else:
                            err_msg = res.get("msg") or f"错误码 {res.get('code')}"
                    else:
                        created = True
                        break
                except Exception as e:
                    err_msg = str(e)
            
            if not fid:
                time.sleep(0.5)
                fid = gy_find(n, pid)
            if not fid:
                if err_msg:
                    log(f"⚠️ 网盘创建子目录 [{n}] 失败 ({err_msg})，自动降级挂载至上级目录 (ID: {pid or '根目录'}) 继续下载")
                else:
                    log(f"⚠️ 网盘未找到新建目录 [{n}]，自动降级挂载至上级目录 (ID: {pid or '根目录'}) 继续下载")
                return pid  # 优雅降级返回已成功确认的上一级 pid
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
    if new_name: body["newName"] = sanitize(new_name)[:80]
    r = gy_post("/api/cloud-add/magnet", body, timeout=90)
    tid = (r.get("data") or {}).get("taskId") or r.get("taskId")
    if not tid:
        raise RuntimeError(f"云任务创建失败: {json.dumps(r, ensure_ascii=False)[:300]}")
    return str(tid)

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
    res = gy_post("/api/download-task", {"fileId": fid, "fileName": fname, "targetDir": target_dir}, timeout=60)
    tid = (res.get("data") or {}).get("taskId") or res.get("taskId")
    if not tid:
        raise RuntimeError(f"NAS下载任务创建失败: {json.dumps(res, ensure_ascii=False)[:300]}")
    return str(tid)

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

def gy_transfers():
    """获取 NAS 下载传输状态，统一标准化映射为 {str(taskId): item_dict}"""
    try:
        raw = gy_get("/api/transfers", timeout=30)
    except Exception as e:
        log(f"获取传输列表失败: {e}")
        return {}
    if isinstance(raw, dict):
        if "list" in raw and isinstance(raw["list"], list):
            return {str(item.get("taskId")): item for item in raw["list"] if isinstance(item, dict) and item.get("taskId")}
        if "data" in raw:
            d = raw["data"]
            if isinstance(d, list):
                return {str(item.get("taskId")): item for item in d if isinstance(item, dict) and item.get("taskId")}
            if isinstance(d, dict) and "list" in d and isinstance(d["list"], list):
                return {str(item.get("taskId")): item for item in d["list"] if isinstance(item, dict) and item.get("taskId")}
            if isinstance(d, dict):
                return {str(k): v for k, v in d.items() if isinstance(v, dict)}
        if "tasks" in raw and isinstance(raw["tasks"], list):
            return {str(item.get("taskId")): item for item in raw["tasks"] if isinstance(item, dict) and item.get("taskId")}
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    elif isinstance(raw, list):
        return {str(item.get("taskId")): item for item in raw if isinstance(item, dict) and item.get("taskId")}
    return {}

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
    """并行搜索索引器 → 标准化结果列表"""
    key = jt_key()
    jt_url = (CFG.get("jackett_url") or JT).rstrip("/")
    timeout = min(30, int(CFG.get("search_timeout", 30)))
    def one(idx):
        url = (f"{jt_url}/api/v2.0/indexers/{idx}/results?apikey={key}"
               + (f"&Category={category}" if category else "")
               + "&Query=" + urllib.parse.quote(query))
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return idx, json.load(r).get("Results") or []
        except Exception as e:
            return idx, f"{type(e).__name__}: {e}"
    out = []
    indexers = CFG.get("indexers", [])
    if indexers:
        with ThreadPoolExecutor(min(8, len(indexers))) as ex:
            futures = {ex.submit(one, idx): idx for idx in indexers}
            for fut in as_completed(futures):
                idx, res = fut.result()
                if isinstance(res, str):
                    log(f"  索引器 {idx} 失败: {res}")
                    continue
                for r in res:
                    ih = (r.get("InfoHash") or "").upper()
                    mag = r.get("MagnetUri") or ""
                    if not ih and mag:
                        ih = extract_infohash(mag)
                    out.append({
                        "idx": idx, "title": r.get("Title") or "",
                        "size": int(r.get("Size") or 0), "seeders": int(r.get("Seeders") or 0),
                        "magnet": mag, "link": r.get("Link") or "",
                        "infohash": ih,
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
    with urllib.request.urlopen(req, timeout=CFG.get("search_timeout", 30)) as r:
        xml = r.read().decode("utf-8", "ignore")
    out = []
    for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
        def tag(t):
            m = re.search(rf"<{t}[^>]*>(.*?)</{t}>", item, re.S)
            return html_mod.unescape(m.group(1).strip()) if m else ""
        title = tag("title")
        link = tag("link")
        ih = extract_infohash(link)
        if not title or not ih:
            continue
        desc = tag("description")
        dm = re.search(r"<br\s*/?>([0-9.]+)\s*(GB|MB|KB)", desc, re.I)
        size = 0
        if dm:
            mult = {"GB": 1024**3, "MB": 1024**2, "KB": 1024}[dm.group(2).upper()]
            size = int(float(dm.group(1)) * mult)
        # RSS 不含做种数 → 给 0, 排序时靠其他索引器补充
        out.append({"idx": "bt4g", "title": title, "size": size, "seeders": 0,
                    "magnet": link, "link": "", "infohash": ih,
                    "pub": tag("pubDate")})
    return out

def extract_torrent_infohash(raw: bytes) -> str:
    """从 .torrent 文件二进制数据解析根字典并计算 info 的 SHA1 infohash"""
    def skip_bencode(data: bytes, pos: int) -> int:
        ch = data[pos:pos+1]
        if ch == b'i':
            end = data.find(b'e', pos + 1)
            if end == -1: raise ValueError("invalid bencoded int")
            return end + 1
        elif ch.isdigit():
            colon = data.find(b':', pos)
            if colon == -1: raise ValueError("invalid bencoded str")
            slen = int(data[pos:colon])
            return colon + 1 + slen
        elif ch in (b'l', b'd'):
            curr = pos + 1
            while data[curr:curr+1] != b'e':
                curr = skip_bencode(data, curr)
            return curr + 1
        raise ValueError(f"unknown bencode tag: {ch}")

    try:
        if not raw or raw[:1] != b'd':
            return ""
        pos = 1
        while pos < len(raw) and raw[pos:pos+1] != b'e':
            colon = raw.find(b':', pos)
            if colon == -1: break
            klen = int(raw[pos:colon])
            kstart = colon + 1
            kend = kstart + klen
            key = raw[kstart:kend]
            val_start = kend
            val_end = skip_bencode(raw, val_start)
            if key == b'info':
                return hashlib.sha1(raw[val_start:val_end]).hexdigest().upper()
            pos = val_end
    except Exception:
        pass
    return ""

def extract_torrent_meta(raw: bytes):
    """从 .torrent 种子提取 infohash 与 tracker"""
    ih = extract_torrent_infohash(raw)
    trackers = []
    try:
        m_ann = re.search(rb"8:announce(\d+):", raw)
        if m_ann:
            l = int(m_ann.group(1))
            start = m_ann.end()
            ann = raw[start:start+l].decode("utf-8", "ignore")
            if ann.startswith("http://") or ann.startswith("https://") or ann.startswith("udp://"):
                trackers.append(ann)
    except Exception:
        pass
    return ih, trackers

def to_magnet(res):
    """结果 → (magnet, resolved_hint) ; 种子链接先下载字节并用光鸭或本地快速解析"""
    mag = res.get("magnet") or ""
    if mag and mag.startswith("magnet:"):
        return mag, None
    link = res.get("link") or (mag if (mag.startswith("http://") or mag.startswith("https://")) else "")
    if link:
        opener = urllib.request.build_opener(MagnetRedirectHandler)
        try:
            req = urllib.request.Request(link, headers={"User-Agent": "Mozilla/5.0"})
            with opener.open(req, timeout=40) as resp:
                raw = resp.read()
                ih, trackers = extract_torrent_meta(raw)
                d = None
                try:
                    d = gy_resolve_torrent(raw)
                except Exception as ge:
                    log(f"光鸭上传解析种子字节异常: {ge}")
                if not ih and d:
                    data_obj = d.get("data") if isinstance(d.get("data"), dict) else {}
                    ih = data_obj.get("infohash") or d.get("infohash") or data_obj.get("hash") or d.get("hash")
                if not ih:
                    ih = res.get("infohash")
                if not ih:
                    return None, None
                mag_out = f"magnet:?xt=urn:btih:{ih.upper()}"
                if res.get("title"):
                    mag_out += f"&dn={urllib.parse.quote(res['title'])}"
                for tr in trackers[:3]:
                    mag_out += f"&tr={urllib.parse.quote(tr)}"
                return mag_out, d
        except MagnetRedirectException as me:
            return me.url, None
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location") or ""
            if e.code in (301, 302, 303, 307, 308) and loc.startswith("magnet:"):
                return loc, None
            if res.get("infohash"):
                return f"magnet:?xt=urn:btih:{res['infohash'].upper()}", None
            raise
        except Exception as e:
            log(f"下载种子并转换磁链失败 ({link}): {e}")
            if res.get("infohash"):
                return f"magnet:?xt=urn:btih:{res['infohash'].upper()}", None
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

def sanitize(name):
    # 过滤文件系统与光鸭云盘非法字符: \ / : * ? " < > |
    return re.sub(r'[\\/:*?"<>|]+', " ", name).strip()[:120]

def show_key(name):
    clean = sanitize(name)
    return re.sub(r"\s+", " ", clean).strip().title()

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
    return sanitize(re.sub(r"\s+", " ", t).strip(" .-")[:70])

def is_video(name):
    return name.lower().endswith((".mkv", ".mp4", ".ts", ".m2ts", ".avi", ".mov", ".wmv", ".flv", ".rmvb"))

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
    """选最佳内嵌字幕流: 双语中英 > 简体中文 > 繁体中文 > 其它中文 > 英文; 返回 {index, lang, codec} 或 None"""
    streams = ffprobe_streams(video_path)
    subs = [s for s in streams if s.get("codec_type") == "subtitle"
            and s.get("codec_name") not in ("hdmv_pgs_subtitle", "dvd_subtitle", "subrip_png")]
    if not subs:
        return None
    def score(s):
        lang = ((s.get("tags") or {}).get("language") or "").lower()
        title = (((s.get("tags") or {}).get("title")) or "").lower()
        zh_tags = ("zh", "chi", "zho", "chs", "cht", "sc", "tc", "chinese", "mandarin", "cantonese")
        zh = any(t in lang for t in zh_tags) or any(k in title for k in ("简", "繁", "中", "chs", "cht", "chi", "chinese", "双语", "中英"))
        bilingual = any(k in title for k in ("双语", "中英", "chs&eng", "cht&eng", "cn&en", "bilingual"))
        chs = any(k in title for k in ("简", "chs", "gb", "sc")) or lang in ("chs", "sc")
        cht = any(k in title for k in ("繁", "cht", "big5", "tc")) or lang in ("cht", "tc")
        en = any(t in lang for t in ("en", "eng", "english")) or "eng" in title
        if zh:
            l_score = 0 if bilingual else (1 if chs else (2 if cht else 3))
        elif en:
            l_score = 4
        else:
            l_score = 5
        is_ass = 0 if s.get("codec_name") in ("ass", "ssa") else 1
        return (l_score, "sdh" in title, is_ass)
    subs.sort(key=score)
    best = subs[0]
    best_score = score(best)[0]
    if best_score > 4:
        return None
    lang_res = "zh" if best_score < 4 else "en"
    return {"index": best["index"], "lang": lang_res, "codec": best.get("codec_name", "")}

def extract_embedded_sub(video_path, stream_index, dst_path):
    if dst_path.lower().endswith(".ass"):
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", video_path,
               "-map", f"0:{stream_index}", "-c:s", "copy", dst_path]
    else:
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", video_path,
               "-map", f"0:{stream_index}", "-c:s", "subrip", dst_path]
    r = subprocess.run(cmd, capture_output=True, timeout=600)
    if r.returncode == 0 and os.path.exists(dst_path) and os.path.getsize(dst_path) > 200:
        return True
    # 回退 copy
    cmd_fallback = ["ffmpeg", "-y", "-v", "error", "-i", video_path,
                    "-map", f"0:{stream_index}", "-c", "copy", dst_path]
    r2 = subprocess.run(cmd_fallback, capture_output=True, timeout=600)
    return r2.returncode == 0 and os.path.exists(dst_path) and os.path.getsize(dst_path) > 200

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

def shooter_file_hash(path):
    """计算射手网(Shooter)复合特征哈希: 4个4KB数据块的MD5拼接"""
    try:
        size = os.path.getsize(path)
        if size < 8192:
            return None
        offsets = [4096, int(size/3*2), int(size/3), size - 8192]
        hashes = []
        with open(path, 'rb') as f:
            for offset in offsets:
                f.seek(offset)
                block = f.read(4096)
                hashes.append(hashlib.md5(block).hexdigest())
        return ';'.join(hashes)
    except Exception:
        return None

def shooter_fetch(video_path):
    """通过射手网开放 API 获取中文字幕。无需登录，根据视频特征哈希直接秒级精准匹配。"""
    try:
        fhash = shooter_file_hash(video_path)
        if not fhash: return None
        fname = os.path.basename(video_path)
        url = "https://www.shooter.cn/api/subapi.php"
        data = urllib.parse.urlencode({
            "filehash": fhash,
            "pathinfo": fname,
            "format": "json"
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"User-Agent": "ShooterPlayer"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
        if not raw or raw.strip() == b"\xff" or raw.strip() == b"":
            return None
        res_list = json.loads(raw.decode("utf-8", "ignore"))
        if not isinstance(res_list, list) or not res_list:
            return None
        candidates = []
        for item in res_list:
            desc = (item.get("Desc") or "").lower()
            files = item.get("Files") or []
            for f in files:
                link = f.get("Link")
                ext = (f.get("Ext") or "srt").lower()
                if link:
                    candidates.append({"desc": desc, "ext": ext, "link": link})
        if not candidates:
            return None
        def cand_score(c):
            d = c["desc"]
            bilingual = any(x in d for x in ["中英", "双语", "chs&eng", "cht&eng", "bilingual"])
            chs = any(x in d for x in ["简", "chs", "gb", "sc", "chinese"])
            cht = any(x in d for x in ["繁", "cht", "big5", "tc"])
            lang_rank = 0 if bilingual else (1 if chs else (2 if cht else 3))
            fmt_rank = 0 if c["ext"] in ("ass", "ssa") else 1
            return (lang_rank, fmt_rank)
        candidates.sort(key=cand_score)
        best = candidates[0]
        req_dl = urllib.request.Request(best["link"], headers={"User-Agent": "ShooterPlayer"})
        with urllib.request.urlopen(req_dl, timeout=25) as resp_dl:
            sub_bytes = resp_dl.read()
        if sub_bytes.startswith(b"\x1f\x8b"):
            sub_bytes = gzip.decompress(sub_bytes)
        data_clean, ext_clean = extract_sub_archive(sub_bytes)
        if data_clean:
            return data_clean, ext_clean
        return sub_bytes, best["ext"]
    except Exception as e:
        log(f"射手网 (Shooter) 字幕查询异常: {e}")
        return None

def find_subtitle(video_path, hint):
    """字幕策略:
    1) 已有同名外部字幕跳过
    2) 内嵌中文字幕 (双语 > 简中 > 繁中) 提取为 .zh.ass / .zh.srt
    3) 射手网开放 API (免账号视频特征哈希匹配，高精度中文字幕)
    4) SubHD 下载中文字幕 (若配置账号)
    5) 内嵌英文字幕提取兜底 (.en.srt)
    """
    base = os.path.splitext(video_path)[0]
    if any(os.path.exists(p) for p in [f"{base}.zh.ass", f"{base}.zh.srt", f"{base}.chs.srt",
                                        f"{base}.cht.srt", f"{base}.srt", f"{base}.ass"]):
        return ["已有"]

    # 2. 内嵌中文字幕优先(快且准); ass 保留样式, 失败再退 srt
    pick = pick_embedded_sub(video_path)
    if pick and pick["lang"] == "zh":
        target_ext = "ass" if pick.get("codec") in ("ass", "ssa") else "srt"
        for ext_out in (target_ext, "srt" if target_ext != "srt" else "ass"):
            dst = f"{base}.zh.{ext_out}"
            if extract_embedded_sub(video_path, pick["index"], dst):
                try: os.chmod(dst, 0o666)
                except Exception: pass
                return [f"内嵌提取(zh) → {os.path.basename(dst)}"]

    # 3. 射手网开放 API (免账号、零配置、依据视频精确哈希下载)
    sh_res = shooter_fetch(video_path)
    if sh_res:
        data, ext = sh_res
        dst = f"{base}.zh.{ext}"
        try:
            with open(dst, "wb") as f:
                f.write(data)
            try: os.chmod(dst, 0o666)
            except Exception: pass
            return [f"射手网(Shooter) → {os.path.basename(dst)}"]
        except Exception as e:
            log(f"写入射手网字幕失败: {e}")

    # 4. SubHD 下载中文字幕(账号配置后); 多候选逐个试
    op = subhd_session()
    if op:
        for raw in subhd_fetch(op, hint, limit=3):
            data, ext = extract_sub_archive(raw)
            if data:
                dst = f"{base}.zh.{ext}"
                try:
                    with open(dst, "wb") as f: f.write(data)
                    try: os.chmod(dst, 0o666)
                    except Exception: pass
                    return [f"SubHD → {os.path.basename(dst)}"]
                except Exception as e:
                    log(f"写入 SubHD 字幕失败: {e}")

    # 5. 兜底: 内嵌英文字幕
    if pick and pick["lang"] == "en":
        dst = f"{base}.en.srt"
        if extract_embedded_sub(video_path, pick["index"], dst):
            try: os.chmod(dst, 0o666)
            except Exception: pass
            return [f"内嵌提取(en兜底) → {os.path.basename(dst)}"]

    return ["未找到中文字幕"]

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
    gy_check_login()
    magnet, resolved_hint = to_magnet(res)
    if not magnet: raise RuntimeError("无法获得磁链")

    resolved = None
    if resolved_hint:
        hint_data = resolved_hint.get("data") if isinstance(resolved_hint.get("data"), dict) else None
        if hint_data and (hint_data.get("files") or hint_data.get("totalSize")):
            resolved = hint_data
        elif resolved_hint.get("files") or resolved_hint.get("totalSize"):
            resolved = resolved_hint

    if not resolved:
        resolved = gy_resolve(magnet)

    res_data = resolved.get("data") if isinstance(resolved.get("data"), dict) else {}
    files = resolved.get("files") or res_data.get("files") or []
    total_size = resolved.get("totalSize") or res_data.get("totalSize") or 0
    title_cloud = resolved.get("title") or res_data.get("title") or ""

    # 单文件磁力兜底: 光鸭 resolve 后 files 可能为空，但 title/totalSize 明确代表单文件
    if not files and total_size >= (min_mb or CFG["min_video_mb"]) * MB:
        single_title = title_cloud or label
        if is_video(single_title) or (not os.path.splitext(single_title)[1] and (is_video(res.get("title") or "") or parse_title(res.get("title") or "").get("quality") in ("2160p", "1080p", "720p"))):
            if not os.path.splitext(single_title)[1]:
                single_title = f"{single_title}.mkv"
            files = [{
                "fileIndex": 0,
                "fileName": single_title,
                "fileSize": total_size,
                "selected": True
            }]

    vids = [f for f in files if is_video(f.get("fileName") or "")
            and (f.get("fileSize") or 0) >= (min_mb or CFG["min_video_mb"]) * MB
            and "sample" not in (f.get("fileName") or "").lower()]

    # 若过滤为空，但有且仅有一个大文件且无扩展名，兜底视为视频文件
    if not vids and len(files) == 1 and (files[0].get("fileSize") or 0) >= (min_mb or CFG["min_video_mb"]) * MB:
        fn0 = files[0].get("fileName") or ""
        if not os.path.splitext(fn0)[1]:
            files[0]["fileName"] = f"{fn0}.mkv"
            vids = [files[0]]

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
    idxs = [f["fileIndex"] for f in vids] if (files and len(files) > 0) else None

    # 确定目标文件名后缀并保持完整扩展名 (彻底解决光鸭网盘和落地 NAS 无后缀名问题)
    vid_ext = ""
    if vids:
        vid_ext = os.path.splitext(vids[0].get("fileName") or "")[1].lower()
    if not vid_ext and title_cloud:
        vid_ext = os.path.splitext(title_cloud)[1].lower()
    if not vid_ext or vid_ext not in (".mkv", ".mp4", ".ts", ".avi", ".mov", ".wmv", ".flv", ".rmvb"):
        vid_ext = ".mkv"

    clean_lbl = sanitize(label)
    if idxs is None or len(vids) == 1:
        lbl_ext = os.path.splitext(clean_lbl)[1].lower()
        if not lbl_ext or lbl_ext not in (".mkv", ".mp4", ".ts", ".avi", ".mov", ".wmv"):
            cloud_name = f"{clean_lbl}{vid_ext}"
        else:
            cloud_name = clean_lbl
    else:
        cloud_name = clean_lbl

    parent_id = gy_ensure_path(parent_path)
    tid = gy_add_magnet(magnet, parent_id, idxs, cloud_name)
    return tid, magnet, vids

# ---------------------------------------------------------------- 推进任务
def phase_cloud(st):
    """stage=cloud: 查云任务, 完成则启动 NAS 下载"""
    tasks = {str(t.get("taskId")): t for t in gy_cloud_tasks() if t.get("taskId") is not None}
    for k, t in list(st["tasks"].items()):
        if t["stage"] != "cloud": continue
        ct = tasks.get(str(t.get("cloudTaskId")))
        if not ct:
            found = [c for c in tasks.values()
                     if t.get("hash") and t["hash"] in (c.get("res") or "").upper()]
            ct = found[0] if found else None
            if ct: t["cloudTaskId"] = str(ct.get("taskId"))
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
        done = False
        if prog is not None:
            try:
                done = float(str(prog).rstrip("%").strip()) >= 100.0
            except Exception:
                done = False
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
                root_n, ext_n = os.path.splitext(name)
                if not ext_n or ext_n.lower() not in (".mkv", ".mp4", ".ts", ".avi", ".mov", ".wmv"):
                    name = f"{root_n}.mkv"
                return [(fid, name, default_size)]
        except Exception:
            pass
        return []

    for e in entries:
        if is_file_entry(e):
            fn = e.get("fileName") or ""
            sz = e.get("fileSize") or 0
            if is_video(fn):
                out.append((e["fileId"], fn, sz))
            elif not os.path.splitext(fn)[1] and sz >= CFG["min_video_mb"] * MB:
                # 兼容旧版本上传无扩展名的视频文件
                out.append((e["fileId"], f"{fn}.mkv", sz))
        else:
            out += walk_videos(e["fileId"], depth + 1)
    return out

def start_nas_for_task(st, k, t, fallback_name=None, fallback_size=0):
    vids = walk_videos(t["cloudFileId"], default_name=fallback_name, default_size=fallback_size)
    if not vids:
        t["empty_cloud_retries"] = t.get("empty_cloud_retries", 0) + 1
        if t["empty_cloud_retries"] <= 3:
            log(f"⚠️ 云盘暂未索引到视频文件，等待下轮重试({t['empty_cloud_retries']}/3): {t['label']}")
            return
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
        # 严格保证下载到 NAS 的文件名包含合法视频后缀！
        root_fn, ext_fn = os.path.splitext(fname)
        if not ext_fn or ext_fn.lower() not in (".mkv", ".mp4", ".ts", ".avi", ".mov", ".wmv"):
            fname = f"{root_fn}.mkv"
        clean_fname = sanitize(fname)
        out = os.path.join(target, clean_fname)
        if (os.path.exists(out) and not os.path.exists(out + ".aria2")
                and os.path.getsize(out) >= size * 0.95 and size > 0):
            continue  # 已完整存在
        # 中断残留(prealloc 全尺寸但无下载进程): 删掉让 aria2 从头下, 否则可能秒完但内容是空的
        if os.path.exists(out + ".aria2"):
            try: os.remove(out); os.remove(out + ".aria2")
            except Exception: pass
        tid = gy_nas_start(fid, clean_fname, target)
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
        all_completed = True
        has_failed = False
        target_dir = t.get("nas_dir")

        for n in t.get("nas", []):
            tr = transfers.get(str(n.get("nasTaskId"))) or {}
            st_status = str(tr.get("status") or "").lower()
            st_pct = tr.get("percent")
            try:
                prog_val = float(str(tr.get("progress") or 0).rstrip("%").strip())
            except Exception:
                prog_val = 0.0

            # 1. 显式完成状态检测 (支持各类 Guangya 返回形式: completed, done, success, 2, 或 percent 100)
            is_tr_done = (st_status in ("completed", "complete", "finished", "done", "success", "2")
                          or st_pct == 100 or prog_val >= 100.0)

            if is_tr_done:
                continue

            # 2. 显式失败检测
            if st_status in ("failed", "cancelled", "error"):
                has_failed = True
                all_completed = False
                continue

            # 3. 正在下载中状态
            if st_status in ("downloading", "getting_url", "pending", "active", "waiting"):
                all_completed = False
                continue

            # 4. transfers 中已无该任务 (Guangya 已将完成任务移出 transfers 队列，或者服务重启)
            # 此时检查 NAS 本地磁盘实际文件是否存在并完整
            path = n.get("file")
            file_found = False

            # (a) 首先检查登记的绝对路径
            if path and os.path.exists(path) and not os.path.exists(path + ".aria2"):
                sz = os.path.getsize(path)
                expected = n.get("size", 0)
                if not expected or sz >= expected * 0.95:
                    file_found = True

            # (b) 若登记路径未直接匹配，在 target_dir 中查找匹配的物理落地视频
            if not file_found and target_dir and os.path.isdir(target_dir):
                base_name = os.path.splitext(os.path.basename(path))[0] if path else ""
                for candidate in os.listdir(target_dir):
                    if candidate.endswith(".aria2"): continue
                    cand_path = os.path.join(target_dir, candidate)
                    if not os.path.isfile(cand_path): continue
                    cand_size = os.path.getsize(cand_path)
                    expected = n.get("size", 0)
                    if (base_name and base_name.lower() in candidate.lower()) or (expected and cand_size >= expected * 0.95):
                        n["file"] = cand_path
                        file_found = True
                        break

            if file_found:
                continue
            else:
                has_failed = True
                all_completed = False

        if has_failed:
            t["retries"] = t.get("retries", 0) + 1
            if t["retries"] <= 3:
                log(f"⚠️ NAS下载未检测到完成，重试 {t['label']} (第{t['retries']}/3次)")
                start_nas_for_task(st, k, t)
            else:
                t["stage"] = "failed"
                t["error"] = "NAS下载失败(重试3次未检测到落地文件)"
                event(f"失败 {t['label']}: NAS下载失败")
            continue

        if all_completed:
            finish_task(st, k, t)

def finish_task(st, k, t):
    t["stage"] = "done"  # 1. 率先置为 done，绝不会因为字幕失败被篡改为 failed！
    t["done_at"] = time.time()
    st["waiting"].pop(t.get("show") or "", None)

    # 2. 尝试提取/下载字幕（即使出错也不影响主任务入库）
    subs = []
    try:
        for n in t.get("nas", []):
            fpath = n.get("file")
            if not fpath or not os.path.exists(fpath): continue
            hint = t.get("sub_hint") or os.path.splitext(os.path.basename(fpath))[0][:40]
            try:
                r = find_subtitle(fpath, hint)
                subs.append(f"{os.path.basename(fpath)}: {','.join(r)}")
            except Exception as se:
                log(f"字幕抓取异常({fpath}): {se}")
    except Exception as e:
        log(f"字幕处理外层异常: {e}")

    # 3. 剧集 / 电影 心愿单状态持久化
    try:
        if t.get("kind") == "tv" and t.get("show"):
            cur_shows = shows()
            for sh in cur_shows:
                if show_key(sh["name"]) == t["show"]:
                    s, e = t.get("ep", (0, 0))
                    if (s, e) > tuple(sh.get("last", (0, 0))):
                        sh["last"] = [s, e]
                        save_shows(cur_shows)
                    break
        elif t.get("kind") == "movie":
            wl = load_json(WISH_F, {})
            for wk, w in wl.items():
                if show_key(w.get("name", "")) == show_key(t.get("label", "")) or wk == show_key(t.get("label", "")):
                    w["status"] = "done"
                    save_json(WISH_F, wl)
                    break
    except Exception as pe:
        log(f"状态更新持久化异常: {pe}")

    save_state(st)
    event(f"完成 {t['label']} | 字幕: {'; '.join(subs) or '无'}")
    if CFG.get("cleanup_cloud") and t.get("cloudTaskId"):
        try: gy_post("/api/cloud-add/delete", {"taskId": t["cloudTaskId"], "deleteFile": True})
        except Exception: pass

# ---------------------------------------------------------------- 搜新集与追踪
def search_new_episodes(st, dry=False):
    submitted = 0
    cur_shows = shows()
    shows_changed = False
    for sh in cur_shows:
        if not sh.get("enabled", True): continue
        name = show_key(sh["name"])
        last_s, last_e = tuple(sh.get("last", [0, 0]))
        if last_s == 0:
            last_s = 1
            last_e = 0
            sh["last"] = [1, 0]
            shows_changed = True
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

        # 跨季探测: 若当前季无新集且已有进度(last_e > 0)，自动探测下一季第1集上线
        if not wanted and last_e > 0:
            next_s = last_s + 1
            try:
                next_results = jackett_search(f"{sh['name']} S{next_s:02d}", category=5000)
                next_by_ep = {}
                for r in next_results:
                    p = parse_title(r["title"])
                    if p["bad"] or p["season"] != next_s or not p["ep"]: continue
                    if p["quality"] not in wanted_qualities(): continue
                    if not format_allowed(p) or not size_ok(r["size"] / (1024 ** 3)): continue
                    if not lang_ok(p.get("langs") or set()): continue
                    r["_p"] = p
                    if p["ep"] not in next_by_ep or rank_key(r) < rank_key(next_by_ep[p["ep"]]):
                        next_by_ep[p["ep"]] = r
                next_wanted = [e for e in sorted(next_by_ep) if not already_have_video(name, next_s, e)]
                if next_wanted:
                    log(f"🎉 检测到 {name} 新一季上线 (S{next_s:02d}E{next_wanted[0]:02d})，自动转入第 {next_s} 季追踪")
                    last_s = next_s
                    last_e = 0
                    sh["last"] = [next_s, 0]
                    shows_changed = True
                    by_ep = next_by_ep
                    wanted = next_wanted
            except Exception as ex_next:
                log(f"跨季探测异常 {name}: {ex_next}")

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
                ih = extract_infohash(magnet)
                st["tasks"][k] = {"kind": "tv", "show": name, "season": last_s, "ep": [last_s, e],
                                  "label": label, "cloudTaskId": str(tid), "hash": ih,
                                  "nas_dir": os.path.join(CFG["media_root"], name),
                                  "sub_hint": f"{sh['name']} S{last_s:02d}E{e:02d}",
                                  "added": time.time(), "stage": "cloud", "wanted": [(last_s, e)]}
                if ih: st["processed"].append(ih)
                submitted += 1
                event(f"已提交 {label} ← {r['title'][:60]} ({r['idx']}, {r['seeders']}seeds)")
                time.sleep(2)
            except Exception as ex:
                log(f"提交失败 {label}: {ex}")
                st["waiting"].setdefault(name, {})[f"S{last_s:02d}E{e:02d}"] = f"提交失败: {ex}"
    if shows_changed and not dry:
        save_shows(cur_shows)

def search_movies(st, dry=False):
    """周期追踪心愿单中未完成的电影：搜索 2160p 资源，上线即自动提交下载"""
    wl = load_json(WISH_F, {})
    if not wl: return
    changed = False
    submitted = 0
    for key, item in list(wl.items()):
        name = item.get("name", "").strip()
        kind = item.get("kind", "auto")
        if not name: continue
        if kind == "tv" or is_tv_query(name): continue
        if item.get("status") in ("done", "downloading"): continue

        clean_name = show_key(name)
        movie_dir = os.path.join(CFG["media_root"], "电影", clean_name)
        if os.path.isdir(movie_dir):
            vids = [f for f in os.listdir(movie_dir) if is_video(f)]
            if vids:
                item["status"] = "done"
                changed = True
                continue

        in_tasks = any(t.get("kind") == "movie" and clean_name in show_key(t.get("label", ""))
                       and t.get("stage") in ("cloud", "nas") for t in st["tasks"].values())
        if in_tasks:
            item["status"] = "downloading"
            changed = True
            continue

        trans_title, _ = cn2en(name)
        search_query = trans_title or name
        try:
            results = jackett_search(search_query, category=2000)
        except Exception as e:
            log(f"电影追踪搜索失败 {name}: {e}")
            continue

        best_4k = best_for_movie(results, CFG["wanted_quality"])
        target_res = best_4k
        if not target_res and "1080p" in (CFG.get("quality_fallback") or []):
            target_res = best_for_movie(results, "1080p")

        if not target_res:
            st["waiting"][clean_name] = "未找到 4K 资源，等待上线中"
            item["status"] = "waiting_4k"
            changed = True
            continue

        if dry:
            print(f"[dry] 电影 {clean_name} 匹配到: {target_res['title']} ({target_res['seeders']}seeds)")
            continue

        if submitted >= CFG.get("max_submit_per_run", 3):
            log(f"电影 {clean_name}: 本轮提交已达上限，等下轮")
            break

        label = clean_release(target_res["title"])
        try:
            tid, magnet, vids = submit(target_res, label, [CFG["cloud_root"], "电影", clean_name])
            k = f"t{int(time.time()*1000)}"
            ih = extract_infohash(magnet)
            st["tasks"][k] = {
                "kind": "movie", "label": label, "cloudTaskId": str(tid),
                "hash": ih, "nas_dir": movie_dir, "sub_hint": search_query[:40],
                "added": time.time(), "stage": "cloud"
            }
            if ih: st["processed"].append(ih)
            submitted += 1
            item["status"] = "downloading"
            changed = True
            st["waiting"].pop(clean_name, None)
            event(f"电影自动追踪提交: {clean_name} ← {target_res['title'][:60]} ({target_res['idx']})")
            time.sleep(2)
        except Exception as ex:
            log(f"电影 {clean_name} 自动提交失败: {ex}")
            st["waiting"][clean_name] = f"提交失败: {ex}"

    if changed and not dry:
        save_json(WISH_F, wl)

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
            log(f"开始搜新集与电影({why})")
            search_new_episodes(st, dry=dry)
            search_movies(st, dry=dry)
            if not dry:
                st["last_search"] = time.time()
        else:
            log(f"跳过搜新集与电影({why})")
    # 清理 done>30天
    now = time.time()
    for k in [k for k, t in st["tasks"].items() if t["stage"] in ("done", "failed")
              and now - t.get("added", 0) > 30 * 86400]:
        st["tasks"].pop(k, None)

# ---------------------------------------------------------------- CLI
def is_tv_query(t): return bool(RE_EP.search(t) or RE_SEASON.search(t))

def prompt_or_fallback_1080(results, args, filter_fn=None):
    """当 4K 资源不存在时，根据 --allow1080 配置或终端交互询问用户是否降级为 1080p"""
    candidates = []
    for r in results:
        p = parse_title(r["title"])
        if p["bad"] or p["quality"] != "1080p": continue
        if filter_fn and not filter_fn(r): continue
        r["_p"] = p
        candidates.append(r)
    candidates.sort(key=rank_key)
    if not candidates:
        return None

    if getattr(args, "allow1080", False) or "1080p" in (CFG.get("quality_fallback") or []):
        print(f"💡 未找到 2160p(4K) 资源，根据允许降级策略自动选用最佳 1080p: {candidates[0]['title'][:60]}")
        return candidates[0]

    if sys.stdin.isatty():
        print("\n⚠️ 未找到 2160p(4K) 优质资源，检测到以下 1080p 候选资源：")
        for i, c in enumerate(candidates[:5], 1):
            sz = round(c["size"] / (1024*1024*1024), 2)
            print(f"  [{i}] {c['title'][:66]} | {c['seeders']}做种 | {sz}GB | {c['idx']}")
        try:
            ans = input("\n是否降级下载最佳 1080p 资源？[Y/n/序号]: ").strip()
            if ans == "" or ans.lower() in ("y", "yes"):
                return candidates[0]
            elif ans.isdigit() and 1 <= int(ans) <= len(candidates[:5]):
                return candidates[int(ans) - 1]
            else:
                print("用户取消下载。")
                sys.exit(2)
        except (KeyboardInterrupt, EOFError):
            print("\n已取消")
            sys.exit(2)
    return None

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
    exact_title = getattr(args, "exact_title", "") or ""
    direct_magnet = getattr(args, "magnet", "") or ""
    direct_infohash = getattr(args, "infohash", "") or ""

    if direct_magnet:
        # 用户在前端或命令行明确选中某个资源，直接提交该资源，无需二次检索
        name = show_key(title)
        is_tv = bool(m) or is_tv_query(title)
        clean_lbl = clean_release(exact_title or title)
        if is_tv:
            s, e = (int(m.group(1)), int(m.group(2))) if m else (1, 1)
            parent_p = [CFG["cloud_root"], name]
            nas_p = os.path.join(CFG["media_root"], name)
            sub_hint = f"{name} S{s:02d}E{e:02d}"
            label = f"{name} S{s:02d}E{e:02d}"
            task_kind = "tv"
            wanted = {(s, e)}
        else:
            parent_p = [CFG["cloud_root"], "电影", name]
            nas_p = os.path.join(CFG["media_root"], "电影", name)
            sub_hint = title[:40]
            label = clean_lbl
            task_kind = "movie"
            wanted = None

        is_mag = direct_magnet.startswith("magnet:")
        res_direct = {
            "magnet": direct_magnet if is_mag else "",
            "link": direct_magnet if not is_mag else "",
            "infohash": direct_infohash or (extract_infohash(direct_magnet) if is_mag else ""),
            "title": exact_title or title,
            "size": 0, "seeders": 1, "idx": "direct"
        }
        print(f"直接提交选中资源: {label}")
        tid, magnet, vids = submit(res_direct, label, parent_p, wanted_eps=wanted)
        k = f"t{int(time.time()*1000)}"
        ih = extract_infohash(magnet) or direct_infohash
        task_data = {"kind": task_kind, "label": label, "cloudTaskId": str(tid),
                     "hash": ih, "nas_dir": nas_p, "sub_hint": sub_hint,
                     "added": time.time(), "stage": "cloud"}
        if is_tv and m:
            task_data.update({"show": name, "season": int(m.group(1)), "ep": [int(m.group(1)), int(m.group(2))], "wanted": [(int(m.group(1)), int(m.group(2)))]})
        st["tasks"][k] = task_data
        if ih: st["processed"].append(ih)
        save_state(st)
        event(f"已创建指定资源云下载: {label}")
        print(f"云任务 {tid} 已创建, 之后 run/poll 会自动推进到NAS并补字幕")
        return

    if m:  # 单集
        s, e = int(m.group(1)), int(m.group(2))
        show_guess = re.split(r"\bS\d{1,2}E\d{1,3}\b", title, flags=re.I)[0]
        name = show_key(show_guess)
        results = jackett_search(title, category=5000)
        best = None
        if exact_title:
            matches = [r for r in results if r["title"] == exact_title or exact_title in r["title"]]
            if matches: best = matches[0]
        if not best:
            best = best_for_episode(results, s, e, CFG["wanted_quality"])
        if not best:
            best = prompt_or_fallback_1080(results, args, lambda r: best_for_episode([r], s, e, "1080p"))
        if not best:
            no4k_exit(results)
        print(f"提交: {name} S{s:02d}E{e:02d} ← {best['title'][:70]}")
        tid, magnet, vids = submit(best, f"{name} S{s:02d}E{e:02d}", [CFG["cloud_root"], name],
                                   wanted_eps={(s, e)})
        k = f"t{int(time.time()*1000)}"
        ih = extract_infohash(magnet)
        st["tasks"][k] = {"kind": "tv", "show": name, "season": s, "ep": [s, e],
                          "label": f"{name} S{s:02d}E{e:02d}", "cloudTaskId": str(tid),
                          "hash": ih,
                          "nas_dir": os.path.join(CFG["media_root"], name),
                          "sub_hint": f"{name} S{s:02d}E{e:02d}",
                          "added": time.time(), "stage": "cloud", "wanted": [(s, e)]}
        if ih: st["processed"].append(ih)
        save_state(st)
        event(f"已创建追剧云下载: {name} S{s:02d}E{e:02d}")
        print(f"云任务 {tid} 已创建, 之后 run/poll 会自动推进到NAS并补字幕")
    elif is_tv_query(title):
        sys.exit("暂不支持整季包下载, 请用单集标题(如 剧名 S02E07)或 addshow 登记追踪")
    else:
        name = show_key(title)
        results = jackett_search(title, category=2000)
        best = None
        if exact_title:
            matches = [r for r in results if r["title"] == exact_title or exact_title in r["title"]]
            if matches: best = matches[0]
        if not best:
            best = best_for_movie(results, CFG["wanted_quality"])
        if not best:
            best = prompt_or_fallback_1080(results, args, lambda r: best_for_movie([r], "1080p"))
        if not best:
            no4k_exit(results)
        print(f"提交电影: {name} ← {best['title'][:70]}")
        label = clean_release(best["title"])
        tid, magnet, vids = submit(best, label, [CFG["cloud_root"], "电影", name])
        k = f"t{int(time.time()*1000)}"
        ih = extract_infohash(magnet)
        st["tasks"][k] = {"kind": "movie", "label": label, "cloudTaskId": str(tid),
                          "hash": ih,
                          "nas_dir": os.path.join(CFG["media_root"], "电影", name), "sub_hint": title[:40],
                          "added": time.time(), "stage": "cloud"}
        if ih: st["processed"].append(ih)
        save_state(st)
        event(f"已创建电影云下载: {label}")
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
    last = [1, 0]
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
    p = sub.add_parser("add"); p.add_argument("title"); p.add_argument("--allow1080", action="store_true"); p.add_argument("--exact-title", default=""); p.add_argument("--magnet", default=""); p.add_argument("--infohash", default=""); p.set_defaults(fn=cmd_add)
    p = sub.add_parser("addshow"); p.add_argument("name"); p.add_argument("--last", default=""); p.set_defaults(fn=cmd_addshow)
    p = sub.add_parser("rmshow"); p.add_argument("name"); p.set_defaults(fn=cmd_rmshow)
    p = sub.add_parser("shows"); p.set_defaults(fn=cmd_shows)
    p = sub.add_parser("status"); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("run"); p.add_argument("--dry", action="store_true"); p.add_argument("--search-only", action="store_true"); p.set_defaults(fn=cmd_run)
    p = sub.add_parser("poll"); p.set_defaults(fn=lambda a: (save_state((lambda s: (cycle(s, search=False), s)[1])(state()))))
    p = sub.add_parser("sub"); p.add_argument("file"); p.set_defaults(fn=cmd_sub)
    args = ap.parse_args()
    if args.cmd in ("run", "poll"):
        lock_file = "/tmp/gy_autodl.lock" if os.path.exists("/tmp") else os.path.join(DATA, "autodl.lock")
        try:
            lock = open(lock_file, "w")
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            log("已有 run 在执行, 跳过"); return
        args.fn(args)
    else:
        args.fn(args)

if __name__ == "__main__":
    main()
