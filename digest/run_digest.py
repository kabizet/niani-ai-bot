# -*- coding: utf-8 -*-
"""每日日报：B站 + 抖音 -> 合并分析 -> NapCat 发 QQ 私聊。

设计要点：
  * 两个平台各自独立采集，一个挂了另一个照发
  * 平台级失败会单独告警，但不会中断整份日报
  * 当天两边都没数据 -> 只发一个"无"
  * 发成功后写日期标记，供 23:30 补偿任务去重

用法:
  python3 run_digest.py              正常执行
  python3 run_digest.py --if-missing 今天已发过就跳过
  python3 run_digest.py --force      忽略标记强制发送
"""
import json
import os
import sys
import traceback
from collections import Counter
from datetime import datetime
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import digest_core as core
import douyin_client as dy

TARGET_QQ = int(os.environ.get("TARGET_QQ", "10001"))
NAPCAT = os.environ.get("NAPCAT_URL", "http://127.0.0.1:3000")
LOGDIR = os.path.join(HERE, "logs")
STATEDIR = os.path.join(HERE, "state")
WEEK = core.WEEK


def log(msg):
    os.makedirs(LOGDIR, exist_ok=True)
    line = "[%s] %s" % (datetime.now(core.TZ).strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    with open(os.path.join(LOGDIR, "digest.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def recipients():
    """日报收件人：固定只发号主主号。"""
    return [TARGET_QQ]


def send_qq(text, uid=None):
    targets = [uid] if uid else recipients()
    results = []
    for t in targets:
        payload = json.dumps({"user_id": int(t), "message": text},
                             ensure_ascii=False).encode("utf-8")
        req = Request(NAPCAT + "/send_private_msg", data=payload,
                      headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=30) as r:
                results.append((t, json.loads(r.read().decode("utf-8")).get("retcode")))
        except Exception as e:
            results.append((t, "err:%s" % e))
    return results


def marker_path(day):
    os.makedirs(STATEDIR, exist_ok=True)
    return os.path.join(STATEDIR, "%s.sent" % day.strftime("%Y-%m-%d"))


def fmt_min(m):
    m = int(round(m))
    return "%d分钟" % m if m < 60 else "%d小时%d分" % (m // 60, m % 60)


# ---------- 采集 ----------

def collect_bili(start):
    """返回 (items, error)。"""
    try:
        cookie = core._load_cookie()
        if not cookie:
            return [], "没有 cookie"
        ok, uname, mid = core.check_login(cookie)
        if not ok:
            return [], "登录失效"
        items = core.fetch_history(cookie, int(start.timestamp()))
        items = [it for it in items if it["view_at"] >= int(start.timestamp())]
        return items, None
    except Exception as e:
        if "LOGIN_EXPIRED" in str(e):
            return [], "登录失效"
        return [], str(e)


def collect_douyin(start):
    try:
        items = dy.fetch(int(start.timestamp() * 1000))
        return items, None
    except dy.CookieExpired as e:
        return [], "cookie 失效: %s" % e
    except Exception as e:
        return [], str(e)


# ---------- 渲染 ----------

def cat_of(title, author, tag=""):
    return core.classify({"title": title, "tag_name": tag, "author": author})


def render_combined(day, bili, dy_items, bili_err, dy_err):
    if not bili and not dy_items:
        parts = ["无"]
        if bili_err or dy_err:
            parts.append("")
            if bili_err:
                parts.append("（B站异常：%s）" % bili_err)
            if dy_err:
                parts.append("（抖音异常：%s）" % dy_err)
        return "\n".join(parts)

    L = ["【%s %s 观看日报】" % (day.strftime("%Y-%m-%d"), WEEK[day.weekday()])]

    # ---- B站 ----
    if bili:
        mins = sum((it["progress"] or it["duration"]) for it in bili) / 60.0
        full = sum(1 for it in bili if it["is_finish"])
        L.append("")
        L.append("■ B站  %d个 · 约%s · 完播%d%%"
                 % (len(bili), fmt_min(mins),
                    round(full * 100.0 / len(bili)) if bili else 0))
        cats = Counter(cat_of(it["title"], it["author"], it["tag_name"]) for it in bili)
        L.append("  板块：" + "、".join("%s%d" % (k, v) for k, v in cats.most_common(5)))
        ups = Counter(it["author"] for it in bili if it["author"])
        L.append("  UP：" + "、".join("%s(%d)" % (a, n) for a, n in ups.most_common(4)))
    elif bili_err:
        L.append("")
        L.append("■ B站  采集失败（%s）" % bili_err)

    # ---- 抖音 ----
    if dy_items:
        dur = sum(i["duration_s"] for i in dy_items) / 60.0
        L.append("")
        L.append("■ 抖音  %d个 · 视频总时长约%s" % (len(dy_items), fmt_min(dur)))
        cats = Counter(cat_of(i["title"], i["author"]) for i in dy_items)
        L.append("  板块：" + "、".join("%s%d" % (k, v) for k, v in cats.most_common(5)))
        aus = Counter(i["author"] for i in dy_items if i["author"])
        L.append("  作者：" + "、".join("%s(%d)" % (a, n) for a, n in aus.most_common(4)))
        hot = sorted(dy_items, key=lambda x: -(x.get("digg") or 0))[:1]
        if hot and hot[0].get("digg"):
            L.append("  最热：%s（%d赞）" % (hot[0]["title"][:26], hot[0]["digg"]))
    elif dy_err:
        L.append("")
        L.append("■ 抖音  采集失败（%s）" % dy_err)
    else:
        L.append("")
        L.append("■ 抖音  今日无记录")

    # ---- 活跃时段 ----
    hours = Counter()
    for it in bili:
        hours[datetime.fromtimestamp(it["view_at"], core.TZ).hour] += 1
    for i in dy_items:
        hours[datetime.fromtimestamp(i["watch_ms"] / 1000, core.TZ).hour] += 1
    if hours:
        peak = sorted(hours.items(), key=lambda kv: -kv[1])[:3]
        L.append("")
        L.append("■ 活跃时段  " + "、".join("%d点×%d" % (h, n) for h, n in sorted(peak)))

    # ---- 值得深入 ----
    deep = []
    for it in bili:
        c = cat_of(it["title"], it["author"], it["tag_name"])
        boost = core.DEEP_BOOST.get(c, 0)
        if boost:
            m = (it["progress"] or it["duration"]) / 60.0
            deep.append((boost * 2 + min(m / 6.0, 4) + (1.5 if it["is_finish"] else 0),
                         "B站/" + c, it["title"], it["author"]))
    for i in dy_items:
        c = cat_of(i["title"], i["author"])
        boost = core.DEEP_BOOST.get(c, 0)
        if boost:
            m = i["duration_s"] / 60.0
            deep.append((boost * 2 + min(m / 6.0, 4) + min((i.get("digg") or 0) / 20000.0, 2),
                         "抖音/" + c, i["title"], i["author"]))
    deep.sort(key=lambda x: -x[0])
    picked, seen = [], Counter()
    for score, tag, title, author in deep:
        if len(picked) >= 4:
            break
        if seen[author] >= 1:
            continue
        seen[author] += 1
        picked.append((tag, title, author))
    if picked:
        L.append("")
        L.append("■ 值得单独深入")
        for n, (tag, title, author) in enumerate(picked, 1):
            L.append("  %d. [%s] %s" % (n, tag, title[:32]))
            L.append("     %s" % author[:18])

    return "\n".join(L)


def main():
    argv = sys.argv[1:]
    force = "--force" in argv
    if_missing = "--if-missing" in argv

    now = datetime.now(core.TZ)
    day_arg = None
    for a in argv:
        if a.startswith("--date="):
            day_arg = a.split("=", 1)[1]
    if day_arg:
        start = datetime.strptime(day_arg, "%Y-%m-%d").replace(tzinfo=core.TZ)
    else:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(hour=0, minute=0, second=0, microsecond=0) \
        + __import__("datetime").timedelta(days=1)
    marker = marker_path(start)

    if if_missing and not force and os.path.exists(marker):
        log("今天已发过，补偿任务跳过")
        return 0

    bili, bili_err = ([], None)
    dy_items, dy_err = ([], None)
    try:
        import llm_chat as _lc
        cfg = _lc.load_config()
        plats = cfg.get("digest_platforms") or ["bili", "douyin"]
        if not _lc.feature_on(cfg, "digest"):
            log("观看日报插件已关闭，本次跳过")
            return 0
    except Exception:
        plats = ["bili", "douyin"]

    if "bili" in plats:
        bili, bili_err = collect_bili(start)
        bili = [it for it in bili if it["view_at"] < int(end.timestamp())]
        log("B站 %d 条%s" % (len(bili), "" if not bili_err else " 错误:" + bili_err))

    if "douyin" in plats:
        dy_items, dy_err = collect_douyin(start)
        dy_items = [i for i in dy_items
                    if start.timestamp() * 1000 <= i["watch_ms"] < end.timestamp() * 1000]
        log("抖音 %d 条%s" % (len(dy_items), "" if not dy_err else " 错误:" + dy_err))

    if bili_err:
        try:
            send_qq("⚠️ B站日报采集失败：%s\n（抖音数据不受影响）" % bili_err)
        except Exception as e:
            log("告警发送失败: %s" % e)
    if dy_err:
        try:
            send_qq("⚠️ 抖音采集失败：%s\n需要重新登录抖音时请找管理员。"
                    % dy_err)
        except Exception as e:
            log("告警发送失败: %s" % e)

    report = render_combined(start, bili, dy_items, bili_err, dy_err)
    # 存一份给对话机器人当上下文
    os.makedirs(STATEDIR, exist_ok=True)
    with open(os.path.join(STATEDIR, start.strftime("%Y-%m-%d") + ".report.txt"),
              "w", encoding="utf-8") as f:
        f.write(report)
    res = send_qq(report)
    log("已发送 长度=%d 结果=%s" % (len(report), res))
    with open(marker, "w") as f:
        f.write(datetime.now(core.TZ).isoformat())
    print(report)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log("异常:\n" + traceback.format_exc())
        try:
            send_qq("⚠️ 日报任务异常，请查看服务器日志 ~/digest/logs/digest.log")
        except Exception:
            pass
        sys.exit(5)
