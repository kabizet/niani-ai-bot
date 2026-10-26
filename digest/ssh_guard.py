# -*- coding: utf-8 -*-
"""SSH 登录监控：识别爆破/陌生 IP，自动拉黑，并通过 QQ 通知号主。

策略：
  - 10 分钟内同一 IP 失败 >= 5 次 → 拉黑（ufw deny）
  - 白名单 IP 永不动（含号主当前出口、内网、docker 网段）
  - 拉黑/解封/陌生 IP 成功登录 都会写日志并通知
"""
import json
import os
import re
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state")
LOG = os.path.join(HERE, "logs", "ssh_guard.log")
BLOCK_FILE = os.path.join(STATE, "ssh_blocked.json")
SEEN_FILE = os.path.join(STATE, "ssh_seen_ips.json")
AUTH = "/var/log/auth.log"
WINDOW = 600
THRESHOLD = 5

# 永远不拦
WHITELIST = {
    "127.0.0.1", "::1",
    "172.17.0.1", "172.17.0.2", "172.17.0.3", "172.17.0.4",
}


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _load(p, default):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(p, d):
    os.makedirs(STATE, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def notify(text):
    """用主号给号主发消息。"""
    try:
        import urllib.request
        body = json.dumps({"user_id": 10001, "message": text}).encode()
        req = urllib.request.Request("http://127.0.0.1:3000/send_private_msg",
                                     data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode()).get("retcode") == 0
    except Exception as e:
        log("通知失败: %s" % e)
        return False


def ufw(action, ip):
    cmd = ["ufw", action, "from", ip, "to", "any"]
    try:
        r = subprocess.run(["sudo"] + cmd, capture_output=True, text=True, timeout=30)
        return r.returncode == 0, (r.stdout + r.stderr).strip()[:120]
    except Exception as e:
        return False, str(e)


def _whitelist_file():
    try:
        import json as _j
        with open("/home/youruser/digest/state/ssh_whitelist.json", encoding="utf-8") as f:
            return set((_j.load(f) or {}).get("ips") or [])
    except Exception:
        return set()


def current_owners():
    """号主常用出口 IP（从已知登录记录里取，避免把自己锁外面）。"""
    ips = set(WHITELIST)
    try:
        out = subprocess.run(["last", "-i", "-w"], capture_output=True,
                             text=True, timeout=10).stdout
        for line in out.splitlines()[:30]:
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
            if m:
                ips.add(m.group(1))
    except Exception:
        pass
    return ips


def parse_new_lines(path, offset):
    """读 auth.log 新增行。"""
    try:
        size = os.path.getsize(path)
        if size < offset:
            offset = 0
        with open(path, encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            data = f.read()
            offset = f.tell()
        return data.splitlines(), offset
    except Exception:
        return [], offset


FAIL_RE = re.compile(r"Failed password for (?:invalid user )?(\S+) from (\d+\.\d+\.\d+\.\d+)")
OK_RE = re.compile(r"Accepted (?:password|publickey) for (\S+) from (\d+\.\d+\.\d+\.\d+)")


def main():
    log("SSH 监控启动")
    offsets = {}
    fails = {}   # ip -> [ts,...]
    blocked = _load(BLOCK_FILE, {})
    seen = _load(SEEN_FILE, {})
    owners = current_owners() | _whitelist_file()
    log("白名单: %s" % sorted(owners))

    while True:
        lines, off = parse_new_lines(AUTH, offsets.get(AUTH, 0))
        offsets[AUTH] = off
        now = time.time()
        for ln in lines:
            m = FAIL_RE.search(ln)
            if m:
                user, ip = m.group(1), m.group(2)
                if ip in owners:
                    continue
                fails.setdefault(ip, []).append(now)
                fails[ip] = [t for t in fails[ip] if now - t <= WINDOW]
                log("失败登录 %s@%s（近10分钟 %d 次）" % (user, ip, len(fails[ip])))
                if len(fails[ip]) >= THRESHOLD and ip not in blocked:
                    ok, msg = ufw("deny", ip)
                    blocked[ip] = {"at": now, "user": user, "count": len(fails[ip]),
                                   "ok": ok, "msg": msg}
                    _save(BLOCK_FILE, blocked)
                    log("拉黑 %s -> %s %s" % (ip, ok, msg))
                    notify("🚫 已自动拉黑 IP %s\n原因：10 分钟内 SSH 失败 %d 次（尝试账号 %s）\n"
                           "操作：ufw deny（%s）\n解除命令：sudo ufw delete deny from %s to any"
                           % (ip, len(fails[ip]), user, "成功" if ok else "失败 " + msg, ip))
                continue
            m = OK_RE.search(ln)
            if m:
                user, ip = m.group(1), m.group(2)
                if ip in owners:
                    continue
                if ip not in seen:
                    seen[ip] = now
                    _save(SEEN_FILE, seen)
                    log("陌生 IP 登录成功 %s@%s" % (user, ip))
                    notify("⚠️ 陌生 IP 登录成功\n账号：%s\nIP：%s\n时间：%s\n"
                           "如果是你自己，回我一声；不是你，我马上封掉。"
                           % (user, ip, time.strftime("%Y-%m-%d %H:%M:%S")))
        time.sleep(5)


if __name__ == "__main__":
    main()
