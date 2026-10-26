# -*- coding: utf-8 -*-
"""每天 6 点的 AI / 计算机 / 网安 / 逆向 资讯简报。

流程：抓多个权威源的 RSS -> 关键词初筛 -> 大模型精选并去广告 -> 发 QQ。
"""
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import llm_chat
import tools

STATE = os.path.join(HERE, "state")
LOGDIR = os.path.join(HERE, "logs")
NAPCAT = os.environ.get("NAPCAT_URL", "http://127.0.0.1:3000")
OWNER = 10001
# 默认收件人；面板勾选后写入 llm.json 的 news_recipients 覆盖它
RECIPIENTS = [10001, 10005, 10006]


def recipients():
    try:
        with open(os.path.join(HERE, "llm.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        r = cfg.get("news_recipients")
        if isinstance(r, list) and r:
            return [int(x) for x in r]
    except Exception:
        pass
    return RECIPIENTS

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

SOURCES = [
    # 国内
    ("IT之家", "https://www.ithome.com/rss/"),
    ("量子位", "https://www.qbitai.com/feed"),
    ("Solidot", "https://www.solidot.org/index.rss"),
    ("安全客", "https://api.anquanke.com/data/v1/rss"),
    # 境外（服务器直连可用）
    ("arXiv", "https://arxiv.org/rss/cs.AI"),
    ("HackerNews", "https://hnrss.org/frontpage"),
    ("TechCrunch", "https://techcrunch.com/feed/"),
    ("ArsTechnica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("Krebs", "https://krebsonsecurity.com/feed/"),
    ("DarkReading", "https://www.darkreading.com/rss.xml"),
    ("CyberScoop", "https://www.cyberscoop.com/feed/"),
    ("OpenAI", "https://openai.com/blog/rss.xml"),
]

# 初筛：命中任一即保留，避免把娱乐八卦喂给模型
KEYS = [
    "ai", "人工智能", "大模型", "模型", "机器学习", "深度学习", "神经网络", "llm",
    "gpt", "claude", "gemini", "deepseek", "算力", "芯片", "gpu", "英伟达", "nvidia",
    "开源", "linux", "编程", "程序员", "算法", "架构", "云", "数据库", "安全",
    "漏洞", "攻击", "勒索", "木马", "逆向", "破解", "免杀", "渗透", "攻防", "ctf",
    "黑客", "供应链", "0day", "0-day", "补丁", "隐私", "加密", "量子",
    # 英文源用
    "security", "vulnerab", "exploit", "malware", "ransomware", "breach",
    "hacked", "hacker", "patch", "kernel", "zero-day", "research", "model",
    "neural", "agent", "openai", "anthropic", "deepmind", "llama", "inference",
    "training", "benchmark", "privacy", "encryption", "backdoor", "phishing",
]
JUNK = ["广告", "促销", "优惠", "折扣", "抽奖", "带货", "开团", "秒杀", "领券",
        "sponsored", "deal of the day", "buy now", "coupon"]


def log(msg):
    os.makedirs(LOGDIR, exist_ok=True)
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    with open(os.path.join(LOGDIR, "news.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch_feed(name, url, limit=40):
    try:
        req = Request(url, headers={"User-Agent": UA})
        with urlopen(req, timeout=20) as r:
            raw = r.read()
    except Exception as e:
        log("%s 抓取失败: %s" % (name, e))
        return []
    try:
        root = ET.fromstring(raw)
    except Exception as e:
        log("%s 解析失败: %s" % (name, e))
        return []

    out = []
    for item in root.iter():
        tag = item.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        d = {}
        for ch in item:
            k = ch.tag.split("}")[-1]
            if k in ("title", "link", "description", "summary", "pubDate", "updated", "published", "id"):
                d[k] = (ch.text or "").strip()
                if k == "link" and not d[k]:
                    d[k] = ch.get("href") or ""
        title = d.get("title") or ""
        link = d.get("link") or ""
        if not title or not link:
            continue
        out.append({
            "source": name,
            "title": re.sub(r"\s+", " ", title)[:120],
            "link": link,
            "desc": re.sub(r"\s+", " ", d.get("description") or d.get("summary") or "")[:200],
            "date": d.get("pubDate") or d.get("updated") or d.get("published") or "",
        })
        if len(out) >= limit:
            break
    return out


def keywords():
    """关注方向：面板里改过就用面板的，否则用默认。"""
    try:
        with open(os.path.join(HERE, "llm.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        ks = cfg.get("news_keywords")
        if isinstance(ks, list) and ks:
            return [str(k).strip().lower() for k in ks if str(k).strip()]
    except Exception:
        pass
    return list(KEYS)


def prefilter(items):
    keep = []
    seen = set()
    kws = keywords()
    for it in items:
        blob = (it["title"] + " " + it["desc"]).lower()
        if any(j in blob for j in JUNK):
            continue
        if not any(k in blob for k in kws):
            continue
        key = re.sub(r"\W+", "", it["title"].lower())[:36]
        if key in seen:
            continue
        seen.add(key)
        keep.append(it)
    return keep


def compose(items):
    lines = []
    for i, it in enumerate(items, 1):
        lines.append("%d. [%s] %s\n   %s\n   %s" % (
            i, it["source"], it["title"], it["link"], it["desc"][:120]))
    raw = "\n".join(lines)

    prompt = (
        "下面是今天从国内外科技/安全媒体抓到的候选条目，共 %d 条。\n\n%s\n\n"
        "请挑出 10-12 条对「人工智能 / 计算机 / 网络安全 / 逆向工程」从业者最有价值的，"
        "剔除广告、软文、促销、纯产品营销和无信息量的内容。英文条目请直接翻成中文。\n\n"
        "输出格式严格要求（不要开场白、不要结尾总结、不要编号以外多余内容）：\n"
        "▪ 中文标题（不超过28字）\n"
        "  事件：用一句话说清这是什么事（不超过40字，讲事实，不写评价、不写「值得看」）\n"
        "  链接\n\n"
        "每条三行，条与条之间空一行。"
        % (len(items), raw))
    ok, out = llm_chat.complete(
        prompt,
        system="你是严谨的技术资讯编辑，只输出清单本身，不加任何客套话。",
        max_tokens=1400)
    if not ok:
        raise RuntimeError(out)
    return out


def send(text, to=None):
    import urllib.request
    try:
        import zodiac as _z
    except Exception:
        _z = None
    got = []
    for uid in (to or recipients()):
        msg = text
        try:
            zl = _z.zodiac_line(uid) if _z else None
        except Exception:
            zl = None
        # 幸运运势单独发一条，不和早报混在一起
        if zl:
            try:
                body2 = json.dumps({"user_id": uid, "message": zl},
                                   ensure_ascii=False).encode("utf-8")
                req2 = Request(NAPCAT + "/send_private_msg", data=body2, method="POST",
                               headers={"Content-Type": "application/json"})
                with urlopen(req2, timeout=30) as r:
                    json.loads(r.read().decode("utf-8")).get("retcode")
            except Exception as e:
                log("幸运运势单独发送失败 %s: %s" % (uid, e))
        try:
            body = json.dumps({"user_id": uid, "message": msg},
                              ensure_ascii=False).encode("utf-8")
            req = Request(NAPCAT + "/send_private_msg", data=body, method="POST",
                          headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=30) as r:
                got.append((uid, json.loads(r.read().decode("utf-8")).get("retcode")))
        except Exception as e:
            got.append((uid, "err:%s" % e))
    return got


def main():
    try:
        import llm_chat as _lc
        if not _lc.feature_on(_lc.load_config(), "news"):
            log("资讯早报插件已关闭，本次跳过")
            return 0
    except Exception:
        pass
    all_items = []
    for name, url in SOURCES:
        got = fetch_feed(name, url)
        log("%s 抓到 %d 条" % (name, len(got)))
        all_items.extend(got)

    if not all_items:
        send("⚠️ 今天没抓到任何资讯源，请检查网络或源是否失效。")
        return 1

    kept = prefilter(all_items)
    log("初筛后 %d 条" % len(kept))
    if not kept:
        send("今天没有符合你关注方向的新资讯。")
        return 0

    kept = kept[:90]
    try:
        text = compose(kept)
    except Exception as e:
        log("模型整理失败: %s" % e)
        send("⚠️ 早报生成失败：%s" % e)
        return 2

    text = text.strip()
    if not text:
        send("今天没有值得推送的内容。")
        return 0

    head = "【AI · 安全 早报】%s" % time.strftime("%Y-%m-%d")
    if not text.startswith("【"):
        text = head + "\n\n" + text

    os.makedirs(STATE, exist_ok=True)
    with open(os.path.join(STATE, "news-%s.txt" % time.strftime("%Y-%m-%d")),
              "w", encoding="utf-8") as f:
        f.write(text)

    res = send(text)
    log("已发送 长度=%d 结果=%s" % (len(text), res))
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
