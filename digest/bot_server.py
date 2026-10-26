# -*- coding: utf-8 -*-
"""QQ 消息应答服务。

NapCat 通过 httpClients 把事件 POST 到 /webhook。
私聊任意消息都会回；群消息仅在被 @ 时回。
回复内容默认走大模型（llm.json 配置），未配置时退回指令菜单。
"""
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import llm_chat
import memory_store
import tools

try:
    import asyncio
    import websockets
    HAVE_WS = True
except Exception:
    HAVE_WS = False

NAPCAT = os.environ.get("NAPCAT_URL", "http://127.0.0.1:3000")
SELF_ID = int(os.environ.get("SELF_QQ", "10004"))
PORT = int(os.environ.get("BOT_PORT", "3001"))
LOGDIR = os.path.join(HERE, "logs")

HELP = ("「日报」→ 今天的观看日报\n"
        "「状态」→ 运行状态\n"
        "其余的话我会正常跟你聊")

NO_LLM = "（还没配置大模型，暂时只能这样）\n" + HELP

# 纯应和词/纯笑：对方只回这类词，当作对话结束，不回复
_ACK_RE = re.compile(r"^(嗯+|哦+|好+|行+|好的|好吧|行吧|知道了|知道|好滴|好哦|嗯呢|嗯嗯|哦哦|好der|好嘞|收到|可以|成|行行|对对|okk|ok|OK|哈哈+|haha+|hahaha+|hh+|h{2,}|嘻嘻+|嘿嘿+|hehe+)$")

_last_reply = {}
_history = {}
_lock = threading.Lock()
RATE_SEC = 2


def log(msg):
    os.makedirs(LOGDIR, exist_ok=True)
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    with open(os.path.join(LOGDIR, "bot.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def post_json(url, payload, timeout=30):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body,
                  headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _brief(t):
    if isinstance(t, list):
        return "<图片/表情>"
    return str(t)[:40].replace("\n", " ")


def send_private(uid, text):
    try:
        r = post_json(NAPCAT + "/send_private_msg",
                      {"user_id": int(uid), "message": text})
        log("回复 -> %s: %s (retcode=%s)" % (uid, _brief(text), r.get("retcode")))
        return r
    except Exception as e:
        log("回复失败 -> %s: %s" % (uid, e))
        return None


def send_group(gid, text):
    try:
        r = post_json(NAPCAT + "/send_group_msg",
                      {"group_id": int(gid), "message": text})
        log("回复群 %s: %s (retcode=%s)" % (gid, _brief(text), r.get("retcode")))
        return r
    except Exception as e:
        log("群回复失败 -> %s: %s" % (gid, e))
        return None


def extract_text(ev):
    msg = ev.get("message")
    if isinstance(msg, str):
        return msg
    if isinstance(msg, list):
        parts = []
        for seg in msg:
            if seg.get("type") == "text":
                parts.append((seg.get("data") or {}).get("text") or "")
        return "".join(parts)
    return ""


def upload_file(mtype, target, path, name):
    """NapCat 只认 URL，所以走自己的 3001 端口把文件喂给容器。"""
    import urllib.parse as up
    if not os.path.isfile(path):
        return {"retcode": -1, "message": "本地文件不存在"}
    base = "http://172.17.0.1:3001/files/" + up.quote(os.path.basename(path))
    if mtype == "private":
        return post_json(NAPCAT + "/upload_private_file",
                         {"user_id": int(target), "file": base, "name": name})
    return post_json(NAPCAT + "/upload_group_file",
                     {"group_id": int(target), "file": base, "name": name})


def strip_md(text):
    """QQ 里不显示 markdown，把 ** 和 # 去掉。"""
    import re as _re
    t = str(text or "")
    t = _re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = _re.sub(r"(?m)^#{1,6}\s*", "", t)
    return t


_BOT_UINS = {10004, 10003, 10002}
_CHAIN = {}
_MUTE_UNTIL = {
    "until": 0
}


def free_groups():
    """哪些群免 @ 说话：只读自己的 llm.json 里 group_free。"""
    try:
        cfg = llm_chat.load_config() or {}
        return set(int(x) for x in (cfg.get("group_free") or []))
    except Exception:
        return set()


def group_free_allowed(gid, uid):
    """免艾特规则：白名单群直接允许；机器人之间限制连击，防止无限对话。"""
    try:
        if int(gid) not in free_groups():
            return False
        u = int(uid)
        if u in _BOT_UINS:
            # 机器人之间绝不对话，避免互刷
            return False
        _CHAIN[int(gid)] = 0
        return True
    except Exception:
        return False


def mentioned_me(ev):
    msg = ev.get("message")
    if isinstance(msg, list):
        for seg in msg:
            if seg.get("type") == "at":
                if str((seg.get("data") or {}).get("qq") or "") == str(SELF_ID):
                    return True
    return ("[CQ:at,qq=%s]" % SELF_ID) in (ev.get("raw_message") or "")


def get_msg_info(msg_id):
    """读出被引用的那条消息：正文、发送者、id。"""
    try:
        r = post_json(NAPCAT + "/get_msg", {"message_id": int(msg_id)})
        m = r.get("data") or {}
        t = extract_text({"message": m.get("message")}) or m.get("raw_message") or ""
        s = m.get("sender") or {}
        return {"text": t[:2500], "uid": s.get("user_id"),
                "name": s.get("card") or s.get("nickname") or "",
                "mid": m.get("message_id") or msg_id}
    except Exception as e:
        log("读取引用消息失败 %s: %s" % (msg_id, e))
        return {"text": "", "uid": None, "name": "", "mid": msg_id}


def quote_of(ev):
    segs = ev.get("message")
    if not isinstance(segs, list):
        return None
    for s in segs:
        if s.get("type") == "reply":
            mid = (s.get("data") or {}).get("id")
            if mid:
                return get_msg_info(mid)
    return None


def recent_transcript(mtype, target_id, limit=12, around=None):
    """拉最近聊天记录；给了 around（消息 id）就围绕那条取前后窗口。"""
    try:
        count = 40 if around else limit
        if mtype == "private":
            r = post_json(NAPCAT + "/get_friend_msg_history",
                          {"user_id": int(target_id), "count": count})
        else:
            r = post_json(NAPCAT + "/get_group_msg_history",
                          {"group_id": int(target_id), "count": count})
        msgs = (r.get("data") or {}).get("messages") or []
    except Exception as e:
        log("拉聊天记录失败: %s" % e)
        return ""
    msgs = sorted(msgs, key=lambda m: m.get("time") or 0)

    def render(m):
        s = m.get("sender") or {}
        if str(s.get("user_id")) == str(SELF_ID):
            who = "我(助理)"
        else:
            who = s.get("card") or s.get("nickname") or str(s.get("user_id"))
        body = (m.get("raw_message") or "").replace("\n", " ")[:160]
        return "%s: %s" % (who, body) if body else ""

    picks = msgs
    if around:
        idx = None
        for i, m in enumerate(msgs):
            if str(m.get("message_id")) == str(around):
                idx = i
                break
        if idx is not None:
            win = msgs[max(0, idx - 6): idx + 7]
            tail = [m for m in msgs[idx + 7:]][-4:]
            picks = win + tail
        else:
            picks = msgs[-limit:]
    else:
        picks = msgs[-limit:]

    lines = [render(m) for m in picks]
    return "\n".join([x for x in lines if x])


def harvest_stickers(ev):
    """对方发的图/动画表情：只记录原图路径供「发文件」用，不自动存进表情库。
    存库由 AI 主动决定（sticker_save），存进来的才能以 QQ 表情形式发出。"""
    segs = ev.get("message")
    if not isinstance(segs, list):
        return
    for s in segs:
        d = s.get("data") or {}
        f = (d.get("file") or "").strip()
        url = (d.get("url") or "").strip()
        typ = s.get("type")
        if typ in ("image", "face") and (f or url):
            try:
                p = tools.sticker_locate(f, url)
                if p:
                    tools.sticker_set_last(p)
                    log("收表情: 已记录原图 %s（未入库）" % os.path.basename(p))
                else:
                    log("收表情: 拿不到原图 file=%s" % f)
            except Exception as e:
                log("收表情失败: %s" % e)


def describe_images(ev):
    """把消息里的图片用视觉模型描述，返回追加文字。没配置识图 Key 或没图返回空。"""
    out = ""
    ml = ev.get("message")
    if not isinstance(ml, list):
        return out
    for s in ml:
        if s.get("type") != "image":
            continue
        f = ((s.get("data") or {}).get("file") or "").strip()
        if not f:
            continue
        try:
            got = tools._napcat("get_image", {"file": f})
            dd = got.get("data") or {}
            path = tools._resolve_path(dd.get("file") or dd.get("path"))
            if path and os.path.exists(path):
                dsc = llm_chat.vision_describe(path)
                if dsc:
                    out += "\n[对方发来一张图片，AI 看到的内容：%s]" % dsc
        except Exception as e:
            log("识图失败: %s" % e)
    return out


def _send_paras(sender, text):
    """把带换行的文本按自然段拆成多条消息发送。"""
    parts = [p.strip() for p in text.split("\n") if p.strip()]
    if len(parts) <= 1:
        sender(text)
        return
    for p in parts:
        sender(p)


def deliver(sender, out):
    """回复可能带表情包标记。换行拆成多条消息发。AI 决定不回复时什么都不发。"""
    if not out or not out.strip():
        return out or ""
    if "@@STICKER@@" in out:
        pre, _, path = out.partition("@@STICKER@@")
        path = path.strip()
        if pre.strip():
            _send_paras(sender, pre.strip())
        try:
            sender([{"type": "image", "data": {"file": path}}])
        except Exception as e:
            log("发表情失败: %s" % e)
        return pre.strip()
    _send_paras(sender, out)
    return out


def build_status():
    import urllib.request
    lines = ["■ 运行状态"]
    try:
        with urllib.request.urlopen(NAPCAT + "/get_login_info", timeout=10) as r:
            j = json.loads(r.read().decode())
        d = j.get("data") or {}
        lines.append("QQ：%s(%s) 在线" % (d.get("nickname"), d.get("user_id")))
    except Exception as e:
        lines.append("QQ：异常 %s" % e)

    for name, path in (("B站", "bili_cookie.txt"), ("抖音", "douyin_cookie.txt")):
        p = os.path.join(HERE, path)
        if os.path.exists(p):
            age = (time.time() - os.path.getmtime(p)) / 86400
            lines.append("%s cookie：%.0f 天前更新" % (name, age))
        else:
            lines.append("%s cookie：缺失" % name)

    sent = os.path.join(HERE, "state", time.strftime("%Y-%m-%d") + ".sent")
    lines.append("今日已发：%s" % ("是" if os.path.exists(sent) else "否"))
    lines.append("大模型：%s" % ("已配置" if llm_chat.available() else "未配置"))
    return "\n".join(lines)


def build_today_report():
    import run_digest as rd
    from datetime import datetime

    now = datetime.now(rd.core.TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    bili, bili_err = rd.collect_bili(start)
    dy_items, dy_err = rd.collect_douyin(start)
    return rd.render_combined(start, bili, dy_items, bili_err, dy_err)


def _remember(key, role, content):
    h = _history.setdefault(key, [])
    h.append((role, content))
    if len(h) > 8:
        del h[:-8]


def handle_event(ev):
    try:
        _t = extract_text(ev).strip()
        if ev.get("user_id") in (10001,) and re.search(r"(停|闭嘴|别说了|不要叫|安静)", _t):
            _MUTE_UNTIL["until"] = time.time() + 900
            log("号主喊停，群聊静默 15 分钟")
    except Exception:
        pass
    if ev.get("post_type") != "message":
        return
    mtype = ev.get("message_type")
    uid = ev.get("user_id")
    if not uid or int(uid) == SELF_ID:
        return

    if mtype == "private":
        if int(uid) in _BOT_UINS:
            return          # 别的 bot 私聊也不理，避免互刷
        key = ("p", int(uid))
        prefix = ""

        def reply(t):
            if isinstance(t, str):
                t = strip_md(t)
            send_private(uid, t)
    elif mtype == "group":
        gid = ev.get("group_id")
        if time.time() < _MUTE_UNTIL.get("until", 0) and not mentioned_me(ev):
            return
        if not mentioned_me(ev) and not group_free_allowed(gid, uid):
            return
        key = ("g", int(gid), int(uid))
        prefix = "[CQ:at,qq=%d] " % int(uid)

        def reply(t, _gid=gid, _p=prefix):
            if isinstance(t, list):
                send_group(_gid, t)
            else:
                send_group(_gid, _p + t)
    else:
        return

    harvest_stickers(ev)
    text = extract_text(ev).strip()
    # 引用表情包消息发「文件」→ 直接把原图以文件发回，不走 AI
    _q0 = quote_of(ev)
    if _q0 and re.search(r"文件", text) and len(text) <= 8:
        _tgt = ev.get("group_id") if mtype == "group" else uid
        _rp = ""
        try:
            with open(os.path.join(HERE, "state", "last_sticker.txt"), encoding="utf-8") as _f:
                _rp = _f.read().strip()
        except Exception:
            pass
        if _rp and os.path.exists(_rp):
            import shutil
            # 按文件真实头修正扩展名（NapCat 表情落盘常给 .jpg，但内容可能是 GIF/PNG）
            _fx = ""
            try:
                with open(_rp, "rb") as _fh:
                    _head = _fh.read(16)
                if _head[:6] in (b"GIF87a", b"GIF89a"):
                    _fx = ".gif"
                elif _head[:8] == b"\x89PNG\r\n\x1a\n":
                    _fx = ".png"
                elif _head[:3] == b"\xff\xd8\xff":
                    _fx = ".jpg"
                elif _head[:4] == b"RIFF" and _head[8:12] == b"WEBP":
                    _fx = ".webp"
            except Exception:
                pass
            _name = os.path.basename(_rp)
            if _fx and not _name.lower().endswith(_fx):
                _name = os.path.splitext(_name)[0] + _fx
            try:
                # 3001 文件服务只从 state/out 下发，先把原图复制过去再发
                _outd = os.path.join(HERE, "state", "out")
                os.makedirs(_outd, exist_ok=True)
                _copy = os.path.join(_outd, _name)
                if os.path.abspath(_rp) != os.path.abspath(_copy):
                    shutil.copyfile(_rp, _copy)
                else:
                    _copy = _rp
                upload_file(mtype, _tgt, _copy, _name)
                reply("原图发给你了（%s）" % _name)
            except Exception as _e:
                reply("发失败：%s" % (str(_e) or "")[:60])
        else:
            reply("表情库没存到那张的原图，再发一次试试")
        return
    if _ACK_RE.match(text):
        return
    orig_text = text
    q = quote_of(ev)
    if q and q.get("text"):
        target = ev.get("group_id") if mtype == "group" else uid
        tr = recent_transcript(mtype, target, limit=12, around=q.get("mid"))
        mine = str(q.get("uid")) == str(SELF_ID)
        text = ("（对方引用了%s这条消息：\n%s）\n"
                "（引用消息前后的聊天记录，用来理清前因后果）\n%s\n\n"
                "（现在对方说的是：%s）"
                % ("你自己说过的" if mine else "一条", q["text"], tr, text))
    # 识图：消息里的图片转成文字描述，追加给大模型
    img_desc = describe_images(ev)
    if not text and img_desc:
        # 对方只发了张图/表情、没任何文字也没提问 → 明确标注，防止 AI 脑补延伸
        text = ("对方主动发来一张图/表情（没有任何文字，也没提问）。图里内容是：%s。\n"
                "凭心情回应：值得说就一两句，没话说直接发个表情或不回复，"
                "不要脑补'对方问了什么'，不要延伸新话题，不要说'你问的其他事'。"
                % img_desc.strip())
    elif img_desc:
        text = (text + img_desc).strip()
    if not text:
        return

    now = time.time()
    sig = text[:80]
    with _lock:
        prev = _last_reply.get(key)
        # 只拦「同一人 2 秒内重复同一句话」，不再吞掉正常连发
        if prev and now - prev[0] < RATE_SEC and prev[1] == sig:
            log("重复消息，忽略 %s" % (key,))
            return
        _last_reply[key] = (now, sig)

    log("收到 %s: %s" % (key, text[:60].replace("\n", " ")))
    try:
        memory_store.set_profile(uid, last_chat=int(now))
    except Exception:
        pass
    t = text.lower()

    # 占卜流程优先接管
    tk = ("g:%s:%s" % (gid, uid)) if mtype == "group" else ("p:%s" % uid)
    try:
        tarot_reply = tarot_step(tk, orig_text)
    except Exception as e:
        log("占卜异常: %s" % e)
        tarot_reply = None
    if tarot_reply:
        reply(tarot_reply)
        _remember(key, "user", orig_text)
        _remember(key, "assistant", tarot_reply)
        return

    if re.search(r"^/?日报$|^/?报告$|^/?report$", t):
        reply("稍等，正在生成今天的日报…")
        try:
            reply(build_today_report())
        except Exception as e:
            reply("生成失败：%s" % e)
        return
    if re.search(r"^/?状态$|^/?status$", t):
        reply(build_status())
        return
    if re.search(r"^/?帮助$|^/?help$|^/?指令$", t):
        reply(HELP)
        return

    if not llm_chat.available():
        reply(NO_LLM)
        return

    _remember(key, "user", text)
    _now_s = time.strftime("%Y-%m-%d %H:%M")
    _w = "一二三四五六日"[time.localtime().tm_wday]
    ctx = ("【当前时间】%s（星期%s）\n\n" % (_now_s, _w)
           ) + llm_chat.build_context(build_status(), speaker=int(uid))
    ok, out, trace = llm_chat.chat_with_tools(
        text, ctx, _history.get(key, [])[:-1],
        verbose=lambda m: log(m), speaker=int(uid),
        source_text=orig_text)
    if trace:
        log("工具调用 %d 次" % len(trace))
    if ok:
        _remember(key, "assistant", out)
        sent = deliver(reply, out)
        # 工具里给了表情/文件，由这里真正发出去
        deliver_artifacts(trace, mtype, (gid if mtype == "group" else uid), reply)
        threading.Thread(target=memory_store.distill,
                         args=(int(uid), text, sent, log), daemon=True).start()
    else:
        log("LLM 失败: %s" % out)
        reply("（大模型暂时用不了：%s）" % out[:80])


# ---------------- 轮询兜底 ----------------
# NapCat 的事件推送在本环境不生效，改为每 3 秒主动读最近会话，发现新消息就处理。
# 不依赖 httpClients 也能工作。

POLL_SEC = 3
_seen = {}


def poller():
    log("poller 启动，每 %ds 轮询一次" % POLL_SEC)
    init = True
    while True:
        try:
            r = post_json(NAPCAT + "/get_recent_contact", {"count": 30})
            for c in (r.get("data") or []):
                ct = c.get("chatType")
                peer = c.get("peerUin")
                if ct not in (1, 2) or not peer:
                    continue
                key = ("p" if ct == 1 else "g", int(peer))
                mark = c.get("msgId") or c.get("msgTime")
                prev = _seen.get(key)
                _seen[key] = mark
                if init or prev is None or prev == mark:
                    continue
                lm = c.get("lastestMsg") or {}
                sender = (lm.get("sender") or {}).get("user_id")
                if not sender or int(sender) == SELF_ID:
                    continue
                raw_msg = lm.get("raw_message") or ""
                if raw_msg.startswith("[__ALIVE_PROBE__]"):
                    continue          # 看门狗的收消息探针，不回复
                segs = lm.get("message") or []
                if not segs and raw_msg:
                    segs = [{"type": "text", "data": {"text": raw_msg}}]
                ev = {
                    "post_type": "message",
                    "message_type": "private" if ct == 1 else "group",
                    "user_id": int(sender),
                    "self_id": SELF_ID,
                    "group_id": int(peer) if ct == 2 else None,
                    "raw_message": raw_msg,
                    "message": segs,
                }
                log("poller 发现新消息 %s" % (key,))
                handle_event(ev)
            init = False
        except Exception as e:
            log("poller 异常: %s" % e)
        time.sleep(POLL_SEC)


# ---------------- QQ 空间监听 ----------------

def _qz_clean(content):
    """把 @{uin:...,nick:...,who:1} 这种标记还原成人看的 @昵称。"""
    s = re.sub(r"@\{uin:(\d+),nick:([^,}]*),who:\d+\}",
               lambda m: "@" + (m.group(2) or m.group(1)), content or "")
    return s.replace("\n", " ").strip()


def _mentions_me(content):
    c = content or ""
    return ("@%s" % "助手") in c \
        or str(SELF_ID) in c or "傻子ai" in c.lower()


def qzone_watcher():
    import tools
    log("空间监听线程启动")
    time.sleep(15)
    seen_p = os.path.join(HERE, "state", "qz_seen.json")
    feed_p = os.path.join(HERE, "state", "qz_feed.json")
    bad = set()
    while True:
        cfg = llm_chat.load_config()
        if not (llm_chat.feature_on(cfg, "qzone_social")
                or llm_chat.feature_on(cfg, "qzone_post")):
            time.sleep(300)
            continue
        watch = cfg.get("qzone_watch") or []
        interval = max(2, int(cfg.get("qzone_watch_interval_min") or 3)) * 60
        like_on = cfg.get("qzone_auto_like", True) and llm_chat.feature_on(cfg, "qzone_social")
        comment_on = cfg.get("qzone_auto_comment", True) and llm_chat.feature_on(cfg, "qzone_social")

        seen = {}
        if os.path.exists(seen_p):
            try:
                with open(seen_p, encoding="utf-8") as f:
                    seen = json.load(f)
            except Exception:
                seen = {}
        dirty = False

        def handle(uin, tid, content, mine=False):
            nonlocal dirty
            # 已互动过的不再重复（防止重建基线时二次评论）
            acted_p = os.path.join(HERE, "state", "qz_acted.json")
            acted = set()
            if os.path.exists(acted_p):
                try:
                    with open(acted_p, encoding="utf-8") as f:
                        acted = set(json.load(f))
                except Exception:
                    acted = set()
            if str(tid) in acted:
                log("已互动过，跳过 %s" % tid)
                return
            if like_on:
                log("点赞 %s/%s: %s" % (uin, tid,
                                       tools.tool_qzone_like({"uin": int(uin), "tid": tid})))
            # 自己的说说：只有点名了我才评论
            want_comment = comment_on
            if mine and not _mentions_me(content):
                want_comment = False
            if want_comment and content:
                extra = ""
                desc = ""
                sres = ""
                try:
                    # 视频动态：抽帧拼图识别；图片动态：下载识别
                    vgrid = tools.qzone_video_frames(uin, tid)
                    if vgrid:
                        desc = llm_chat.vision_describe(vgrid) or ""
                        try:
                            os.remove(vgrid)
                        except Exception:
                            pass
                    else:
                        pics = tools.qzone_post_images(uin, tid)
                        if pics:
                            lp = tools.qzone_download_image(pics[0], uin)
                            if lp:
                                desc = llm_chat.vision_describe(lp) or ""
                                try:
                                    os.remove(lp)
                                except Exception:
                                    pass
                except Exception:
                    pass
                # 动态要求「搜来源/上网/查」时，用识图描述当搜索词去搜，结果给 LLM 参考
                if re.search(r"(搜|来源|上网|查一下|查查|搜一下)", content):
                    q = (desc or content).strip()[:60]
                    if q:
                        try:
                            _r = tools.tool_web_search({"query": q, "count": 3})
                            if _r and "没有找到" not in str(_r)[:30]:
                                sres = str(_r)[:400]
                        except Exception:
                            pass
                if desc:
                    extra += "\n（AI 看图：%s）" % desc[:200]
                if sres:
                    extra += "\n（联网搜到：%s）" % sres
                need_desc = bool(re.search(r"(描述|是什么|图里|图中|有什么|内容)", content))
                need_src = bool(re.search(r"(来源|上网|搜|查)", content))
                inst = "描述这张图。" if need_desc else ""
                if need_src:
                    inst += ("并给出搜索到的来源/出处。" if need_desc else "给出搜索到的来源/出处。")
                if not inst:
                    inst = "凭感觉回应一句。"
                ok, txt = llm_chat.complete(
                    "说说原文（作者：%s）：\n「%s%s」\n\n"
                    "对方要求你：%s\n"
                    "只用一句自然、口语、像真人随口说的话回应，不超过 25 字，"
                    "要结合上面 AI 看图描述和搜索结果，别跑题。\n"
                    "没什么可说的就只输出 SKIP。禁止「值得」「评论：」前缀，禁止多行，禁止解释。"
                    % ("我" if mine else "朋友", content, extra, inst),
                    system="你是个普通网友，说话自然不客套，不编造经历。",
                    max_tokens=60)
                t = (txt or "").strip().strip('"“”').splitlines()
                t = t[0].strip() if t else ""
                for pre in ("值得", "评论：", "评论:"):
                    if t.startswith(pre):
                        t = t[len(pre):].strip(" ：:")
                if ok and t and "SKIP" not in t.upper():
                    log("评论 %s: %s -> %s" % (tid, t,
                        tools.tool_qzone_comment({"uin": int(uin), "tid": tid, "text": t})))
            acted.add(str(tid))
            try:
                with open(acted_p, "w", encoding="utf-8") as f:
                    json.dump(sorted(acted)[-500:], f)
            except Exception:
                pass

        # 1) 号主自己的号：只看，不通知
        for uin in watch:
            try:
                feeds = tools.qzone_feeds(int(uin), 6)
                bad.discard(str(uin))
            except Exception as e:
                if str(uin) not in bad:
                    log("空间 %s 不可读（%s），跳过" % (uin, str(e)[:30]))
                    bad.add(str(uin))
                continue
            if not feeds:
                continue
            last = seen.get("own_%s" % uin)
            if last is None:
                seen["own_%s" % uin] = str(feeds[0].get("tid") or "")
                dirty = True
                continue
            for it in feeds:
                tid = str(it.get("tid") or "")
                if not tid or tid == last:
                    break
                c = _qz_clean(it.get("content") or "")[:300]
                handle(uin, tid, c, mine=True)
                seen["own_%s" % uin] = tid
                dirty = True

        # 2) 好友动态：点赞 + 看情况评论
        try:
            feed = tools.qzone_friend_feed(15)
        except Exception as e:
            log("好友动态读取失败: %s" % str(e)[:60])
            feed = []
        fresh = []
        for it in feed:
            if it["tid"] in seen.get("feed", []):
                continue
            fresh.append(it)
        if fresh:
            if "feed" not in seen:
                # 首次建基线：不是全部丢掉，最近 N 小时内的还是要互动，避免"建基线时正好有帖子"被吞
                hours = int(cfg.get("qzone_baseline_hours") or 48)
                cap = int(cfg.get("qzone_baseline_max") or 5)
                now_ts = time.time()
                recent = [i for i in fresh
                          if i.get("abstime") and (now_ts - i["abstime"]) < hours * 3600]
                recent = recent[:cap]
                for it in reversed(recent):
                    c = tools.qzone_post_content(it["uin"], it["tid"])
                    log("基线内补处理: %s %s %s" % (it["nickname"], it["tid"], (c or "")[:30]))
                    handle(it["uin"], it["tid"], c,
                           mine=it["uin"] in {10001, 10002, 10003})
                seen["feed"] = [i["tid"] for i in feed]
                dirty = True
                log("好友动态基线 %d 条，补处理 %d 条" % (len(seen["feed"]), len(recent)))
            else:
                for it in reversed(fresh):
                    c = tools.qzone_post_content(it["uin"], it["tid"])
                    log("好友动态: %s %s %s" % (it["nickname"], it["tid"], (c or "")[:30]))
                    handle(it["uin"], it["tid"], c,
                           mine=it["uin"] in {10001, 10002, 10003})
                    seen.setdefault("feed", []).append(it["tid"])
                    dirty = True
                seen["feed"] = seen["feed"][-200:]

        # 3) 缓存最近动态，供对话时参考（不发消息）
        try:
            cache = []
            skip_uins = {SELF_ID, 10001, 10002, 10003}
            seen_tid = set()
            for it in feed[:12]:
                if it["uin"] in skip_uins:
                    continue
                if it["tid"] in seen_tid:
                    continue
                seen_tid.add(it["tid"])
                cache.append({"uin": it["uin"], "name": it["nickname"],
                              "tid": it["tid"], "time": it["feedstime"],
                              "content": tools.qzone_post_content(it["uin"], it["tid"])[:80]})
            with open(feed_p, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
        except Exception:
            pass

        if dirty:
            try:
                with open(seen_p, "w", encoding="utf-8") as f:
                    json.dump(seen, f)
            except Exception as e:
                log("写入 qz_seen 失败: %s" % e)
        time.sleep(interval)


# ---------------- 占卜（塔罗） ----------------

TAROT_FILE = os.path.join(HERE, "state", "tarot.json")


def _now_ts():
    import time as _t
    return _t.time()


def _tarot_load():
    if os.path.exists(TAROT_FILE):
        try:
            with open(TAROT_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _tarot_save(d):
    with open(TAROT_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)


_CN_NUM = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六",
           7: "七", 8: "八", 9: "九", 10: "十", 11: "十一", 12: "十二", 13: "十三"}


def tarot_step(key, text):
    """返回 None 表示不接管；否则返回要回复的文字。"""
    import divination
    cfg = llm_chat.load_config()
    tarot_on = llm_chat.feature_on(cfg, "tarot")
    iching_on = llm_chat.feature_on(cfg, "iching")
    if not (tarot_on or iching_on):
        return None
    t = (text or "").strip()
    low = t.lower()

    # 明确的放弃/中断
    STOP = ("取消", "算了", "不占了", "退出", "别占", "不玩", "停", "等等", "不对",
            "不想占", "不想算", "没想占", "没什么想占", "退掉", "结束占卜", "退出占卜",
            "不抽了", "不问了", "算了不", "不占")
    if any(w in low for w in STOP):
        d = _tarot_load()
        if key in d:
            d.pop(key, None)
            _tarot_save(d)
            return "行，不算了。"
        return None

    d = _tarot_load()
    sess = d.get(key)

    # 会话超过 10 分钟自动作废
    if sess:
        import time as _t
        if _t.time() - float(sess.get("ts") or 0) > 600:
            d.pop(key, None)
            _tarot_save(d)
            sess = None

    if t in ("占卜", "塔罗", "抽牌", "/占卜"):
        if not tarot_on:
            return None
        d[key] = {"stage": "q", "type": "tarot", "ts": _now_ts()}
        _tarot_save(d)
        return "想占什么？先说一句你的问题（比如「最近的工作」「一段关系」）"

    if t in ("周易", "易经", "易经占", "占卦", "占一卦", "摇卦", "起卦", "算卦", "/周易"):
        if not iching_on:
            return None
        d[key] = {"stage": "q", "type": "iching", "ts": _now_ts()}
        _tarot_save(d)
        return "想卜什么？先说一句你问的事（比如「最近的工作」「要不要跳槽」）"

    if not sess:
        return None

    if sess.get("stage") == "q":
        if len(t) < 2:
            return "问题太短了，多说一句你想算什么"
        sess["q"] = t[:120]
        sess["ts"] = _now_ts()
        if sess.get("type") == "iching":
            sess["stage"] = "n"
            d[key] = sess
            _tarot_save(d)
            return "记下了。报三个数字（1-8、1-8、1-6），比如「3 5 2」，用来起卦"
        # 塔罗：按问题推荐牌阵
        spread = divination.match_spread(t)
        if spread["name"] != "三张牌":
            sess["spread"] = spread["name"]
            sess["stage"] = "confirm"
            d[key] = sess
            _tarot_save(d)
            _kw = spread["tags"][0] if spread.get("tags") else ""
            return "关于「%s」，是否使用『%s』牌阵？\n回复「用」，即按它抽取；其余默认三牌。如需更换问题，请回复「换」" % (
                _kw, spread["name"])
        sess["stage"] = "n"
        d[key] = sess
        _tarot_save(d)
        return "记下了。从 1-78 里挑三个数字发给我\n如需更换问题，请回复「换」"

    if sess.get("stage") == "confirm":
        _ct = (t or "").strip()
        if "换" in _ct:
            sess.pop("spread", None)
            sess.pop("q", None)
            sess["stage"] = "q"
            d[key] = sess
            _tarot_save(d)
            return "好，重说一遍你想算什么"
        if "用" in _ct:
            sp_name = sess.get("spread") or "三张牌"
            _n = 3
            for _s in divination.SPREADS:
                if _s["name"] == sp_name:
                    _n = _s["n"]
                    break
            sess["stage"] = "n"
            d[key] = sess
            _tarot_save(d)
            return "好，用『%s』牌阵。从 1-78 里挑%s个数字发给我\n如需更换问题，请回复「换」" % (
                sp_name, _CN_NUM.get(_n, str(_n)))
        sess.pop("spread", None)
        sess["stage"] = "n"
        d[key] = sess
        _tarot_save(d)
        return "好，默认三张牌。从 1-78 里挑三个数字发给我\n如需更换问题，请回复「换」"

    if sess.get("stage") == "n":
        if "换" in (t or "").strip():
            sess.pop("spread", None)
            sess.pop("q", None)
            sess["stage"] = "q"
            d[key] = sess
            _tarot_save(d)
            return "好，重说一遍你想算什么"
        nums = [int(x) for x in re.findall(r"\d{1,2}", t)]
        if sess.get("type") == "iching":
            nums = [n for n in nums if 1 <= n <= 8]
            if len(set(nums)) < 3:
                if len(t) > 12 or "?" in t or "？" in t:
                    d.pop(key, None)
                    _tarot_save(d)
                    return None
                return "要三个数字，范围 1-8，比如「3 5 2」"
            result = divination.iching_draw(nums[:3])
            question = sess.get("q") or ""
            d.pop(key, None)
            _tarot_save(d)
            if not result:
                return "起卦没起出来，换个数字再试"
            raw = divination.iching_text(result, question)
            ok, txt = llm_chat.complete(
                "%s\n\n请按卦给这个人解一次周易：\n"
                "1. 先说本卦是什么、卦意，直接连到 ta 问的这件事上，说具体，别背教科书套话\n"
                "2. 再讲动爻和第几爻、变卦说明事情在朝什么方向变（本卦是现在，变卦是走向）\n"
                "3. 最后给一句直觉、明确、能落地的小建议或判断，别吓人、别灌鸡汤、别玄虚\n"
                "语气像真人朋友，直接不客套，不要「作为AI」。控制在 220 字以内，分行清楚，别用任何加粗符号（**）。"
                % raw,
                system="你是个懂周易、会看人下菜的朋友：先共情 ta 问的事，再结合卦象给直觉判断，话说得具体、落到 ta 的生活上，给个能做的方向，不吓唬人、不灌鸡汤、不故弄玄虚。",
                max_tokens=420)
            if not ok:
                return raw + "\n（解读暂时出错：%s）" % txt[:40]
            return txt.replace("**", "").strip()
        # 塔罗：按牌阵抽牌
        spread_name = sess.get("spread") or "三张牌"
        spread = None
        for _s in divination.SPREADS:
            if _s["name"] == spread_name:
                spread = _s
                break
        if not spread:
            spread = divination.SPREADS[0]
        want = spread["n"]  # 牌阵几张就挑几个数字
        nums = [n for n in nums if 1 <= n <= 78]
        if len(set(nums)) < want:
            # 明显不是数字的一句话：当成放弃占卜，交回正常对话
            if len(t) > 12 or "?" in t or "？" in t:
                d.pop(key, None)
                _tarot_save(d)
                return None
            _eg = " ".join(str((i * 13 + 7) % 78 + 1) for i in range(want))
            return "要%d个不一样的数字，范围 1-78，比如「%s」" % (want, _eg)
        question = sess.get("q") or ""
        d.pop(key, None)
        _tarot_save(d)
        cards = divination.draw_spread(question, nums[:3], spread)
        raw = divination.spread_text(question, cards, spread)
        ok, txt = llm_chat.complete(
            "%s\n\n请按这个牌阵给这个人解一次塔罗：\n"
            "1. 按牌阵的每个位置逐个说这张牌怎么解读、直接连到 ta 问的事，别背教科书含义，别说「代表新的开始」这种套话\n"
            "2. 再讲这些牌之间怎么呼应、冲突、一起指向什么\n"
            "3. 最后给一句直觉、明确、能落地的小建议或判断，别吓人、别灌鸡汤、别玄虚\n"
            "语气像真人朋友，直接不客套，不要「作为AI」。控制在 280 字以内，分行清楚，别用任何加粗符号（**）。"
            % raw,
            system="你是个懂塔罗、会看人下菜的朋友：先共情 ta 问的事，再结合牌给直觉判断，话说得具体、落到 ta 的生活上，给个能做的方向，不吓唬人、不灌鸡汤、不故弄玄虚。",
            max_tokens=560)
        if not ok:
            return "牌抽好了（%s）：\n" % spread_name + "\n".join(
                "· %s：%s（%s）—— %s" % (spread["positions"][i] if i < len(spread["positions"]) else "第%d位" % (i + 1),
                                            c["name"], c["pos"], c["meaning"])
                for i, c in enumerate(cards)) + "\n（解读暂时出错：%s）" % txt[:40]
        return txt.replace("**", "").strip()

    return None

DEFAULT_CARE = [
    {"qq": 10005, "hours": [9, 21], "days": [1, 2, 3, 4, 5, 6, 7], "enabled": True},
    {"qq": 10006, "hours": [9, 21], "days": [1, 2, 3, 4, 5, 6, 7], "enabled": True},
]


def care_targets():
    cfg = llm_chat.load_config()
    ts = cfg.get("care_targets")
    if isinstance(ts, list):
        out = []
        for t in ts:
            try:
                days = [int(x) for x in (t.get("days") or [])]
                if not days:
                    days = [1, 2, 3, 4, 5, 6, 7]
                out.append({"qq": int(t.get("qq")),
                            "hours": [int(h) for h in (t.get("hours") or [9, 21])],
                            "days": days,
                            "enabled": bool(t.get("enabled", True))})
            except Exception:
                continue
        return out
    return DEFAULT_CARE


def care_loop():
    log("主动关心线程启动")
    p = os.path.join(HERE, "state", "care.json")
    while True:
        try:
            cfg0 = llm_chat.load_config()
            if not llm_chat.feature_on(cfg0, "care"):
                time.sleep(300)
                continue
            hour = time.localtime().tm_hour
            today = time.strftime("%Y-%m-%d")
            st = {}
            if os.path.exists(p):
                try:
                    with open(p, encoding="utf-8") as f:
                        st = json.load(f)
                except Exception:
                    st = {}
            for t in care_targets():
                if not t["enabled"]:
                    continue
                qq = t["qq"]
                wd = time.localtime().tm_wday + 1  # 1=周一 ... 7=周日
                days = t.get("days") or [1, 2, 3, 4, 5, 6, 7]
                if wd not in days:
                    continue
                for slot in t["hours"]:
                    if not (slot <= hour < slot + 3):
                        continue
                    k = "%s-%s-%d" % (qq, today, slot)
                    if st.get(k):
                        continue
                    prof = memory_store.get_profile(qq)
                    mem = memory_store.memory_context(qq)
                    _now_s = time.strftime("%Y-%m-%d %H:%M")
                    _w = "一二三四五六日"[time.localtime().tm_wday]
                    ok, text = llm_chat.complete(
                        "%s\n\n现在是 %s（星期%s）。给「%s」写一句%s的招呼，像朋友随口说话，不超过 20 字，"
                        "别问「在吗」。"
                        "禁止编造你做过的事（送东西、见面、路过某地、刚吃过什么）——你没有身体。"
                        "可以从上面记忆里挑一件真实的事提一句，或者说句实在的关心。"
                        % (mem, _now_s, _w, prof.get("name") or "朋友",
                           "早上" if slot < 12 else "晚上"),
                        system="你是一个话不多但靠谱的朋友。只输出一句中文，不要引号。",
                        max_tokens=60)
                    if ok and text.strip():
                        send_private(qq, text.strip().strip('"“”'))
                        st[k] = 1
                        log("主动关心 -> %s: %s" % (qq, text.strip()[:40]))
            with open(p, "w", encoding="utf-8") as f:
                json.dump(st, f)
        except Exception as e:
            log("关心线程异常: %s" % e)
        time.sleep(60)


# ---------------- 记忆自审 ----------------

def audit_loop():
    log("记忆自审线程启动")
    time.sleep(60)
    while True:
        try:
            n = memory_store.audit(log)
            if n:
                log("自审共剔除 %d 条对不上的记忆" % n)
        except Exception as e:
            log("自审异常: %s" % e)
        time.sleep(6 * 3600)


def friends_loop():
    """定时同步好友 -> 档案 + 检测新好友并通知主号。60 秒一次，新好友实时反映到面板。"""
    log("好友同步线程启动")
    time.sleep(10)
    snap = os.path.join(HERE, "state", "friends_snapshot.json")
    known = set()
    try:
        if os.path.exists(snap):
            with open(snap, encoding="utf-8") as f:
                known = set(json.load(f))
    except Exception:
        known = set()
    while True:
        try:
            friends = tools._napcat("get_friend_list")
            cur = {}
            for f in (friends.get("data") or []):
                u = f.get("user_id")
                if u and int(u) != SELF_ID:
                    cur[int(u)] = f.get("nickname") or ""
            cur_ids = set(cur.keys())
            if known:
                new = cur_ids - known
                for n in sorted(new):
                    try:
                        send_private(10001, "新好友：%s（%s）" % (cur.get(n, "?"), n))
                    except Exception as e:
                        log("新好友通知失败: %s" % e)
                    log("检测到新好友 %s %s" % (n, cur.get(n, "")))
            known = cur_ids
            try:
                with open(snap, "w", encoding="utf-8") as f:
                    json.dump(sorted(list(known)), f)
            except Exception:
                pass
            if llm_chat.feature_on(llm_chat.load_config(), "auto_profile"):
                memory_store.sync_profiles(log)
        except Exception as e:
            log("同步好友异常: %s" % e)
        time.sleep(60)


def banter_loop():
    """没人的时候主动在群里说句话，别太频繁。"""
    import random
    time.sleep(180)
    while True:
        try:
            cfg = llm_chat.load_config() or {}
            groups = cfg.get("group_free") or []
            quiet_min = int(cfg.get("banter_quiet_min") or 40)
            for gid in groups:
                try:
                    r = post_json(NAPCAT + "/get_group_msg_history",
                                  {"group_id": int(gid), "count": 3}, timeout=15)
                    msgs = (r.get("data") or {}).get("messages") or []
                    last_t = max([int(m.get("time") or 0) for m in msgs] or [0])
                    if time.time() - last_t < quiet_min * 60:
                        continue
                    if random.random() > 0.5:
                        continue
                    ok, out, _ = llm_chat.chat_with_tools(
                        "群里已经安静很久了。你闲着，主动说一句闲话，10-25 个字，"
                        "像在群里随口一说，可以聊服务器、天气、你刚看到的小事。"
                        "只输出你要发的那句话，不要解释、不要引号。",
                        "", [], speaker=10001)
                    line = (out or "").strip().strip('"').split("\n")[0][:80]
                    if line:
                        send_group(int(gid), line)
                        log("主动闲聊 -> %s: %s" % (gid, line))
                except Exception as e:
                    log("闲聊失败 %s: %s" % (gid, e))
        except Exception as e:
            log("闲聊线程异常: %s" % e)
        time.sleep(random.randint(1800, 3600))


def watchdog_loop():
    """QQ 掉线/卡死时自动重启 NapCat。"""
    log("看门狗启动")
    import subprocess
    fails = 0
    time.sleep(30)
    while True:
        ok = False
        try:
            r = post_json(NAPCAT + "/get_login_info", {}, timeout=8)
            ok = (r.get("retcode") == 0 and (r.get("data") or {}).get("user_id"))
        except Exception:
            ok = False
        if ok:
            if fails:
                log("QQ 已恢复（此前连续失败 %d 次）" % fails)
            fails = 0
        else:
            fails += 1
            log("QQ 心跳失败 %d/3" % fails)
            if fails >= 3:
                log("判定掉线，自动重启 NapCat")
                try:
                    subprocess.run(["docker", "restart", "napcat"], timeout=180)
                    log("NapCat 已重启")
                except Exception as e:
                    log("重启 NapCat 失败: %s" % e)
                fails = 0
                time.sleep(40)
        time.sleep(60)


def deliver_artifacts(trace, mtype, target, reply):
    """把工具产出（表情/文件）真正发出去。"""
    for tr in trace or []:
        o = str(tr.get("out") or "")
        if o.startswith("@@STICKER@@"):
            p = o.split("@@STICKER@@", 1)[1].strip()
            try:
                reply([{"type": "image", "data": {"file": p}}])
                log("已发表情 %s" % p)
            except Exception as e:
                log("表情发送失败: %s" % e)
        elif o.startswith("@@FILE@@"):
            body = o.split("@@FILE@@", 1)[1]
            parts = body.split("|")
            path = parts[0]
            name = parts[1] if len(parts) > 1 else os.path.basename(path)
            try:
                r = upload_file(mtype, target, path, name)
                log("发文件 %s -> %s" % (name, (r or {}).get("retcode")))
                if (r or {}).get("retcode") != 0:
                    reply("文件传不上去：%s" % ((r or {}).get("message") or "")[:60])
            except Exception as e:
                log("发文件失败: %s" % e)
                reply("文件发送失败：%s" % e)


def _save_pending_friend(uid, comment):
    p = os.path.join(HERE, "state", "pending_friends.json")
    try:
        items = []
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                items = json.load(f)
        items.append({"qq": int(uid), "comment": comment or "",
                      "time": time.strftime("%Y-%m-%d %H:%M")})
        with open(p, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
    except Exception as e:
        log("存好友申请失败: %s" % e)


def _handle_ws_event(ev):
    try:
        if ev.get("post_type") != "request":
            return
        rtype = ev.get("request_type")
        uid = ev.get("user_id")
        if not uid:
            return
        if rtype in ("friend", "friendship"):
            comment = ev.get("comment") or ""
            try:
                send_private(10001, "收到好友申请：%s%s" % (
                    uid, ("（备注：%s）" % comment) if comment else ""))
            except Exception as e:
                log("申请通知失败: %s" % e)
            _save_pending_friend(uid, comment)
            log("收到好友申请 %s %s" % (uid, comment))
    except Exception as e:
        log("ws事件异常: %s" % e)


def ws_server_loop():
    if not HAVE_WS:
        log("无 websockets，好友申请事件不可用")
        return
    async def handler(ws, *args):   # websockets>=14 只传 ws
        try:
            async for msg in ws:
                try:
                    _handle_ws_event(json.loads(msg))
                except Exception as e:
                    log("ws解析异常: %s" % e)
        except Exception:
            pass
    async def _serve():
        try:
            async with websockets.serve(handler, "0.0.0.0", 3002):
                await asyncio.Future()
        except Exception as e:
            log("ws serve异常: %s" % e)
    try:
        asyncio.run(_serve())
    except Exception as e:
        log("ws server异常: %s" % e)


class Handler(BaseHTTPRequestHandler):

    def log_message(self, *a):
        pass

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            text = raw.decode("utf-8", "replace")
            log("RAW 请求 %s %s | %s" % (self.path, len(raw), text[:260].replace("\n", " ")))
            # 完整记录所有上报事件（诊断好友申请用）
            try:
                with open(os.path.join(HERE, "logs", "webhook_events.log"),
                          "a", encoding="utf-8") as _f:
                    _f.write("%s %s | %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                               self.path, text[:3000]))
            except Exception:
                pass
            ev = json.loads(text)
        except Exception as e:
            log("事件解析失败: %s" % e)
            self.send_response(204)
            self.end_headers()
            return

        self.send_response(204)
        self.end_headers()
        try:
            if ev.get("post_type") == "request":
                _handle_ws_event(ev)
            else:
                handle_event(ev)
        except Exception as e:
            log("处理异常: %s" % e)

    def do_GET(self):
        # 给 NapCat 容器下载用：http://172.17.0.1:3001/files/<name>
        if self.path.startswith("/files/"):
            import urllib.parse as up
            name = up.unquote(self.path[len("/files/"):].split("?")[0])
            name = os.path.basename(name)
            p = os.path.join(HERE, "state", "out", name)
            if os.path.isfile(p):
                try:
                    with open(p, "rb") as f:
                        data = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                except Exception as e:
                    log("发送文件失败: %s" % e)
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"qqbot ok")


if __name__ == "__main__":
    log("bot server 启动 port=%d self=%s llm=%s"
        % (PORT, SELF_ID, llm_chat.available()))
    threading.Thread(target=poller, daemon=True).start()
    # 空间监听已按用户要求停用（不需要盯特定好友），保留代码但不再启动
    threading.Thread(target=care_loop, daemon=True).start()
    threading.Thread(target=audit_loop, daemon=True).start()
    threading.Thread(target=friends_loop, daemon=True).start()
    threading.Thread(target=banter_loop, daemon=True).start()
    threading.Thread(target=watchdog_loop, daemon=True).start()
    threading.Thread(target=qzone_watcher, daemon=True).start()
    threading.Thread(target=ws_server_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
