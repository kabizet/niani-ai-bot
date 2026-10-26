# -*- coding: utf-8 -*-
"""按 QQ 分开的长期记忆库 + 人物档案 + 对话自动提炼。"""
import json
import os
import re
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state")
MEMDIR = os.path.join(STATE, "memory")
PROFILES = os.path.join(STATE, "profiles.json")
_LOCK = threading.Lock()

# 号主本人：多个号 + 多个称呼，别人提到这些名字 = 在说号主
OWNER_QQS = []
SELF_QQ = 0          # 机器人自己，不算「人」
OWNER_NAMES = []

BOOTSTRAP = {}


def _ensure():
    os.makedirs(MEMDIR, exist_ok=True)


def _load_json(p, default):
    if not os.path.exists(p):
        return default
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(p, data):
    _ensure()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


# ---------------- 人物档案 ----------------

def load_profiles():
    d = _load_json(PROFILES, {})
    if not d:
        d = dict(BOOTSTRAP)
        _save_json(PROFILES, d)
        return d
    # 补齐引导数据（不覆盖用户已有修改）
    changed = False
    for qq, info in BOOTSTRAP.items():
        if qq not in d:
            d[qq] = info
            changed = True
        else:
            for k, v in info.items():
                if k not in d[qq]:
                    d[qq][k] = v
                    changed = True
    if changed:
        _save_json(PROFILES, d)
    return d


def get_profile(qq):
    return load_profiles().get(str(qq)) or {}


def sync_profiles(log=None):
    """把所有好友同步成档案：称呼默认用 QQ 昵称，称呼默认用 QQ 昵称。"""
    try:
        import tools
        r = tools._napcat("get_friend_list", {})
        fs = r.get("data") or []
    except Exception as e:
        if log:
            log("同步好友失败: %s" % e)
        return 0
    d = load_profiles()
    changed = 0
    for f in fs:
        qq = str(f.get("user_id"))
        nick = (f.get("nickname") or "").strip()
        remark = (f.get("remark") or "").strip()
        if not qq:
            continue
        if int(qq) == SELF_QQ:
            continue
        cur = d.get(qq) or {}
        display = (remark or nick or qq)
        new = dict(cur)
        new["nick"] = nick
        if not new.get("name") or new.get("name") == new.get("nick"):
            new["name"] = display
        if not new.get("role"):
            new["role"] = "朋友"
        if new != cur:
            d[qq] = new
            changed += 1
    if changed:
        _save_json(PROFILES, d)
    if log and changed:
        log("同步档案：更新 %d 人" % changed)
    return changed


def set_profile(qq, **kw):
    d = load_profiles()
    cur = d.get(str(qq)) or {}
    cur.update(kw)
    d[str(qq)] = cur
    _save_json(PROFILES, d)
    return cur


def is_owner(qq):
    return int(qq) in OWNER_QQS


def owner_hint():
    return ""


# ---------------- 记忆库 ----------------

def _mem_path(qq):
    return os.path.join(MEMDIR, "%s.json" % qq)


def load_mem(qq):
    return _load_json(_mem_path(qq), [])


def add_mem(qq, text, tags="", src="chat", src_msg=None):
    text = (text or "").strip()
    if not text:
        return False
    with _LOCK:
        items = load_mem(qq)
        low = text.lower()
        for it in items:
            if it.get("text", "").strip().lower() == low:
                return False
        items.append({"t": int(time.time()), "text": text, "tags": tags,
                      "src": src, "src_msg": (src_msg or "")[:200]})
        _save_json(_mem_path(qq), items[-400:])
    return True


def add_manual(qq, text):
    """面板手工添加，直接生效，不标记待核。"""
    text = (text or "").strip()
    if not text:
        return False
    with _LOCK:
        items = load_mem(qq)
        for it in items:
            if it.get("text", "").strip() == text:
                return False
        items.append({"t": int(time.time()), "text": text, "tags": "",
                      "src": "manual", "src_msg": ""})
        _save_json(_mem_path(qq), items[-400:])
    return True


def del_mem(qq, index):
    items = load_mem(qq)
    if not 0 <= index < len(items):
        return False
    items.pop(index)
    _save_json(_mem_path(qq), items)
    return True


def clear_mem(qq):
    _save_json(_mem_path(qq), [])
    return True


def _history_texts(qq, count=80):
    import tools
    try:
        r = tools._napcat("get_friend_msg_history",
                          {"user_id": int(qq), "count": count})
        msgs = (r.get("data") or {}).get("messages") or []
    except Exception:
        return None
    return [_norm(m.get("raw_message")) for m in msgs
            if str((m.get("sender") or {}).get("user_id")) == str(qq)]


def audit(log=None):
    """自审：把无法在真实聊天记录里对上的记忆剔掉。

    老条目没有 src_msg 的，标记 unverified，不再用于对话上下文。
    """
    if not os.path.isdir(MEMDIR):
        return 0
    total_removed = 0
    for fn in os.listdir(MEMDIR):
        if not fn.endswith(".json"):
            continue
        qq = fn[:-5]
        items = load_mem(qq)
        if not items:
            continue
        hist = _history_texts(qq)
        if hist is None:
            if log:
                log("自审跳过 %s：拉不到聊天记录" % qq)
            continue
        keep = []
        dirty = False
        for it in items:
            # 面板手工添加的条目视为人工确认过，不参与核对
            if it.get("src") == "manual":
                keep.append(it)
                continue
            sm = _norm(it.get("src_msg"))
            if not sm:
                if not it.get("unverified"):
                    it["unverified"] = True
                    dirty = True
                keep.append(it)
                continue
            if any(sm[:30] in h or h[:30] in sm for h in hist if h):
                if it.get("unverified"):
                    it.pop("unverified", None)
                    dirty = True
                keep.append(it)
            else:
                total_removed += 1
                dirty = True
        if dirty or len(keep) != len(items):
            _save_json(_mem_path(qq), keep)
        if log:
            log("自审 %s：%d → %d 条" % (qq, len(items), len(keep)))
    return total_removed


def search_mem(qq, query="", limit=8):
    items = load_mem(qq)
    if not query:
        return items[-limit:]
    kws = [k for k in re.split(r"\s+|，|,|。", query) if k]
    hit = [i for i in items
           if any(k.lower() in i.get("text", "").lower()
                  or k.lower() in i.get("tags", "").lower() for k in kws)]
    return hit[-limit:]


def memory_context(qq):
    """给模型的上下文：这个人是谁 + 关于 ta 记得什么。"""
    p = get_profile(qq)
    parts = []
    if p:
        bits = []
        if p.get("name"):
            bits.append("称呼：%s" % p["name"])
        if p.get("aka"):
            bits.append("别名：" + " / ".join(p["aka"]))
        if p.get("role"):
            bits.append("身份：%s" % p["role"])
        if p.get("location"):
            bits.append("所在地：%s" % p["location"])
        if p.get("notes"):
            bits.append(p["notes"])
        parts.append("【正在和你说话的人】" + "；".join(bits))
    if is_owner(qq):
        parts.append(owner_hint())
    mem = [i for i in load_mem(qq) if not i.get("unverified")]
    if mem:
        recent = "\n".join("- " + i["text"][:120] for i in mem[-12:])
        parts.append("【关于这个人的记忆（最近 12 条）】\n" + recent)
    return "\n\n".join(parts)


# ---------------- 自动提炼 ----------------

DISTILL_PROMPT = (
    "下面这句话是【用户本人】说的：\n\n「%s」\n\n"
    "请只从**用户这句话本身**里提炼关于 ta 本人的、值得长期记住的事实，比如："
    "偏好、习惯、在做的项目、工作/学习情况、身体状况、重要计划、住址、雷区。\n\n"
    "严格约束：\n"
    "1. 助理之前说过的话不算依据，用户没亲口说的一律不许写。\n"
    "2. 用户只是在提问、感叹、开玩笑、或者复述别人的话，就输出空。\n"
    "3. 每条一行，以「- 」开头，不超过 45 字。\n"
    "4. 没有值得记的就只输出空字符串，不要解释、不要标题。"
)


def _norm(s):
    return re.sub(r"\s+", "", s or "")


def verify_in_history(qq, text):
    """这段话必须真的出现在对方发给我的聊天记录里，否则不许入库。

    这是防幻觉/防注入的硬门禁：任何没在真实会话里出现过的内容都记不住。
    """
    try:
        import tools
        r = tools._napcat("get_friend_msg_history",
                          {"user_id": int(qq), "count": 40})
        msgs = (r.get("data") or {}).get("messages") or []
    except Exception as e:
        return False, "读取聊天记录失败: %s" % e
    t = _norm(text)
    if not t:
        return False, "空消息"
    for m in msgs:
        if str((m.get("sender") or {}).get("user_id")) != str(qq):
            continue
        raw = _norm(m.get("raw_message"))
        if not raw:
            continue
        a, b = t[:40], raw[:40]
        if a and b and (a in raw or b in t):
            return True, "命中"
    return False, "聊天记录里没有这句话"


def distill(qq, user_text, assistant_text, log=None):
    """从一轮对话里提炼长期记忆。必须先通过真实性核对。"""
    import llm_chat
    if not llm_chat.available():
        return 0
    u = (user_text or "").strip()[:600]
    a = (assistant_text or "").strip()[:600]
    if len(u) < 6:
        return 0

    # 去掉我们拼上去的「（引用：…）」前缀再核对
    probe = re.sub(r"^（引用：[^）]*）\s*", "", u)
    probe = re.sub(r"^（对方引用了这条消息：.*?）\s*", "", probe, flags=re.S)
    ok, why = verify_in_history(qq, probe)
    if not ok:
        if log:
            log("记忆跳过 %s：%s" % (qq, why))
        return 0

    # 只拿「用户自己那句原话」去提炼，助理的回复一律不参与
    ok, out = llm_chat.complete(
        DISTILL_PROMPT % probe[:400],
        system="你是信息整理助手，只输出条目，不输出任何解释。",
        max_tokens=220)
    if not ok:
        return 0
    n = 0
    for line in (out or "").splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue
        text = line.lstrip("-").strip()
        if len(text) < 4 or len(text) > 120:
            continue
        if add_mem(qq, text, src="distill", src_msg=probe):
            n += 1
    if log and n:
        log("记忆提炼 %s 新增 %d 条" % (qq, n))
    return n
