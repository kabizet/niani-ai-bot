# -*- coding: utf-8 -*-
"""多账号状态监控：任何一个掉线都通知号主，并尝试重启对应容器。"""
import json
import os
import subprocess
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state")
CFG = os.path.join(HERE, "llm.json")
NOTIFY_FILE = os.path.join(STATE, "account_alerts.json")

# 默认三个号（容器名、内部地址、显示名）
DEFAULT_ACCOUNTS = [
    {"name": "主号", "base": "http://172.17.0.2:3000", "container": "napcat", "qq": 10004},
    {"name": "小号一", "base": "http://172.17.0.3:3000", "container": "napcat-dot", "qq": 10003},
    {"name": "小号二", "base": "http://172.17.0.4:3000", "container": "napcat-2bx", "qq": 10002},
]

OWNER = 10001


def cfg():
    try:
        with open(CFG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def accounts():
    a = cfg().get("accounts")
    if isinstance(a, list) and a:
        return a
    return DEFAULT_ACCOUNTS


def owner():
    try:
        r = cfg().get("digest_recipients") or []
        return int(r[0]) if r else OWNER
    except Exception:
        return OWNER


def _post(base, path, payload=None, timeout=8):
    req = urllib.request.Request(base.rstrip("/") + "/" + path,
                                 data=json.dumps(payload or {}).encode(),
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def status(base, timeout=8):
    """返回 (online, uin, nick)。"""
    try:
        r = _post(base, "get_login_info", {}, timeout)
        d = r.get("data") or {}
        if r.get("retcode") == 0 and d.get("user_id"):
            return True, d.get("user_id"), d.get("nickname") or ""
    except Exception:
        pass
    return False, None, ""


def check_all():
    out = []
    for a in accounts():
        ok, uin, nick = status(a["base"])
        out.append({"name": a.get("name"), "qq": a.get("qq") or uin,
                    "container": a.get("container"), "base": a.get("base"),
                    "online": ok, "nick": nick})
    return out


def _load_alerts():
    try:
        with open(NOTIFY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_alerts(d):
    os.makedirs(STATE, exist_ok=True)
    with open(NOTIFY_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)


def _public_base():
    """优先用固定域名，没有再退回临时隧道。"""
    try:
        import json as _j
        here = os.path.dirname(os.path.abspath(__file__))
        c = _j.load(open(os.path.join(here, "llm.json"), encoding="utf-8"))
        b = (c.get("call_base") or c.get("public_base") or "").strip().rstrip("/")
        if b:
            return b
    except Exception:
        pass
    try:
        txt = open("/home/youruser/digest/logs/cf.log", encoding="utf-8", errors="ignore").read()
        import re as _re
        m = _re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", txt)
        return m[-1].rstrip("/") if m else ""
    except Exception:
        return ""


def fresh_qr(container, wait=28):
    """重启容器，等它生成新的二维码（旧的码 3 分钟就废了）。"""
    try:
        subprocess.run(["sudo", "docker", "restart", container],
                       capture_output=True, timeout=90)
        time.sleep(wait)
        return True
    except Exception:
        return False


def qr_link(container, name, fresh=False):
    """把容器里等扫码的二维码拷出来，返回可扫的链接。"""
    try:
        if fresh:
            fresh_qr(container)
        import re as _re
        safe = container.replace("napcat-", "").replace("napcat", "main")
        qr = os.path.join("/home/youruser/digest/state/qr", "%s.png" % safe)
        os.makedirs(os.path.dirname(qr), exist_ok=True)
        r = subprocess.run(["sudo", "docker", "cp",
                            "%s:/app/napcat/cache/qrcode.png" % container, qr],
                           capture_output=True, timeout=30)
        if r.returncode != 0:
            return ""
        subprocess.run(["sudo", "chown", "ubuntu:ubuntu", qr], timeout=10)
        base = _public_base()
        if not base:
          try:
            log = open("/home/youruser/digest/logs/cf.log", encoding="utf-8", errors="ignore").read()
            m = _re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", log)
            base = m[-1] if m else ""
          except Exception:
            pass
        if not base:
            return ""
        return "%s/img/%s.png?t=%d" % (base, safe, int(time.time()))
    except Exception:
        return ""


def notify(text):
    """用任意一个在线的号发给号主。"""
    o = owner()
    for a in accounts():
        ok, _, _ = status(a["base"], timeout=6)
        if not ok:
            continue
        try:
            _post(a["base"], "send_private_msg", {"user_id": o, "message": text}, timeout=15)
            return True, a.get("name")
        except Exception:
            continue
    return False, None


def restart_container(name):
    try:
        subprocess.run(["docker", "restart", name], timeout=180,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


# ---------------- 收消息活跃度探活 ----------------
# get_login_info 在「假死」（在线但收不到消息）时仍返回在线，所以补一个主动探活：
# 让另一个在线号给目标号发一条探针，看目标号能不能真正收到。
PROBE_FILE = os.path.join(STATE, "receive_probe.json")
PROBE_INTERVAL = 1800          # 每 30 分钟探一次
PROBE_MARK = "[__ALIVE_PROBE__]"
PROBE_TARGETS = (10004, 10003)   # 只有这两个号有机器人


def _load_probe_state():
    try:
        with open(PROBE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_probe_state(d):
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(PROBE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass


def receive_probe(target, sender):
    """主动探活：sender 给 target 发探针，看 target 能否真正收到。True=正常。"""
    ping = PROBE_MARK + str(int(time.time()))
    try:
        _post(sender["base"], "send_private_msg",
              {"user_id": target["qq"], "message": ping}, timeout=12)
    except Exception:
        return False
    time.sleep(8)
    try:
        r = _post(target["base"], "get_recent_contact", {"count": 30}, timeout=12)
    except Exception:
        return False
    for c in (r.get("data") or []):
        if str(c.get("peerUin")) != str(sender["qq"]):
            continue
        lm = c.get("lastestMsg") or {}
        if str((lm.get("sender") or {}).get("user_id")) == str(sender["qq"]) \
                and (lm.get("raw_message") or "").startswith(PROBE_MARK):
            return True
    return False


def watch(log=print, fails_threshold=6, restart=True):
    """跑一轮检查。

    规则：
      - 连不上 6 次（约 3 分钟）才算掉线
      - 掉线后只重启一次容器
      - 重启也救不回来 -> 通知一次，然后进入 30 分钟静默（等人扫码）
      - 恢复后通知一次，计数清零
    """
    alerts = _load_alerts()
    st = check_all()

    # 收消息活跃度探活（独立于登录掉线检测）
    if restart:
        probe_state = _load_probe_state()
        for item in st:
            qq = int(item.get("qq") or 0)
            if qq not in PROBE_TARGETS or not item.get("online"):
                continue
            pkey = str(qq)
            if time.time() - float(probe_state.get(pkey) or 0) < PROBE_INTERVAL:
                continue
            probe_state[pkey] = time.time()
            _save_probe_state(probe_state)
            sender = None
            for other in st:
                if int(other.get("qq") or 0) != qq and other.get("online"):
                    sender = other
                    break
            if not sender:
                continue
            if receive_probe(item, sender):
                log("账号 %s 收消息探活通过" % item["name"])
            else:
                log("账号 %s 收消息探活失败（在线但疑似收不到消息）" % item["name"])
                notify("⚠️ %s（%s）在线但疑似收不到消息，建议留意或手动重启"
                       % (item["name"], qq))
                # 2 小时内不再探/不再提醒，避免误判反复骚扰
                probe_state[pkey] = time.time() + 2 * 3600
                _save_probe_state(probe_state)

    for item in st:
        name = item["name"]
        rec = alerts.get(name) or {"down": 0, "notified": False,
                                   "restarted": False, "qr_at": 0}

        if item["online"]:
            if rec.get("notified") or rec.get("down"):
                log("账号 %s 已恢复" % name)
                try:
                    ok, by = notify("✅ %s（%s）已经重新上线了" %
                                    (name, item.get("qq") or item.get("nick")))
                    if ok:
                        log("已用 %s 发送恢复通知" % by)
                except Exception as e:
                    log("恢复通知失败: %s" % e)
            alerts[name] = {"down": 0, "notified": False,
                            "restarted": False, "qr_at": 0}
            continue

        # 已经在等人扫码的，30 分钟内不再刷
        if rec.get("notified") and time.time() - float(rec.get("qr_at") or 0) < 1800:
            alerts[name] = rec
            continue

        rec["down"] = int(rec.get("down") or 0) + 1
        log("账号 %s 掉线 %d/%d" % (name, rec["down"], fails_threshold))

        if rec["down"] < fails_threshold:
            alerts[name] = rec
            continue

        if not rec.get("restarted") and restart:
            log("尝试重启容器 %s" % item["container"])
            ok = restart_container(item["container"])
            rec["restarted"] = True
            log("容器 %s 重启%s" % (item["container"], "成功" if ok else "失败"))
            time.sleep(18)
            ok2, _u, _n = status(item["base"], timeout=10)
            if ok2:
                alerts[name] = {"down": 0, "notified": False,
                                "restarted": False, "qr_at": 0}
                log("重启后已恢复")
                okn, by = notify("⚠️ %s（%s）掉线过，已自动重启恢复" %
                                 (name, item.get("qq")))
                if okn:
                    log("已用 %s 发送重启通知" % by)
                continue

        # 救不回来：通知一次，然后静默 30 分钟
        if not rec.get("notified"):
            link = qr_link(item.get("container") or "", name)
            if link:
                msg = ("🚨 %s（%s）掉线了，需要扫码。\n\n%s\n\n"
                       "扫完自动恢复；30 分钟内我不再重复提醒。" %
                       (name, item.get("qq") or item.get("nick"), link))
            else:
                msg = ("🚨 %s（%s）掉线了，需要扫码（取码失败，跟我说「重发二维码」）。" %
                       (name, item.get("qq") or item.get("nick")))
            ok, by = notify(msg)
            rec["notified"] = True
            rec["qr_at"] = time.time()
            log("掉线通知%s（%s），进入 30 分钟静默" % ("已发送" if ok else "失败", by or "-"))
        alerts[name] = rec
    _save_alerts(alerts)
    return st

if __name__ == "__main__":
    for row in check_all():
        print(row)


def qr_link_fresh(container, name):
    """强制刷新：重启容器拿新码。"""
    return qr_link(container, name, fresh=True)
