# -*- coding: utf-8 -*-
"""控制台：概览 / 插件 / 记忆库 三个页面。"""
import base64
import json
import os
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import llm_chat
import memory_store

STATE = os.path.join(HERE, "state")
LOGDIR = os.path.join(HERE, "logs")
CFG = os.path.join(HERE, "llm.json")
NAPCAT = "http://127.0.0.1:3000"
PORT = 8080
AUTH_FILE = os.path.join(STATE, "dashboard.json")

JOB_DESC = [
    ("news_brief.py", "资讯早报", "AI / 安全 / 逆向", "news"),
    ("run_digest.py", "观看日报", "B站 + 抖音观看总结", "digest"),
    ("like_card.py", "名片点赞", "每天 05:00 给三个号各点 10 个赞", "like_card"),
]


def ensure_auth():
    os.makedirs(STATE, exist_ok=True)
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    import secrets
    d = {"user": "admin", "pass": secrets.token_urlsafe(9)}
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.chmod(AUTH_FILE, 0o600)
    return d


AUTH = ensure_auth()


def napcat(path, payload=None, timeout=12):
    import urllib.request
    body = json.dumps(payload or {}).encode()
    req = urllib.request.Request(NAPCAT + "/" + path, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def napcat_port(port, path, payload=None, timeout=12):
    """按指定端口调 NapCat（用于各登录小号拉好友）。"""
    import urllib.request
    body = json.dumps(payload or {}).encode()
    req = urllib.request.Request("http://127.0.0.1:%d/%s" % (port, path),
                                 data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def tail(path, n=40):
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except Exception as e:
        return "读取失败: %s" % e


def cron_lines():
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True,
                             text=True, timeout=8).stdout
    except Exception:
        return []
    return [l for l in out.splitlines() if l.strip()]


def describe(line):
    body = line.lstrip("#").strip()
    parts = body.split(None, 5)
    if len(parts) < 6:
        return body[:40], "", "other"
    sched = " ".join(parts[:5])
    cmd = parts[5]
    for frag, title, sub, kind in JOB_DESC:
        if frag in cmd:
            if kind == "like_card":
                _cfg = load_cfg()
                lc = (_cfg.get("like_card") or {})
                n = len(lc.get("targets") or [])
                _nick = {}
                desc = "%s · %d 个号" % (sched, n)
                for _a in (_cfg.get("like_card_accounts") or []):
                    _k = int(_a.get("account") or 0)
                    desc += " · %s点%d个号" % (_nick.get(_k, str(_k)), len(_a.get("targets") or []))
                return title, desc, kind
            return title, "%s · %s" % (sched, sub), kind
    return cmd.split("/")[-1][:30], sched, "other"


def cron_state():
    out = []
    for i, line in enumerate(cron_lines()):
        enabled = not line.lstrip().startswith("#")
        title, detail, kind = describe(line)
        out.append({"i": i, "enabled": enabled, "title": title,
                    "detail": detail, "kind": kind})
    return out


def cron_toggle(i):
    lines = cron_lines()
    if not 0 <= i < len(lines):
        return False, "行不存在"
    line = lines[i]
    lines[i] = line.lstrip()[1:].lstrip() if line.lstrip().startswith("#") else "# " + line
    try:
        subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n",
                       text=True, timeout=10, check=True)
        return True, "ok"
    except Exception as e:
        return False, str(e)


def sched_time(kind):
    """从 crontab 解析早报/日报当前时间，返回 'HH:MM' 或 None。"""
    target = "news_brief.py" if kind == "news" else "run_digest.py"
    for line in cron_lines():
        body = line.lstrip("#").strip()
        if target in body and "--if-missing" not in body:
            parts = body.split(None, 5)
            if len(parts) >= 5:
                try:
                    return "%02d:%02d" % (int(parts[1]), int(parts[0]))
                except Exception:
                    pass
    return None


def set_schedule(kind, hhmm):
    """保存早报/日报时间到 llm.json，并同步更新 crontab 对应行。"""
    if kind not in ("news", "digest"):
        return False, "未知任务类型"
    m = str(hhmm or "").split(":")
    if len(m) != 2:
        return False, "时间格式应为 HH:MM"
    try:
        h, mi = int(m[0]), int(m[1])
        if not (0 <= h < 24 and 0 <= mi < 60):
            return False, "时间超出范围"
    except ValueError:
        return False, "时间格式应为 HH:MM"
    cfg = load_cfg()
    cfg["news_time" if kind == "news" else "digest_time"] = "%02d:%02d" % (h, mi)
    save_cfg(cfg)
    target = "news_brief.py" if kind == "news" else "run_digest.py"
    lines = cron_lines()
    hit = False
    for i, line in enumerate(lines):
        body = line.lstrip("#").strip()
        if target in body and "--if-missing" not in body:
            prefix = "# " if line.lstrip().startswith("#") else ""
            parts = body.split(None, 5)
            parts[0], parts[1] = str(mi), str(h)
            lines[i] = prefix + " ".join(parts)
            hit = True
            break
    if not hit:
        return False, "未找到定时行"
    try:
        subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n",
                       text=True, timeout=10, check=True)
        return True, "已设置为 %02d:%02d" % (h, mi)
    except Exception as e:
        return False, "crontab 更新失败: %s" % e


def load_cfg():
    try:
        with open(CFG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(d):
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def sync_style_to_code(style):
    """前端保存说话方式时，同步写入 llm_chat.py 的 DEFAULT_STYLE，
    避免 llm.json 被重置后默认退回旧值。返回是否写入成功。"""
    path = os.path.join(HERE, "llm_chat.py")
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
        lines = (style or "").split("\n")
        parts = []
        for ln in lines:
            esc = ln.replace("\\", "\\\\").replace('"', '\\"')
            parts.append('    "%s\\n"' % esc)
        block = "DEFAULT_STYLE = (\n" + "\n".join(parts) + "\n)"
        new_src, n = re.subn(r"DEFAULT_STYLE = \(\n(?:[^\n]*\n)*?\)",
                             lambda m: block, src, count=1)
        if n == 1 and new_src != src:
            # 先写临时文件并编译校验，通过才替换，避免写坏源码
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(new_src)
            import py_compile
            try:
                py_compile.compile(tmp, doraise=True)
            except Exception:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
                return False
            os.replace(tmp, path)
            return True
    except Exception:
        pass
    return False


_ck_cache = {"ts": 0.0, "bili": (False, ""), "dy": (False, "")}


def _cookie_ok_bili():
    """实测 B站登录态：调 nav 接口。返回 (是否有效, 用户名)。"""
    p = os.path.join(HERE, "bili_cookie.txt")
    if not os.path.exists(p):
        return False, ""
    ck = open(p, encoding="utf-8", errors="replace").read().strip()
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.bilibili.com/x/web-interface/nav",
            headers={"User-Agent": "Mozilla/5.0", "Cookie": ck})
        with urllib.request.urlopen(req, timeout=6) as r:
            d = json.loads(r.read().decode("utf-8"))
        data = d.get("data") or {}
        if d.get("code") == 0 and data.get("isLogin"):
            return True, data.get("uname") or ""
    except Exception:
        pass
    return False, ""


def _cookie_ok_douyin():
    """抖音按登录标识判断是否有效，并调 self 接口尝试取昵称。"""
    p = os.path.join(HERE, "douyin_cookie.txt")
    if not os.path.exists(p):
        return False, ""
    try:
        ck = open(p, encoding="utf-8", errors="replace").read().strip()
    except Exception:
        return False, ""
    if "sessionid=" not in ck and "sid_tt=" not in ck:
        return False, ""
    nick = ""
    try:
        import urllib.request, ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        url = ("https://www.douyin.com/aweme/v1/web/user/profile/self/"
               "?device_platform=webapp&aid=6383&channel=channel_pc_web")
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Cookie": ck, "Referer": "https://www.douyin.com/"})
        with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        nick = (d.get("user") or {}).get("nickname") or ""
    except Exception:
        pass
    return True, nick


def _cookie_status():
    """带缓存地返回各站 cookie 状态（5 分钟缓存，避免面板每 10s 都打外网）。"""
    now = time.time()
    if now - _ck_cache["ts"] > 300:
        b = _cookie_ok_bili()
        d = _cookie_ok_douyin()
        _ck_cache.update({"ts": now, "bili": b, "dy": d})
    return _ck_cache["bili"], _ck_cache["dy"]


def build_state():
    st = {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
          "cn_hour": int(time.strftime("%H"))}
    try:
        info = napcat("get_login_info").get("data") or {}
        st["qq"] = {"online": bool(info.get("user_id")),
                    "nick": info.get("nickname"), "uin": info.get("user_id")}
    except Exception as e:
        st["qq"] = {"online": False, "err": str(e)}

    cookies = {}
    b_ok, b_user = _cookie_status()[0]
    d_ok, _du = _cookie_status()[1]
    cookies["B站_ok"] = b_ok
    cookies["B站_user"] = b_user
    cookies["抖音_ok"] = d_ok
    cookies["抖音_user"] = _du
    for name, fn in (("B站", "bili_cookie.txt"), ("抖音", "douyin_cookie.txt")):
        p = os.path.join(HERE, fn)
        cookies[name] = round((time.time() - os.path.getmtime(p)) / 86400, 1) \
            if os.path.exists(p) else None
    st["cookies"] = cookies

    today = time.strftime("%Y-%m-%d")
    st["today"] = {"digest": os.path.exists(
        os.path.join(STATE, today + ".report.txt"))}
    try:
        st["report"] = open(os.path.join(STATE, today + ".report.txt"),
                            encoding="utf-8").read()
    except Exception:
        st["report"] = ""

    cfg = load_cfg()
    st["features"] = dict(llm_chat.DEFAULTS.get("features", {}))
    st["features"].update(cfg.get("features") or {})
    st["search_engine"] = cfg.get("search_engine") or "bing"
    st["digest_platforms"] = cfg.get("digest_platforms") or ["bili", "douyin"]
    st["news_time"] = cfg.get("news_time") or sched_time("news") or "06:00"
    st["digest_time"] = cfg.get("digest_time") or sched_time("digest") or "21:00"
    st["qzone_auto_like"] = cfg.get("qzone_auto_like", True)
    st["qzone_auto_comment"] = cfg.get("qzone_auto_comment", True)
    st["qzone_post_enabled"] = (cfg.get("features") or {}).get("qzone_post", True)
    st["llm"] = {
        "base_url": cfg.get("base_url") or "",
        "model": cfg.get("model") or "",
        "api_key_set": bool(cfg.get("api_key")),
    }
    st["digest_recipients"] = [str(x) for x in (cfg.get("digest_recipients") or [])]
    st["news_recipients"] = [str(x) for x in (cfg.get("news_recipients") or [])]
    st["news_keywords"] = cfg.get("news_keywords") or []
    st["care_targets"] = cfg.get("care_targets") or [
        {"qq": 10005, "hours": [9, 21], "enabled": True},
        {"qq": 10006, "hours": [9, 21], "enabled": True},
    ]

    profiles = memory_store.load_profiles()
    people = []
    for qq, p in profiles.items():
        people.append({"qq": qq, "name": p.get("name") or "",
                       "nick": p.get("nick") or "", "role": p.get("role") or ""})
    def _is_owner(p):
        return (p.get("role") or "").startswith("号主本人")
    owners = [p for p in people if _is_owner(p)]
    others = [p for p in people if not _is_owner(p)]
    owner_first = {"10001": 0, "10003": 1, "10002": 2}
    owners.sort(key=lambda p: owner_first.get(str(p["qq"]), 99))
    po = cfg.get("people_order") or []
    others.sort(key=lambda p: (po.index(str(p["qq"])) if str(p["qq"]) in po else 999,
                               p["qq"]))
    people = owners + others
    st["people"] = people
    st["memcount"] = {p["qq"]: len(memory_store.load_mem(p["qq"])) for p in people}
    st["cron"] = cron_state()
    st["like_card_targets"] = (cfg.get("like_card") or {}).get("targets") or []
    st["like_card_accounts"] = cfg.get("like_card_accounts") or []
    st["zodiac"] = cfg.get("zodiac") or []

    try:
        with open(os.path.join(STATE, "usage.json"), encoding="utf-8") as f:
            u = json.load(f)
        cur = u.get(today) or {}
        st["usage"] = {"p": cur.get("p", 0), "c": cur.get("c", 0),
                       "calls": cur.get("calls", 0),
                       "hist": [{"d": k, "t": v.get("p", 0) + v.get("c", 0)}
                                for k, v in sorted(u.items())[-7:]]}
    except Exception:
        st["usage"] = {"p": 0, "c": 0, "calls": 0, "hist": []}
    st["log"] = tail(os.path.join(LOGDIR, "bot.log"), 30)
    return st


def run_job(job):
    py = sys.executable or "/usr/bin/python3"
    cmd = {"digest": [py, os.path.join(HERE, "run_digest.py")],
           "news": [py, os.path.join(HERE, "news_brief.py")]}.get(job)
    if not cmd:
        return "未知任务"
    try:
        subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return "已触发"
    except Exception as e:
        return "失败: %s" % e


PAGE_FILE = os.path.join(HERE, "panel.html")

def page_html():
    try:
        with open(PAGE_FILE, encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return "<h1>panel.html 打不开: %s</h1>" % e


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def authed(self):
        h = self.headers.get("Authorization") or ""
        if not h.startswith("Basic "):
            return False
        try:
            u, _, p = base64.b64decode(h[6:]).decode().partition(":")
            return u == AUTH["user"] and p == AUTH["pass"]
        except Exception:
            return False

    def need_auth(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="panel"')
        self.end_headers()

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            return {}

    def static_file(self, path):
        """本地化静态资源（Font Awesome 等），仅限 /fa/ 目录，免鉴权。"""
        rel = path.lstrip("/")
        fp = os.path.normpath(os.path.join(HERE, rel))
        if not fp.startswith(HERE) or not os.path.isfile(fp):
            return self.send(404, '{"error":"not found"}')
        ctype = {".css": "text/css; charset=utf-8",
                 ".woff2": "font/woff2", ".woff": "font/woff",
                 ".ttf": "font/ttf", ".svg": "image/svg+xml",
                 ".eot": "application/vnd.ms-fontobject"}.get(
            os.path.splitext(fp)[1], "application/octet-stream")
        try:
            with open(fp, "rb") as f:
                return self.send(200, f.read(), ctype)
        except Exception:
            return self.send(404, '{"error":"not found"}')

    def qr2bx(self, path):
        """临时：实时展示登录二维码（免鉴权，复用 8080 公网端口）。"""
        base = "/tmp/qrserve"
        if path in ("/qr2bx", "/qr2bx/"):
            fp = os.path.join(base, "index.html")
            ctype = "text/html; charset=utf-8"
        elif path == "/qr2bx/qrcode.png":
            fp = os.path.join(base, "qrcode.png")
            ctype = "image/png"
        else:
            return self.send(404, '{"error":"not found"}')
        if not os.path.isfile(fp):
            return self.send(404, '{"error":"not found"}')
        try:
            with open(fp, "rb") as f:
                return self.send(200, f.read(), ctype)
        except Exception:
            return self.send(404, '{"error":"not found"}')

    def do_GET(self):
        u = urlparse(self.path)
        if u.path.startswith("/fa/"):
            return self.static_file(u.path)
        if u.path.startswith("/qr2bx"):
            return self.qr2bx(u.path)
        if not self.authed():
            return self.need_auth()
        if u.path == "/":
            return self.send(200, page_html(), "text/html; charset=utf-8")
        if u.path == "/api/state":
            return self.send(200, json.dumps(build_state(), ensure_ascii=False))
        if u.path == "/api/like_friends":
            account = (parse_qs(u.query).get("account") or ["main"])[0]
            cfg = load_cfg()
            port = 3000
            if account != "main":
                for _a in cfg.get("like_card_accounts") or []:
                    if str(_a.get("account")) == account:
                        port = int(_a.get("port") or 3003)
                        break
            try:
                r = napcat_port(port, "get_friend_list", {})
                li = napcat_port(port, "get_login_info", {})
                own = (li.get("data") or {}).get("user_id")
                data = r.get("data") or []
                friends = [{"qq": f.get("user_id"), "nick": f.get("nickname")}
                           for f in data if f.get("user_id") != own]
                return self.send(200, json.dumps({"ok": True, "friends": friends},
                                                 ensure_ascii=False))
            except Exception as e:
                return self.send(200, json.dumps(
                    {"ok": False, "err": str(e), "friends": []}, ensure_ascii=False))
        if u.path == "/api/memory":
            qq = (parse_qs(u.query).get("qq") or [""])[0]
            return self.send(200, json.dumps(
                {"items": memory_store.load_mem(qq)}, ensure_ascii=False))
        if u.path == "/api/persona":
            cfg = load_cfg()
            p = cfg.get("persona")
            if isinstance(p, dict):
                nick = (p.get("nicknames") or "").strip() or llm_chat.DEFAULT_NICKNAMES
                style = (p.get("style") or "").strip() or llm_chat.DEFAULT_STYLE
                abil = (p.get("abilities") or "").strip() or llm_chat.default_abilities_front(cfg)
            elif isinstance(p, str) and p.strip():
                nick = llm_chat.DEFAULT_NICKNAMES
                style = p
                abil = llm_chat.default_abilities_front(cfg)
            else:
                nick = llm_chat.DEFAULT_NICKNAMES
                style = llm_chat.DEFAULT_STYLE
                abil = llm_chat.default_abilities_front(cfg)
            return self.send(200, json.dumps(
                {"model": cfg.get("model") or "DeepSeek",
                 "nicknames": nick, "style": style, "abilities": abil},
                ensure_ascii=False))
        return self.send(404, '{"error":"not found"}')

    def do_POST(self):
        if not self.authed():
            return self.need_auth()
        u = urlparse(self.path)
        if u.path == "/api/run":
            job = (parse_qs(u.query).get("job") or [""])[0]
            return self.send(200, json.dumps({"msg": run_job(job)},
                                             ensure_ascii=False))
        d = self.body()
        if u.path == "/api/features":
            cfg = load_cfg()
            f = dict(llm_chat.DEFAULTS.get("features", {}))
            f.update(d.get("features") or {})
            cfg["features"] = f
            save_cfg(cfg)
            return self.send(200, json.dumps({"ok": True}, ensure_ascii=False))
        if u.path == "/api/options":
            cfg = load_cfg()
            if "search_engine" in d:
                cfg["search_engine"] = d["search_engine"]
            if isinstance(d.get("digest_platforms"), list):
                cfg["digest_platforms"] = d["digest_platforms"]
            save_cfg(cfg)
            return self.send(200, json.dumps({"ok": True}, ensure_ascii=False))
        if u.path == "/api/cron":
            ok, msg = cron_toggle(int(d.get("i", -1)))
            return self.send(200, json.dumps({"ok": ok, "msg": msg},
                                             ensure_ascii=False))
        if u.path == "/api/keywords":
            lst = [str(x).strip() for x in (d.get("list") or []) if str(x).strip()]
            cfg = load_cfg()
            if lst:
                cfg["news_keywords"] = lst
            else:
                cfg.pop("news_keywords", None)
            save_cfg(cfg)
            return self.send(200, json.dumps(
                {"ok": True, "msg": ("已保存 %d 个关键词" % len(lst)) if lst else "已恢复默认"},
                ensure_ascii=False))
        if u.path == "/api/recipients":
            lst = [int(x) for x in (d.get("list") or []) if str(x).isdigit()]
            kind = d.get("kind") or "digest"
            cfg = load_cfg()
            if kind == "news":
                cfg["news_recipients"] = lst
            else:
                cfg["digest_recipients"] = lst
            save_cfg(cfg)
            return self.send(200, json.dumps({"ok": True, "msg": "已保存"},
                                             ensure_ascii=False))
        if u.path == "/api/care":
            lst = []
            for t in (d.get("list") or []):
                try:
                    lst.append({"qq": int(t.get("qq")),
                                "hours": [int(h) for h in (t.get("hours") or [9, 21])],
                                "enabled": bool(t.get("enabled", True))})
                except Exception:
                    continue
            cfg = load_cfg()
            cfg["care_targets"] = lst
            save_cfg(cfg)
            return self.send(200, json.dumps({"ok": True, "msg": "已保存"},
                                             ensure_ascii=False))
        if u.path == "/api/zodiac":
            lst = []
            for z in (d.get("list") or []):
                try:
                    lst.append({"qq": int(z.get("qq")),
                                "birthday": str(z.get("birthday") or "").strip(),
                                "enabled": bool(z.get("enabled", True))})
                except Exception:
                    continue
            cfg = load_cfg()
            cfg["zodiac"] = lst
            save_cfg(cfg)
            return self.send(200, json.dumps({"ok": True, "msg": "已保存"},
                                             ensure_ascii=False))
        if u.path == "/api/schedule":
            ok, msg = set_schedule(d.get("kind"), str(d.get("time") or ""))
            return self.send(200, json.dumps({"ok": ok, "msg": msg},
                                             ensure_ascii=False))
        if u.path == "/api/qzone":
            cfg = load_cfg()
            if "like" in d:
                cfg["qzone_auto_like"] = bool(d["like"])
            if "comment" in d:
                cfg["qzone_auto_comment"] = bool(d["comment"])
            if "post" in d:
                f = dict(llm_chat.DEFAULTS.get("features", {}))
                f.update(cfg.get("features") or {})
                f["qzone_post"] = bool(d["post"])
                cfg["features"] = f
            save_cfg(cfg)
            return self.send(200, json.dumps({"ok": True, "msg": "已保存"},
                                             ensure_ascii=False))
        if u.path == "/api/llm":
            cfg = load_cfg()
            if "base_url" in d and str(d["base_url"]).strip():
                cfg["base_url"] = str(d["base_url"]).strip()
            if "model" in d and str(d["model"]).strip():
                cfg["model"] = str(d["model"]).strip()
            if "api_key" in d and str(d["api_key"]).strip():
                cfg["api_key"] = str(d["api_key"]).strip()
            save_cfg(cfg)
            return self.send(200, json.dumps({"ok": True, "msg": "已保存"},
                                             ensure_ascii=False))
        if u.path == "/api/people_order":
            cfg = load_cfg()
            cfg["people_order"] = [str(x) for x in (d.get("order") or [])]
            save_cfg(cfg)
            return self.send(200, json.dumps({"ok": True, "msg": "已保存"},
                                             ensure_ascii=False))
        if u.path == "/api/like_card":
            cfg = load_cfg()
            lc = dict(cfg.get("like_card") or {})
            if "targets" in d:
                lc["targets"] = [int(x) for x in (d["targets"] or [])]
            if "times" in d:
                try:
                    lc["times"] = max(1, min(10, int(d["times"])))
                except Exception:
                    pass
            cfg["like_card"] = lc
            if "accounts" in d:
                cfg["like_card_accounts"] = d["accounts"] or []
            save_cfg(cfg)
            return self.send(200, json.dumps({"ok": True, "msg": "已保存"},
                                             ensure_ascii=False))
        if u.path == "/api/persona":
            cfg = load_cfg()
            p = cfg.get("persona")
            if not isinstance(p, dict):
                p = {}
            for k in ("nicknames", "style", "abilities"):
                if k in d:
                    p[k] = str(d[k])
            cfg["persona"] = p
            save_cfg(cfg)
            if "style" in d:
                sync_style_to_code(str(d["style"]))
            return self.send(200, json.dumps({"ok": True, "msg": "已保存"},
                                             ensure_ascii=False))
        if u.path == "/api/memory/add":
            ok = memory_store.add_manual(str(d.get("qq")), d.get("text"))
            return self.send(200, json.dumps({"ok": ok, "msg": "" if ok else "已存在"},
                                             ensure_ascii=False))
        if u.path == "/api/memory/del":
            ok = memory_store.del_mem(str(d.get("qq")), int(d.get("index", -1)))
            return self.send(200, json.dumps({"ok": ok, "msg": "" if ok else "失败"},
                                             ensure_ascii=False))
        if u.path == "/api/memory/clear":
            memory_store.clear_mem(str(d.get("qq")))
            return self.send(200, json.dumps({"ok": True}, ensure_ascii=False))
        return self.send(404, '{"error":"not found"}')


if __name__ == "__main__":
    print("panel user=%s pass=%s port=%d" % (AUTH["user"], AUTH["pass"], PORT))
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
