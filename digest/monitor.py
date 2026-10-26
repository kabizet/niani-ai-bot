# -*- coding: utf-8 -*-
"""服务器监控：阈值告警 + 证书到期 + 每日 08:00 汇报。"""
import json
import os
import shutil
import socket
import ssl
import subprocess
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state")
LOG = os.path.join(HERE, "logs", "monitor.log")
ALERT_FILE = os.path.join(STATE, "monitor_alerts.json")
CERT_FILE = os.path.join(STATE, "certs.json")
OWNER = 10001

SERVICES = ["qqbot", "qqpanel", "qqcall", "qqacct", "qqssh"]
THRESHOLD = {"disk": 85, "mem": 90, "load_per_core": 2.0}
COOLDOWN = 6 * 3600
CERT_WARN_DAYS = 15


def log(msg, level="INFO"):
    line = "%s | %-5s | monitor | %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), level, msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify(text):
    """发告警：主号优先，掉线就依次换小号兜底。"""
    body = json.dumps({"user_id": OWNER, "message": text}).encode()
    for base in ("http://127.0.0.1:3000",   # 主号
                 "http://127.0.0.1:3003",   # 小号1
                 "http://127.0.0.1:3004"):  # 小号2
        try:
            req = urllib.request.Request(base + "/send_private_msg",
                                         data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                if json.loads(r.read().decode()).get("retcode") == 0:
                    return True
        except Exception as e:
            log("通知失败(%s): %s" % (base, e), "WARN")
    return False


def _load(p, d):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return d


def _save(p, d):
    os.makedirs(STATE, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def metrics():
    m = {}
    total, used, free = shutil.disk_usage("/")
    m["disk_pct"] = round(used / total * 100, 1)
    m["disk_free_gb"] = round(free / 1024 ** 3, 1)
    with open("/proc/meminfo") as f:
        info = dict(line.split(":", 1) for line in f)
    mt = int(info["MemTotal"].split()[0])
    ma = int(info["MemAvailable"].split()[0])
    m["mem_pct"] = round((mt - ma) / mt * 100, 1)
    m["mem_total_gb"] = round(mt / 1024 / 1024, 2)
    m["mem_avail_gb"] = round(ma / 1024 / 1024, 2)
    try:
        load1 = os.getloadavg()[0]
    except Exception:
        load1 = 0
    cores = os.cpu_count() or 1
    m["load1"] = round(load1, 2)
    m["cores"] = cores
    m["svc"] = {}
    for s in SERVICES:
        try:
            r = subprocess.run(["systemctl", "is-active", s], capture_output=True,
                               text=True, timeout=5)
            m["svc"][s] = r.stdout.strip()
        except Exception:
            m["svc"][s] = "unknown"
    return m


def cert_days(host, port=443):
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=8) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                cert = ss.getpeercert()
        exp = time.mktime(time.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z"))
        return round((exp - time.time()) / 86400, 1)
    except Exception as e:
        return None


def daily_report(m):
    lines = ["【每日状态 %s】" % time.strftime("%m-%d %H:%M")]
    lines.append("磁盘：%s%% 占用，剩 %sG" % (m["disk_pct"], m["disk_free_gb"]))
    lines.append("内存：%s%% 占用，可用 %sG / %sG" % (m["mem_pct"], m["mem_avail_gb"], m["mem_total_gb"]))
    lines.append("负载：%s（%d 核）" % (m["load1"], m["cores"]))
    down = [k for k, v in m["svc"].items() if v != "active"]
    lines.append("服务：%s" % ("全部正常" if not down else "异常 " + "、".join(down)))
    try:
        import ssh_guard as g
        b = g._load(g.BLOCK_FILE, {})
        lines.append("SSH：昨日累计拉黑 %d 个 IP" % len(b))
    except Exception:
        pass
    try:
        certs = _load(CERT_FILE, {})
        if certs:
            for host in certs:
                d = cert_days(host)
                if d is not None:
                    lines.append("证书 %s：剩 %s 天" % (host, d))
    except Exception:
        pass
    return "\n".join(lines)


def main():
    log("监控启动")
    alerts = _load(ALERT_FILE, {})
    last_daily = ""
    while True:
        try:
            m = metrics()
            now = time.time()
            # 阈值告警
            checks = [
                ("disk", m["disk_pct"] >= THRESHOLD["disk"],
                 "磁盘占用 %s%%（剩 %sG），超过 %d%%" % (m["disk_pct"], m["disk_free_gb"], THRESHOLD["disk"])),
                ("mem", m["mem_pct"] >= THRESHOLD["mem"],
                 "内存占用 %s%%，可用 %sG" % (m["mem_pct"], m["mem_avail_gb"])),
                ("load", m["load1"] >= m["cores"] * THRESHOLD["load_per_core"],
                 "负载 %s，%d 核" % (m["load1"], m["cores"])),
            ]
            for key, bad, text in checks:
                rec = alerts.get(key) or {}
                if bad and now - rec.get("at", 0) > COOLDOWN:
                    if notify("⚠️ 服务器告警：%s\n如果持续这样，可能需要处理一下。" % text):
                        alerts[key] = {"at": now}
                        log("告警已发: %s" % key, "WARN")
                elif not bad and rec:
                    alerts.pop(key, None)
            # 服务挂了
            for name, state in m["svc"].items():
                key = "svc:" + name
                rec = alerts.get(key) or {}
                if state != "active" and now - rec.get("at", 0) > COOLDOWN:
                    if notify("🚨 服务 %s 状态是 %s，不在运行。" % (name, state)):
                        alerts[key] = {"at": now}
                        log("服务告警: %s=%s" % (name, state), "WARN")
                elif state == "active":
                    alerts.pop(key, None)
            # 证书到期
            certs = _load(CERT_FILE, {})
            for host in certs:
                d = cert_days(host)
                if d is not None and d <= CERT_WARN_DAYS:
                    key = "cert:" + host
                    rec = alerts.get(key) or {}
                    if now - rec.get("at", 0) > 24 * 3600:
                        if notify("🔐 %s 的证书还有 %s 天到期，记得续。" % (host, d)):
                            alerts[key] = {"at": now}
            _save(ALERT_FILE, alerts)

            # 每日 08:00 汇报
            hm = time.strftime("%H:%M")
            day = time.strftime("%Y-%m-%d")
            if hm == "08:00" and last_daily != day:
                last_daily = day
                notify(daily_report(m))
                log("已发送每日汇报")
        except Exception as e:
            log("异常: %s" % e, "ERROR")
        time.sleep(300)


if __name__ == "__main__":
    main()
