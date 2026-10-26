# -*- coding: utf-8 -*-
"""机器人可调用的工具集合。

每个工具都是普通函数，返回字符串（给大模型看）。
危险动作（评论）受 config 开关和每日配额限制。
"""
import json
import os
import re
import time
from html import unescape
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state")
MEM = os.path.join(STATE, "memory.json")
CFG = os.path.join(HERE, "llm.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
NAPCAT = os.environ.get("NAPCAT_URL", "http://127.0.0.1:3000")


# ---------------- 基础 ----------------

def _http(url, data=None, headers=None, method=None, timeout=20):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    body = data.encode("utf-8") if isinstance(data, str) else data
    req = Request(url, data=body, headers=h, method=method)
    with urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _napcat(path, payload=None, timeout=20):
    out = _http(NAPCAT + "/" + path,
                data=json.dumps(payload or {}),
                headers={"Content-Type": "application/json"},
                timeout=timeout)
    return json.loads(out)


def _cfg():
    try:
        with open(CFG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# NapCat 容器路径 -> 宿主机路径（get_image 返回的是容器内路径，宿主机直接读不到）
_HOST_QQ_MAP = (
    ("/app/.config/QQ", "/opt/napcat/qq"),
    ("/app/config", "/opt/napcat/config"),
)


def _resolve_path(p):
    """把 get_image 返回的容器内路径转成宿主机可读路径；本来就是本地路径则原样返回。"""
    if not p:
        return ""
    for c, h in _HOST_QQ_MAP:
        if p.startswith(c):
            return h + p[len(c):]
    return p


def _strip_tags(s):
    s = re.sub(r"<script.*?</script>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<style.*?</style>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = unescape(s)
    return re.sub(r"[ \t\r\f\v]+", " ", s).strip()


# ---------------- 时间 ----------------

def tool_now(_args=None):
    return time.strftime("现在是 %Y-%m-%d %H:%M:%S（%A，北京时间）")


# ---------------- 联网搜索 ----------------

SEARCH_ENGINES = {
    "so360": "https://www.so.com/s?q=",
    "bing": "https://cn.bing.com/search?",
    "sogou": "https://www.sogou.com/web?",
    "baidu": "https://www.baidu.com/s?",
}


def _searx(query, count=6):
    """自建 SearXNG（Bing/搜狗/360 聚合），比单引擎稳。"""
    import json as _j
    import urllib.parse as _up
    import urllib.request as _ur
    url = "http://127.0.0.1:8888/search?" + _up.urlencode(
        {"q": query, "format": "json", "language": "zh-CN"})
    req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with _ur.urlopen(req, timeout=30) as r:
        d = _j.loads(r.read().decode("utf-8", "replace"))
    out = []
    for x in (d.get("results") or [])[:count]:
        t = (x.get("title") or "").strip()
        c = (x.get("content") or "").strip()[:180]
        u = x.get("url") or ""
        if t:
            out.append("%s\n%s\n%s" % (t, c, u))
    return "\n\n".join(out)


def tool_web_search(args):
    q = (args.get("query") or "").strip()
    try:
        r = _searx(q)
        if r:
            return "搜索结果（SearXNG 聚合）：\n\n" + r
    except Exception:
        pass
    n = int(args.get("count") or 5)
    if not q:
        return "缺少 query"
    eng = (_cfg().get("search_engine") or "so360").lower()
    base = SEARCH_ENGINES.get(eng) or SEARCH_ENGINES["so360"]
    if base.endswith("?q="):
        url = base + quote(q)
    else:
        url = base + urlencode({"q": q, "count": n})
    try:
        html = _http(url, headers={"Accept-Language": "zh-CN,zh;q=0.9"})
    except Exception as e:
        return "搜索失败：%s" % e

    items = []
    if eng == "so360":
        for m in re.finditer(r'<li class="res-list.*?</li>', html, re.S):
            blk = m.group(0)
            t = re.search(r'<h3[^>]*>.*?<a[^>]*data-mdurl="([^"]+)"[^>]*>(.*?)</a>', blk, re.S)
            if not t:
                continue
            href, title = t.group(1), _strip_tags(t.group(2))
            if not title or len(title) < 4:
                continue
            sn = re.search(r'<p class="res-desc[^"]*"[^>]*>(.*?)</p>', blk, re.S)
            if sn:
                snippet = _strip_tags(sn.group(1))
            else:
                # 兜底：把整段里标题之后的文字截一段
                rest = blk[t.end():] if t else blk
                snippet = _strip_tags(rest)[:200]
            items.append("%d. %s\n   %s\n   %s" % (len(items) + 1, title, href, snippet[:200]))
            if len(items) >= n:
                break
    elif eng == "bing":
        for m in re.finditer(r'<li class="b_algo".*?</li>', html, re.S):
            blk = m.group(0)
            t = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', blk, re.S)
            if not t:
                continue
            href, title = t.group(1), _strip_tags(t.group(2))
            sn = re.search(r'<p[^>]*>(.*?)</p>', blk, re.S)
            snippet = _strip_tags(sn.group(1)) if sn else ""
            items.append("%d. %s\n   %s\n   %s" % (len(items) + 1, title, href, snippet[:220]))
            if len(items) >= n:
                break
    else:
        # 通用兜底：抓 <a href> + 标题，去重
        seen = set()
        for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', html, re.S):
            href, title = m.group(1), _strip_tags(m.group(2))
            if len(title) < 6 or href in seen:
                continue
            seen.add(href)
            items.append("%d. %s\n   %s" % (len(items) + 1, title[:80], href))
            if len(items) >= n:
                break
    if not items:
        return "没搜到结果（%s 可能被限流）" % eng
    return "搜索结果（%s）：\n%s" % (eng, "\n".join(items))


def tool_search_papers(args):
    """学术文献检索，返回标题/作者/年份/DOI/PDF。"""
    import papers
    q = (args.get("query") or "").strip()
    n = max(1, min(int(args.get("count") or 5), 8))
    if not q:
        return "缺少 query"
    try:
        items = papers.search(q, n)
    except Exception as e:
        return "文献检索失败：%s" % e
    return papers.format_results(q, items)


def tool_web_fetch(args):
    url = (args.get("url") or "").strip()
    if not url.startswith("http"):
        return "url 必须以 http 开头"
    try:
        html = _http(url, headers={"Accept-Language": "zh-CN,zh;q=0.9"}, timeout=25)
    except Exception as e:
        return "抓取失败：%s" % e
    text = _strip_tags(html)
    return "URL: %s\n正文（截断 3000 字）：\n%s" % (url, text[:3000])


def tool_markdown(args):
    """MarkItDown 风格：网页或本地文件转成 Markdown（保留标题/列表/结构）。
    优先用 markitdown 库，未安装则退化为纯文本抓取。"""
    url = (args.get("url") or "").strip()
    path = (args.get("file") or "").strip()
    if not url and not path:
        return "需要 url 或 file（本地文件路径）"
    out = ""
    if url:
        if not url.startswith("http"):
            return "url 必须以 http 开头"
        try:
            html = _http(url, headers={"Accept-Language": "zh-CN,zh;q=0.9"}, timeout=25)
        except Exception as e:
            return "抓取失败：%s" % e
        try:
            from markitdown import MarkItDown
            import tempfile
            tmp = tempfile.mktemp(suffix=".html")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(html)
            out = MarkItDown().convert(tmp).text_content or ""
        except Exception:
            out = _strip_tags(html)
    else:
        if not os.path.exists(path):
            return "文件不存在：%s" % path
        try:
            from markitdown import MarkItDown
            out = MarkItDown().convert(path).text_content or ""
        except Exception:
            out = ""
        if not out:
            try:
                out = _strip_tags(open(path, encoding="utf-8", errors="ignore").read())
            except Exception:
                out = ""
    return "内容（Markdown，截断 4000 字）：\n%s" % (out[:4000])


def tool_firecrawl(args):
    """用 Firecrawl 深度抓网页（JS 渲染、结构化）返回 Markdown。
    key 统一读 llm.json 的智谱 vision.api_key（如单独配了 firecrawl_key 则优先用那个）。"""
    url = (args.get("url") or "").strip()
    if not url.startswith("http"):
        return "url 必须以 http 开头"
    _cfgv = _cfg() or {}
    key = _cfgv.get("firecrawl_key") or ""
    if not key:
        return "Firecrawl 未配置 key（请在 llm.json 填 firecrawl_key），可先用 markdown 工具抓取"
    try:
        import json as _json
        body = _json.dumps({"url": url, "formats": ["markdown"]}).encode("utf-8")
        r = _http("https://api.firecrawl.dev/v1/scrape",
                  data=body, method="POST",
                  headers={"Authorization": "Bearer %s" % key,
                           "Content-Type": "application/json"}, timeout=40)
        d = _json.loads(r) if isinstance(r, str) else r
        md = (d.get("data") or {}).get("markdown") or ""
        if not md:
            return "Firecrawl 没返回内容：%s" % (d.get("message") or d)
        return "URL: %s\n（Firecrawl Markdown，截断 4000 字）：\n%s" % (url, md[:4000])
    except Exception as e:
        return "Firecrawl 调用失败：%s" % e


# ---------------- 记忆库 ----------------

def _load_mem():
    if not os.path.exists(MEM):
        return []
    try:
        with open(MEM, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_mem(items):
    os.makedirs(STATE, exist_ok=True)
    with open(MEM, "w", encoding="utf-8") as f:
        json.dump(items[-300:], f, ensure_ascii=False, indent=1)


def tool_memory_save(args):
    text = (args.get("text") or "").strip()
    if not text:
        return "缺少 text"
    items = _load_mem()
    items.append({"t": int(time.time()), "text": text,
                  "tags": args.get("tags") or ""})
    _save_mem(items)
    return "已记住（当前共 %d 条）" % len(items)


def tool_memory_search(args):
    q = (args.get("query") or "").strip()
    items = _load_mem()
    if not q:
        return "记忆库共 %d 条，最近 5 条：\n%s" % (
            len(items),
            "\n".join("- " + i["text"][:80] for i in items[-5:]))
    kws = [k for k in re.split(r"\s+", q) if k]
    hit = [i for i in items if any(k in i["text"] or k in i.get("tags", "") for k in kws)]
    if not hit:
        return "没找到相关记忆"
    return "找到 %d 条：\n%s" % (
        len(hit), "\n".join("- " + i["text"][:120] for i in hit[-10:]))


def qzone_friend_feed(count=20):
    """好友动态流（相当于手机端空间的红点列表）。"""
    cookie, bkn = _qz_ctx()
    url = ("https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com/cgi-bin/feeds/"
           "feeds3_html_more?uin=10004&scope=0&view=1&filter=all&flag=1"
           "&applist=all&update_num=0&update_time=0&g_tk=%s&count=%d&format=json"
           % (bkn, count))
    raw = _http(url, headers={
        "Cookie": cookie,
        "Referer": "https://user.qzone.qq.com/10004",
        "User-Agent": UA,
    })
    out = []
    for p in raw.split("{ver:'")[1:]:
        def g(k):
            m = re.search(k + r":'([^']*)'", p)
            return m.group(1) if m else ""
        tid = g("key")
        uin = g("uin")
        if not tid or not uin or uin == "0":
            continue
        if tid.startswith("advertisement"):
            continue
        out.append({"tid": tid, "uin": int(uin), "nickname": g("nickname"),
                    "abstime": int(g("abstime") or 0), "feedstime": g("feedstime")})
    return out


def tool_qzone_feed(args):
    """好友动态（不含自己）。"""
    try:
        feed = qzone_friend_feed(15)
    except Exception as e:
        return "读取好友动态失败：%s" % e
    mine_uin = 10004
    owner = {10001, 10002, 10003}
    lines = []
    for it in feed:
        if it["uin"] in owner or it["uin"] == mine_uin:
            continue
        c = qzone_post_content(it["uin"], it["tid"])
        if not c:
            continue
        lines.append("- %s（%s）：%s" % (it["nickname"], it["feedstime"], c[:80]))
        if len(lines) >= 8:
            break
    if not lines:
        return "好友最近没有新动态"
    return "好友空间最近的动态：\n" + "\n".join(lines)


def qzone_post_content(uin, tid, limit=8):
    """按 tid 取说说正文（好友动态里没有正文，得回查）。"""
    try:
        for m in qzone_feeds(int(uin), limit):
            if str(m.get("tid")) == str(tid):
                c = m.get("content") or ""
                c = re.sub(r"@\{uin:(\d+),nick:([^,}]*),who:\d+\}",
                           lambda x: "@" + (x.group(2) or x.group(1)), c)
                return c.replace("\n", " ").strip()[:300]
    except Exception:
        pass
    return ""


def qzone_post_images(uin, tid):
    """返回说说里的图片 URL 列表。图片在 pic 字段，视频封面在 video 字段，
    再从 content/conlist 的 <img> 兜底。"""
    try:
        for m in qzone_feeds(int(uin), 12):
            if str(m.get("tid")) != str(tid):
                continue
            urls = []
            for p in (m.get("pic") or []):
                if isinstance(p, dict):
                    for k in ("url1", "url2", "url3", "smallurl", "pic_id", "url"):
                        u = (p.get(k) or "").strip()
                        if u.startswith("http") and u not in urls:
                            urls.append(u)
            for v in (m.get("video") or []):
                if isinstance(v, dict):
                    for k in ("pic_url", "url1", "url", "video_pic", "img_url"):
                        u = (v.get(k) or "").strip()
                        if u.startswith("http") and u not in urls:
                            urls.append(u)
            blob = (m.get("content") or "")
            for c in (m.get("conlist") or []):
                blob += (c.get("con") or "")
            for u in re.findall(r'<img[^>]+src=["\']([^"\']+)', blob):
                u = u.strip()
                if u.startswith("http") and u not in urls:
                    urls.append(u)
            for u in re.findall(r'https?://[^\s"<>]+\.(?:jpg|jpeg|png|gif|webp)', blob):
                if u not in urls:
                    urls.append(u)
            return urls[:4]
    except Exception:
        pass
    return []


def qzone_download_image(url, uin):
    """下载 QQ 空间图片到本地临时文件，返回路径。失败返回 None。"""
    import urllib.request as _ur
    try:
        req = _ur.Request(url, headers={
            "User-Agent": UA,
            "Referer": "https://user.qzone.qq.com/%d" % uin,
        })
        with _ur.urlopen(req, timeout=20) as r:
            data = r.read()
        if len(data) < 64:
            return None
        ext = ".jpg"
        ct = (r.headers.get("Content-Type") or "").lower()
        if "png" in ct:
            ext = ".png"
        elif "gif" in ct:
            ext = ".gif"
        elif "webp" in ct:
            ext = ".webp"
        p = os.path.join(STATE, "tmp_qzimg_%d%s" % (int(time.time() * 1000), ext))
        with open(p, "wb") as f:
            f.write(data)
        return p
    except Exception:
        return None


def qzone_video_frames(uin, tid):
    """下载说说视频并按时间均匀抽 6 帧拼成一张网格图，返回本地路径；失败返回空。
    供「视频动态也能看内容」的识图用。"""
    import subprocess
    vurl = ""
    try:
        for m in qzone_feeds(int(uin), 12):
            if str(m.get("tid")) != str(tid):
                continue
            for v in (m.get("video") or []):
                if isinstance(v, dict):
                    for k in ("url3", "url1", "video_url", "url"):
                        u = (v.get(k) or "").strip()
                        if u.startswith("http") and ".mp4" in u:
                            vurl = u
                            break
                if vurl:
                    break
            if vurl:
                break
        if not vurl:
            return ""
        os.makedirs(os.path.join(STATE, "tmp"), exist_ok=True)
        base = os.path.join(STATE, "tmp", "vid_%d" % int(time.time() * 1000))
        vpath = base + ".mp4"
        req = Request(vurl, headers={
            "User-Agent": UA, "Referer": "https://user.qzone.qq.com/%d" % uin})
        with urlopen(req, timeout=40) as r:
            data = r.read()
        if len(data) < 1000:
            return ""
        with open(vpath, "wb") as f:
            f.write(data)
        dur = 0.0
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", vpath],
                capture_output=True, text=True, timeout=15)
            dur = float((out.stdout or "").strip() or 0)
        except Exception:
            dur = 0
        if dur <= 0:
            dur = 10.0
        N = 6
        step = dur / N
        frames = []
        for i in range(N):
            t = step * (i + 0.5)
            fp = "%s_%d.jpg" % (base, i)
            subprocess.run(["ffmpeg", "-y", "-ss", str(t), "-i", vpath,
                            "-frames:v", "1", "-q:v", "3", fp],
                           capture_output=True, timeout=30)
            if os.path.exists(fp) and os.path.getsize(fp) > 0:
                frames.append(fp)
        if not frames:
            try:
                os.remove(vpath)
            except Exception:
                pass
            return ""
        from PIL import Image
        cols = 3
        rows = (len(frames) + cols - 1) // cols
        imgs = [Image.open(f) for f in frames]
        w = max(im.width for im in imgs)
        h = max(im.height for im in imgs)
        grid = Image.new("RGB", (w * cols, h * rows), (0, 0, 0))
        for idx, im in enumerate(imgs):
            im = im.resize((w, h))
            grid.paste(im, (w * (idx % cols), h * (idx // cols)))
        out = base + "_grid.jpg"
        grid.save(out, "JPEG", quality=80)
        for f in frames + [vpath]:
            try:
                os.remove(f)
            except Exception:
                pass
        return out
    except Exception:
        return ""


# ---------------- 发文件 ----------------

OUTDIR = os.path.join(STATE, "out")


def _safe_name(name, default="file"):
    name = (name or "").strip() or default
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name)
    return name[:80]


def tool_send_file(args):
    """把网上的文件（论文/文档/压缩包）下载下来并发给对方。

    args: url（必填） | name（可选显示名）
    返回 @@FILE@@ 标记，由上层真正上传。
    """
    from urllib.parse import urlparse as _up
    url = (args.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return "需要一个 http(s) 链接"
    os.makedirs(OUTDIR, exist_ok=True)
    name = _safe_name(args.get("name") or os.path.basename(_up(url).path))
    if "." not in name:
        name += ".bin"
    dst = os.path.join(OUTDIR, "%d_%s" % (int(time.time()), name))
    try:
        req = Request(url, headers={"User-Agent": UA})
        with urlopen(req, timeout=60) as r:
            data = r.read()
            ctype = r.headers.get("Content-Type", "")
        if len(data) > 40 * 1024 * 1024:
            return "文件太大（超过 40MB），下不动"
        if len(data) < 64:
            return "下下来的东西是空的"
        with open(dst, "wb") as f:
            f.write(data)
    except Exception as e:
        return "下载失败：%s" % e
    size = len(data) / 1024.0 / 1024.0
    return "@@FILE@@%s|%s|%.2fMB|%s" % (dst, name, size, ctype)


# ---------------- 记忆（按人） ----------------

def tool_remember(args, qq=None, source_text=None):
    """记之前必须先确认：这句话真的出现在对方发来的聊天记录里。"""
    import memory_store as ms
    if not qq:
        return "不知道说话的人是谁，无法记"
    text = (args.get("text") or "").strip()
    if not text:
        return "缺少 text"
    if not source_text:
        return "没记：拿不到原始消息，不能确认真伪"
    ok, why = ms.verify_in_history(qq, source_text)
    if not ok:
        return "没记：%s，我不记不能确认的东西" % why
    ok2 = ms.add_mem(qq, text, tags=args.get("tags") or "", src="tool",
                     src_msg=source_text)
    return "已记住" if ok2 else "这条之前记过了"


def tool_recall(args, qq=None):
    import memory_store as ms
    if not qq:
        return "不知道说话的人是谁"
    hits = ms.search_mem(qq, args.get("query") or "")
    if not hits:
        return "关于这个人我没有相关记忆"
    return "关于这个人记得：\n" + "\n".join("- " + h["text"][:120] for h in hits)


def tool_profile(args, qq=None):
    import memory_store as ms
    if not qq:
        return "不知道说话的人是谁"
    p = ms.get_profile(qq)
    if not p:
        return "还没有这个人的档案"
    return json.dumps(p, ensure_ascii=False, indent=1)


# ---------------- 表情包 ----------------

STICKER_DIR = os.path.join(STATE, "stickers")


def sticker_files():
    if not os.path.isdir(STICKER_DIR):
        return []
    exts = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
    out = []
    for f in os.listdir(STICKER_DIR):
        p = os.path.join(STICKER_DIR, f)
        if os.path.isfile(p) and f.lower().endswith(exts) and os.path.getsize(p) > 0:
            out.append(p)
    return out


def sticker_locate(f, url=""):
    """定位收到表情的原图本地路径（只记录、不存进表情库），供「发文件」用。
    get_image 优先拿容器内原路径；拿不到再用 CDN url 下载到临时目录。"""
    path = ""
    if f:
        try:
            got = _napcat("get_image", {"file": f})
            d = got.get("data") or {}
            path = _resolve_path(d.get("file") or d.get("path"))
            if not url:
                url = d.get("url") or ""
        except Exception:
            pass
    if path and os.path.exists(path):
        return path
    if url:
        try:
            os.makedirs(os.path.join(STATE, "tmp"), exist_ok=True)
            req = Request(url, headers={"User-Agent": UA})
            with urlopen(req, timeout=20) as r:
                data = r.read()
            ext = ".gif" if "gif" in (r.headers.get("Content-Type") or "").lower() else ".img"
            dst = os.path.join(STATE, "tmp", "stk_%d%s" % (int(time.time() * 1000), ext))
            with open(dst, "wb") as t:
                t.write(data)
            return dst
        except Exception:
            return ""
    return ""


def tool_sticker_save(args, qq=None):
    """把表情原文件存进图库（AI 想存的时候才主动存，存进来之后能以 QQ 表情形式发出）。"""
    f = (args.get("file") or "").strip()
    url = (args.get("url") or "").strip()
    path = ""
    if f:
        got = _napcat("get_image", {"file": f})
        d = got.get("data") or {}
        path = _resolve_path(d.get("file") or d.get("path") or "")
        if not url:
            url = d.get("url") or ""
    if (not path or not os.path.exists(path)) and not url:
        return "拿不到这个表情的原文件"
    os.makedirs(STICKER_DIR, exist_ok=True)
    ts = int(time.time() * 1000)
    if path and os.path.exists(path):
        ext = os.path.splitext(path)[1] or ".gif"
        dst = os.path.join(STICKER_DIR, "%d%s" % (ts, ext))
        with open(path, "rb") as s, open(dst, "wb") as t:
            t.write(s.read())
    else:
        # 容器路径读不到时，用 CDN url 下载
        req = Request(url, headers={"User-Agent": UA})
        with urlopen(req, timeout=20) as r:
            data = r.read()
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "gif" in ctype:
            ext = ".gif"
        elif "jpeg" in ctype or "jpg" in ctype:
            ext = ".jpg"
        else:
            ext = ".png"
        dst = os.path.join(STICKER_DIR, "%d%s" % (ts, ext))
        with open(dst, "wb") as t:
            t.write(data)
    sticker_set_last(dst)
    return "已存进表情库（现在共 %d 张）" % len(sticker_files())


def sticker_pool():
    """表情来源：本地收的 + QQ 收藏表情（fetch_custom_face 返回的 CDN 链接）。"""
    pool = list(sticker_files())
    try:
        r = _napcat("fetch_custom_face", {})
        for u in (r.get("data") or []):
            if isinstance(u, str) and u.startswith("http"):
                pool.append(u)
    except Exception:
        pass
    return pool


LAST_STICKER = os.path.join(STATE, "last_sticker.txt")


def sticker_set_last(path):
    """记录最近偷到/收到并存下的那张表情。"""
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(LAST_STICKER, "w", encoding="utf-8") as f:
            f.write(path)
    except Exception:
        pass


def tool_sticker_reply_last(args, qq=None):
    """把最近偷到/收到并存下的那个表情原样发回给对方。"""
    try:
        with open(LAST_STICKER, encoding="utf-8") as f:
            p = f.read().strip()
    except Exception:
        p = ""
    if not p or not os.path.exists(p):
        return "还没有刚收到的表情可以发"
    return "@@STICKER@@%s" % p


STICKER_TAGS = os.path.join(STATE, "sticker_tags.json")


def _sticker_tags():
    try:
        with open(STICKER_TAGS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _tag_sticker(path):
    """用视觉模型给一张表情打标签。GIF 取首帧。失败返回空串。"""
    try:
        import llm_chat
        p = path
        if path.lower().endswith(".gif"):
            try:
                from PIL import Image
                im = Image.open(path)
                im.seek(0)
                tmp = os.path.join(os.path.dirname(STICKER_TAGS), "_frame_%d.jpg" % int(time.time()))
                im.convert("RGB").save(tmp, "JPEG", quality=85)
                p = tmp
            except Exception:
                pass
        return (llm_chat.vision_describe(p, "用几个中文词描述这张图/表情的内容和情绪，逗号分隔") or "").strip()
    except Exception:
        return ""


def _refresh_sticker_tags():
    """为库里未打标签的本地表情补标签，返回标签表。"""
    tags = _sticker_tags()
    changed = False
    for p in sticker_files():
        if p not in tags:
            t = _tag_sticker(p)
            if t:
                tags[p] = t
                changed = True
    if changed:
        try:
            os.makedirs(STATE, exist_ok=True)
            with open(STICKER_TAGS, "w", encoding="utf-8") as f:
                json.dump(tags, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
    return tags


def tool_sticker_send(args, qq=None):
    """挑一张表情发出去。结合 hint（模型期望的氛围/内容）识图选最搭的，不再纯随机。"""
    hint = (args.get("hint") or "").strip()
    pool = sticker_pool()
    if not pool:
        return "表情库还是空的"
    local = [p for p in pool if p.startswith("/")]
    if hint and local:
        tags = _refresh_sticker_tags()
        words = [w for w in re.split(r"[，,。\s、/\\（）()：:！!？?;；]+", hint.lower()) if len(w) >= 2]
        scored = []
        for p in local:
            t = (tags.get(p) or "").lower()
            if not t:
                continue
            twords = [w for w in re.split(r"[，,。\s、/\\（）()：:！!？?;；]+", t) if len(w) >= 2]
            s = sum(1 for w in words if w in t)
            s += sum(1 for tw in twords if tw in hint.lower())
            if s:
                scored.append((s, p))
        if scored:
            scored.sort(key=lambda x: -x[0])
            return "@@STICKER@@%s" % scored[0][1]
    import random
    return "@@STICKER@@%s" % random.choice(pool)


# ---------------- 天气 ----------------

WCODE = {
    0: "晴", 1: "少云", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨", 56: "冻雨", 57: "冻雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪",
    80: "阵雨", 81: "强阵雨", 82: "暴雨", 85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷雨伴冰雹",
}


def tool_weather(args):
    city = (args.get("city") or "北京").strip()
    try:
        g = json.loads(_http(
            "https://geocoding-api.open-meteo.com/v1/search?name=%s&count=1&language=zh&format=json"
            % quote(city)))
        res = (g.get("results") or [])
        if not res:
            return "找不到城市：%s" % city
        r = res[0]
        lat, lon, name = r["latitude"], r["longitude"], r.get("name") or city
        j = json.loads(_http(
            "https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
            "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
            "weather_code,wind_speed_10m&daily=temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max&timezone=Asia%%2FShanghai&forecast_days=2"
            % (lat, lon)))
    except Exception as e:
        return "查天气失败：%s" % e

    cur = j.get("current") or {}
    daily = j.get("daily") or {}
    code = cur.get("weather_code")
    lines = ["%s 现在：%s，%s°C（体感 %s°C），湿度 %s%%，风速 %s km/h" % (
        name, WCODE.get(code, "未知"), cur.get("temperature_2m"),
        cur.get("apparent_temperature"), cur.get("relative_humidity_2m"),
        cur.get("wind_speed_10m"))]
    tmax = (daily.get("temperature_2m_max") or [])
    tmin = (daily.get("temperature_2m_min") or [])
    pp = (daily.get("precipitation_probability_max") or [])
    if tmax and tmin:
        lines.append("今天 %s~%s°C，降水概率 %s%%" % (tmin[0], tmax[0], pp[0] if pp else "-"))
    if len(tmax) > 1 and len(tmin) > 1:
        lines.append("明天 %s~%s°C" % (tmin[1], tmax[1]))
    return "\n".join(lines)


# ---------------- 日报 ----------------

def tool_digest_now(_args=None):
    try:
        import run_digest as rd
        from datetime import datetime
        now = datetime.now(rd.core.TZ)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        bili, be = rd.collect_bili(start)
        dy, de = rd.collect_douyin(start)
        return rd.render_combined(start, bili, dy, be, de)
    except Exception as e:
        return "生成日报失败：%s" % e


# ---------------- 定时提醒 ----------------

REM = os.path.join(STATE, "reminders.json")


def tool_reminder(args):
    text = (args.get("text") or "").strip()
    minutes = args.get("minutes")
    at = (args.get("at") or "").strip()
    if not text:
        return "缺少 text"
    now = time.time()
    if at:
        try:
            hh, mm = at.split(":")
            t = time.localtime()
            ts = time.mktime((t.tm_year, t.tm_mon, t.tm_mday,
                              int(hh), int(mm), 0, 0, 0, -1))
            if ts < now:
                ts += 86400
        except Exception:
            return "at 格式应为 HH:MM"
    elif minutes:
        ts = now + int(minutes) * 60
    else:
        return "需要 minutes 或 at"
    items = []
    if os.path.exists(REM):
        try:
            items = json.load(open(REM, encoding="utf-8"))
        except Exception:
            items = []
    items.append({"ts": ts, "text": text})
    os.makedirs(STATE, exist_ok=True)
    json.dump(items, open(REM, "w", encoding="utf-8"), ensure_ascii=False)
    return "已设置提醒：%s（%s）" % (
        text, time.strftime("%m-%d %H:%M", time.localtime(ts)))


def pop_due_reminders(now=None):
    now = now or time.time()
    if not os.path.exists(REM):
        return []
    try:
        items = json.load(open(REM, encoding="utf-8"))
    except Exception:
        return []
    due = [i for i in items if i.get("ts", 0) <= now]
    rest = [i for i in items if i.get("ts", 0) > now]
    if due:
        json.dump(rest, open(REM, "w", encoding="utf-8"), ensure_ascii=False)
    return due


# ---------------- QQ 空间 ----------------

def _qz_ctx():
    d = _napcat("get_cookies", {"domain": "qzone.qq.com"})["data"]
    return d["cookies"], d["bkn"]


def _qz_json(text):
    """空间接口的返回五花八门：JSONP、HTML 包 JSON、纯 JSON，统一抠出来。"""
    s, e = text.find("{"), text.rfind("}")
    if s >= 0 and e > s:
        return json.loads(text[s:e + 1])
    raise ValueError("响应里没有 JSON: %s" % text[:120])


def qzone_feeds(uin=10004, num=5):
    """返回结构化说说列表（给监听器用）。"""
    cookie, bkn = _qz_ctx()
    url = ("https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/"
           "emotion_cgi_msglist_v6?uin=%d&ftype=0&sort=0&pos=0&num=%d"
           "&replynum=0&g_tk=%s&callback=_preloadCallback&code_version=1"
           "&format=jsonp&need_private_comment=1" % (uin, num, bkn))
    raw = _http(url, headers={
        "Cookie": cookie,
        "Referer": "https://user.qzone.qq.com/%d" % uin,
    })
    d = _qz_json(raw)
    if d.get("code") != 0:
        raise RuntimeError("code=%s msg=%s" % (d.get("code"), d.get("message")))
    return d.get("msglist") or []


def tool_qzone_list(args):
    uin = int(args.get("uin") or 10004)
    num = min(int(args.get("count") or 5), 10)
    try:
        ms = qzone_feeds(uin, num)
    except Exception as e:
        return "读取空间失败：%s" % e
    if not ms:
        return "该用户没有可见的说说（或权限不足）"
    out = []
    for x in ms:
        ct = x.get("createTime")
        if isinstance(ct, str) and "-" in ct:
            when = ct[:16]
        else:
            try:
                ts = int(ct or 0)
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "时间未知"
            except Exception:
                when = "时间未知"
        out.append("tid=%s | %s | 评论%s | %s | %s" % (
            x.get("tid"), x.get("name"), x.get("cmtnum") or 0, when,
            (x.get("content") or "").replace("\n", " ")[:80]))
    return "QQ空间说说（uin=%d）：\n%s" % (uin, "\n".join(out))


def _qzone_guard(need_comment=False):
    cfg = _cfg()
    if need_comment and not cfg.get("qzone_auto_comment"):
        return "评论功能未开启（llm.json 里 qzone_auto_comment 设为 true 才允许）"
    return None


def _today_count(kind):
    p = os.path.join(STATE, "qz_actions_%s.json" % time.strftime("%Y-%m-%d"))
    if not os.path.exists(p):
        return 0
    try:
        return int(json.load(open(p)) .get(kind, 0))
    except Exception:
        return 0


def _bump(kind):
    p = os.path.join(STATE, "qz_actions_%s.json" % time.strftime("%Y-%m-%d"))
    d = {}
    if os.path.exists(p):
        try:
            d = json.load(open(p))
        except Exception:
            d = {}
    d[kind] = d.get(kind, 0) + 1
    os.makedirs(STATE, exist_ok=True)
    json.dump(d, open(p, "w"))


def tool_qzone_like(args):
    uin = int(args.get("uin") or 10004)
    tid = (args.get("tid") or "").strip()
    if not tid:
        return "缺少 tid"
    cfg = _cfg()
    cap = int(cfg.get("qzone_like_daily_cap") or 20)
    if _today_count("like") >= cap:
        return "今天点赞已达上限 %d 次" % cap
    try:
        cookie, bkn = _qz_ctx()
        unikey = "http://user.qzone.qq.com/%d/mood/%s" % (uin, tid)
        form = urlencode({
            "opuin": uin, "unikey": unikey, "curkey": unikey,
            "appid": 311, "fid": "", "from": 1, "typeid": 0,
            "abstime": 0, "active": 0, "format": "json",
            "qzreferrer": "https://user.qzone.qq.com/%d" % uin,
        })
        raw = _http(
            "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/"
            "likes/internal_dolike_app?g_tk=%s" % bkn,
            data=form,
            headers={"Cookie": cookie,
                     "Content-Type": "application/x-www-form-urlencoded",
                     "Referer": "https://user.qzone.qq.com/%d" % uin,
                     "Origin": "https://user.qzone.qq.com"})
        d = _qz_json(raw)
    except Exception as e:
        return "点赞失败：%s" % e
    if d.get("ret") == 0 or d.get("code") == 0:
        _bump("like")
        return "已点赞 tid=%s" % tid
    return "点赞失败：%s" % json.dumps(d, ensure_ascii=False)[:200]


def tool_qzone_post(args):
    content = (args.get("content") or "").strip()
    if not content:
        return "缺少 content"
    cfg = _cfg()
    cap = int(cfg.get("qzone_post_daily_cap") or 3)
    if _today_count("post") >= cap:
        return "今天发说说已达上限 %d 条" % cap
    try:
        r = _napcat("send_qzone_msg", {"content": content})
    except Exception as e:
        return "发说说失败：%s" % e
    if r.get("retcode") == 0:
        _bump("post")
        return "已发说说 tid=%s" % ((r.get("data") or {}).get("tid"))
    return "发说说失败：%s" % (r.get("message") or r)


def tool_qzone_delete(args):
    tid = (args.get("tid") or "").strip()
    if not tid:
        return "缺少 tid"
    try:
        r = _napcat("delete_qzone_msg", {"tid": tid})
    except Exception as e:
        return "删除失败：%s" % e
    if r.get("retcode") == 0:
        return "已删除 tid=%s" % tid
    return "删除失败：%s" % (r.get("message") or r)


def tool_qzone_comment(args):
    if _qzone_guard(need_comment=True):
        return _qzone_guard(need_comment=True)
    uin = int(args.get("uin") or 10004)
    tid = (args.get("tid") or "").strip()
    text = (args.get("text") or "").strip()
    if not tid or not text:
        return "缺少 tid 或 text"
    cfg = _cfg()
    cap = int(cfg.get("qzone_comment_daily_cap") or 5)
    if _today_count("comment") >= cap:
        return "今天评论已达上限 %d 条" % cap
    try:
        cookie, bkn = _qz_ctx()
        form = urlencode({
            "topicId": "%d_%s__1" % (uin, tid),
            "feedsType": 0, "hostUin": uin, "uin": 10004,
            "content": text, "plat": "qzone", "format": "json",
            "qzreferrer": "https://user.qzone.qq.com/%d" % uin,
        })
        raw = _http(
            "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/"
            "emotion_cgi_re_feeds?g_tk=%s" % bkn,
            data=form,
            headers={"Cookie": cookie,
                     "Content-Type": "application/x-www-form-urlencoded",
                     "Referer": "https://user.qzone.qq.com/%d" % uin,
                     "Origin": "https://user.qzone.qq.com"})
        d = _qz_json(raw)
    except Exception as e:
        return "评论失败：%s" % e
    if d.get("ret") == 0 or d.get("code") == 0:
        _bump("comment")
        return "已评论：%s" % text
    return "评论失败：%s" % json.dumps(d, ensure_ascii=False)[:200]
