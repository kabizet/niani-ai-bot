# -*- coding: utf-8 -*-
"""FunctionsGraph 定时函数：B站当日观看日报 -> 邮件。

零第三方依赖，仅 Python 标准库。华为云 FunctionGraph / 任何支持 Python3.8+ 的
运行时都能直接跑。

环境变量:
  BILI_COOKIE   B站 cookie 串，形如 "SESSDATA=xxx; bili_jct=yyy; DedeUserID=zzz"
  MAIL_USER     SMTP 登录账号（你的 QQ 邮箱）
  MAIL_AUTH     SMTP 授权码（不是 QQ 密码）
  MAIL_TO       收件邮箱，默认 10001@qq.com
  SMTP_HOST     默认 smtp.qq.com
  SMTP_PORT     默认 465 (SSL)
  DRY_RUN       设为 1 时只打印不发送
"""
import json
import os
import smtplib
import ssl
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from urllib.parse import urlencode
from urllib.request import Request, urlopen

TZ = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HISTORY = "https://api.bilibili.com/x/web-interface/history/cursor"
NAV = "https://api.bilibili.com/x/web-interface/nav"
WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

RULES = [
    ("编程/开发", ["编程", "代码", "python", "java", "前端", "后端", "github", "算法",
                   "程序员", "linux", "docker", "数据库", "架构", "开源", "ai", "大模型",
                   "gpt", "深度学习", "机器学习", "爬虫", "运维", "逆向", "汇编", "钩子",
                   "hook", "抓包", "破解", "渗透", "外挂", "插件"]),
    ("知识/科普", ["科普", "为什么", "原理", "解密", "揭秘", "冷知识", "历史", "物理",
                   "化学", "生物", "数学", "天文", "地理", "哲学", "心理学", "经济",
                   "法律", "医学"]),
    ("科技/数码", ["手机", "电脑", "显卡", "cpu", "评测", "开箱", "数码", "芯片", "苹果",
                   "华为", "小米", "相机", "耳机", "键盘", "显示器", "装机", "nas"]),
    ("财经/商业", ["股票", "基金", "投资", "理财", "财经", "经济", "商业", "创业", "公司",
                   "市值", "比特币", "加密货币", "房价", "楼市", "财报"]),
    ("游戏", ["游戏", "攻略", "实况", "解说", "王者", "原神", "steam", "lol", "英雄联盟",
              "mc", "我的世界", "塞尔达", "主机", "手游", "电竞"]),
    ("影视/解说", ["电影", "影视", "剧集", "解说", "纪录片", "预告", "影评", "剧情", "番剧",
                   "动漫", "动画", "国漫", "混剪"]),
    ("音乐", ["音乐", "翻唱", "歌曲", "mv", "钢琴", "吉他", "live", "演唱会", "纯音乐"]),
    ("美食", ["美食", "做饭", "菜谱", "吃货", "探店", "料理", "烧烤", "火锅", "甜品", "厨房"]),
    ("健身/运动", ["健身", "减脂", "增肌", "跑步", "运动", "训练", "篮球", "足球", "游泳", "瑜伽"]),
    ("学习/考试", ["考研", "高考", "英语", "四六级", "学习", "课程", "考试", "论文", "雅思"]),
    ("生活/日常", ["vlog", "日常", "生活", "记录", "租房", "搬家", "整理", "收纳", "省钱"]),
    ("汽车", ["汽车", "电动车", "新能源", "试驾", "车评", "特斯拉", "比亚迪", "摩托"]),
    ("旅游", ["旅行", "旅游", "自驾", "风景", "攻略", "露营", "徒步"]),
    ("搞笑/娱乐", ["搞笑", "沙雕", "整活", "段子", "综艺", "吐槽", "名场面"]),
    ("宠物", ["猫", "狗", "宠物", "喵", "汪", "仓鼠", "养宠"]),
    ("时尚/美妆", ["穿搭", "美妆", "化妆", "护肤", "发型", "时尚", "种草"]),
    ("军事/时政", ["军事", "武器", "战争", "时政", "国际", "地缘", "国防", "坦克", "战机"]),
    ("二次元", ["鬼畜", "mad", "amv", "手书", "cos", "虚拟主播", "vtuber", "宅舞"]),
]
DEEP_BOOST = {"编程/开发": 3, "知识/科普": 3, "财经/商业": 3, "科技/数码": 2,
              "学习/考试": 2, "军事/时政": 1, "汽车": 1, "健身/运动": 1}


def http_json(url, cookie, timeout=20):
    req = Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com/",
        "Cookie": cookie,
    })
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def check_login(cookie):
    try:
        j = http_json(NAV, cookie)
    except Exception:
        return False, None, None
    if j.get("code") == 0 and (j.get("data") or {}).get("isLogin"):
        d = j["data"]
        return True, d.get("uname"), d.get("mid")
    return False, None, None


def fetch_history(cookie, since_ts, max_pages=40):
    items, cursor, seen = [], int(time.time()), set()
    for _ in range(max_pages):
        url = HISTORY + "?" + urlencode({"ps": 30, "type": "archive", "cursor": cursor})
        j = http_json(url, cookie)
        if j.get("code") == -101:
            raise RuntimeError("LOGIN_EXPIRED")
        if j.get("code") != 0:
            raise RuntimeError("history error: %s" % j)
        data = j.get("data") or {}
        lst = data.get("list") or []
        if not lst:
            break
        oldest = None
        for it in lst:
            va = int(it.get("view_at") or 0)
            kid = (it.get("history") or {}).get("oid") or it.get("kid")
            if kid in seen:
                continue
            seen.add(kid)
            oldest = va if oldest is None else min(oldest, va)
            if va >= since_ts:
                h = it.get("history") or {}
                items.append({
                    "title": (it.get("title") or "").strip(),
                    "author": (it.get("author_name") or "").strip(),
                    "view_at": va,
                    "duration": int(it.get("duration") or 0),
                    "progress": int(it.get("progress") or 0),
                    "is_finish": bool(it.get("is_finish")),
                    "tag_name": (it.get("tag_name") or "").strip(),
                })
        nxt = (data.get("cursor") or {}).get("view_at") or 0
        if oldest is not None and oldest < since_ts:
            break
        if not nxt or nxt >= cursor:
            break
        cursor = int(nxt)
        time.sleep(0.3)
    return items


def classify(it):
    blob = ("%s %s %s" % (it["title"], it["tag_name"], it["author"])).lower()
    for name, kws in RULES:
        for kw in kws:
            if kw in blob:
                return name
    return "其他"


def analyze(items, day):
    total = len(items)
    minutes = sum((it["progress"] or it["duration"]) for it in items) / 60.0
    full = sum(1 for it in items if it["is_finish"])
    by_cat = defaultdict(lambda: {"n": 0, "min": 0.0, "items": []})
    for it in items:
        c = classify(it)
        it["cat"] = c
        by_cat[c]["n"] += 1
        by_cat[c]["min"] += (it["progress"] or it["duration"]) / 60.0
        by_cat[c]["items"].append(it)
    cats = sorted(by_cat.items(), key=lambda kv: (-kv[1]["n"], -kv[1]["min"]))
    authors = Counter(it["author"] for it in items if it["author"]).most_common(6)
    hours = Counter(datetime.fromtimestamp(it["view_at"], TZ).hour for it in items)

    deep = []
    for cat, d in by_cat.items():
        boost = DEEP_BOOST.get(cat, 0)
        if not boost:
            continue
        for it in d["items"]:
            mins = (it["progress"] or it["duration"]) / 60.0
            deep.append((boost * 2 + min(mins / 6.0, 4) + (1.5 if it["is_finish"] else 0), cat, it))
    deep.sort(key=lambda x: -x[0])
    picked, seen_a = [], Counter()
    for score, cat, it in deep:
        if len(picked) >= 4:
            break
        if seen_a[it["author"]] >= 1:
            continue
        seen_a[it["author"]] += 1
        picked.append((cat, it))

    return {
        "day": day.strftime("%Y-%m-%d"), "weekday": WEEK[day.weekday()],
        "total": total, "minutes": round(minutes),
        "finish_rate": round(full * 100.0 / total) if total else 0,
        "cats": [{"name": k, "n": v["n"], "min": round(v["min"])} for k, v in cats],
        "authors": authors,
        "hours": sorted(hours.items()),
        "deep": picked,
    }


def fmt_min(m):
    return "%d分钟" % m if m < 60 else "%d小时%d分" % (m // 60, m % 60)


def render(data):
    if data["total"] == 0:
        return "无"
    L = ["【%s %s 观看日报】" % (data["day"], data["weekday"]),
         "共 %d 个视频，约 %s，完播率 %d%%"
         % (data["total"], fmt_min(data["minutes"]), data["finish_rate"])]
    L += ["", "■ 板块分布"]
    for c in data["cats"][:6]:
        pct = round(c["n"] * 100.0 / data["total"])
        L.append("  %s %d个(%d%%) %s %s"
                 % (c["name"], c["n"], pct, "█" * max(1, round(pct / 10)), fmt_min(c["min"])))
    rest = data["cats"][6:]
    if rest:
        L.append("  其余：%s" % "、".join("%s%d" % (c["name"], c["n"]) for c in rest))
    if data["authors"]:
        L += ["", "■ 常看UP主",
              "  " + "、".join("%s(%d)" % (a, n) for a, n in data["authors"])]
    if data["hours"]:
        peak = sorted(data["hours"], key=lambda x: -x[1])[:3]
        L += ["", "■ 活跃时段", "  " + "、".join("%d点×%d" % (h, n) for h, n in sorted(peak))]
    if data["deep"]:
        L += ["", "■ 值得单独深入"]
        for i, (cat, it) in enumerate(data["deep"], 1):
            mins = round((it["progress"] or it["duration"]) / 60.0)
            L.append("  %d. [%s] %s" % (i, cat, it["title"][:40]))
            L.append("     UP:%s · %d分钟%s"
                     % (it["author"][:16], mins, " · 已看完" if it["is_finish"] else ""))
    return "\n".join(L)


def send_mail(user, auth, to, subject, body):
    host = os.environ.get("SMTP_HOST", "smtp.qq.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = user
    msg["To"] = to
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
        s.login(user, auth)
        s.sendmail(user, [to], msg.as_string())


def _load_cookie():
    c = os.environ.get("BILI_COOKIE", "").strip()
    if c:
        return c
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bili_cookie.txt")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    return ""


def _here():
    return os.path.dirname(os.path.abspath(__file__))


def _load_mail():
    u = os.environ.get("MAIL_USER", "").strip()
    a = os.environ.get("MAIL_AUTH", "").strip()
    t = os.environ.get("MAIL_TO", "").strip()
    p = os.path.join(_here(), "mail_config.txt")
    if (not u or not a) and os.path.exists(p):
        lines = [x.strip() for x in
                 open(p, encoding="utf-8").read().splitlines() if x.strip()]
        if len(lines) >= 2:
            u, a = lines[0], lines[1]
        if len(lines) >= 3 and lines[2]:
            t = lines[2]
    return u, a, (t or "10001@qq.com")


def handler(event, context):
    cookie = _load_cookie()
    mail_user, mail_auth, mail_to = _load_mail()
    mail_ready = bool(mail_user and mail_auth)
    dry = os.environ.get("DRY_RUN") == "1" or not mail_ready

    now = datetime.now(TZ)
    force = bool(isinstance(event, dict) and event.get("force"))
    # 抗时区门禁：触发器每 15 分钟一次，只在北京时间 21:00 这一档真正执行。
    # 无论平台的 cron 按 UTC 还是 UTC+8 解释，触发的时刻集合都是整刻钟，
    # 因此这一判断保证一天只发一次。
    if not force and not (now.hour == 21 and now.minute < 15):
        return {"ok": True, "skipped": True,
                "cst": now.strftime("%Y-%m-%d %H:%M"),
                "reason": "outside 21:00 window"}

    start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if not cookie:
        return {"ok": False, "error": "BILI_COOKIE not set"}

    ok, uname, mid = check_login(cookie)
    if not ok:
        body = "⚠️ B站登录已失效，今日日报无法生成。\n请更新 BILI_COOKIE 环境变量后恢复。"
        if not dry and mail_ready:
            send_mail(mail_user, mail_auth, mail_to, "B站日报：登录失效", body)
        return {"ok": False, "error": "login_expired", "sent": not dry and mail_ready}

    try:
        items = fetch_history(cookie, int(start.timestamp()))
    except RuntimeError as e:
        if "LOGIN_EXPIRED" in str(e):
            body = "⚠️ B站登录已失效（历史接口返回 -101），请更新 BILI_COOKIE。"
            if not dry and mail_ready:
                send_mail(mail_user, mail_auth, mail_to, "B站日报：登录失效", body)
            return {"ok": False, "error": "login_expired"}
        raise

    items = [it for it in items if it["view_at"] >= int(start.timestamp())]
    report = render(analyze(items, start))
    if not dry and not mail_ready:
        return {"ok": False, "error": "mail_not_configured",
                "count": len(items), "report": report}
    if not dry:
        send_mail(mail_user, mail_auth, mail_to,
                  "B站日报 %s（%d条）" % (start.strftime("%m-%d"), len(items)), report)
    return {"ok": True, "user": uname, "mid": mid, "count": len(items),
            "dry_run": dry, "report": report}


if __name__ == "__main__":
    import sys
    os.environ.setdefault("DRY_RUN", "1")
    if len(sys.argv) > 1 and sys.argv[1] == "-":
        pass
    print(json.dumps(handler({}, None), ensure_ascii=False, indent=2))
