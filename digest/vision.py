# -*- coding: utf-8 -*-
"""识图：把图片交给配置好的视觉模型，拿一句描述回来。"""
import base64
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import llm_chat


def _cfg():
    try:
        return (llm_chat.load_config() or {}).get("vision") or {}
    except Exception:
        return {}


def configured():
    c = _cfg()
    return bool(c.get("base_url") and c.get("model") and c.get("api_key"))


def _fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0", "Referer": "https://qun.qq.com/"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("图片太大")
    ctype = "image/jpeg"
    head = data[:4]
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        ctype = "image/png"
    elif head[:4] == b"GIF8":
        ctype = "image/gif"
    elif head[:2] == b"BM":
        ctype = "image/bmp"
    return data, ctype


def describe(url=None, data=None, prompt=None, timeout=40):
    """返回一句中文描述；没配或失败返回 None。"""
    c = _cfg()
    if not (c.get("base_url") and c.get("model") and c.get("api_key")):
        return None

    if data is None:
        if not url:
            return None
        try:
            data, ctype = _fetch(url)
        except Exception as e:
            return "（图片没取下来：%s）" % str(e)[:40]
    else:
        ctype = "image/jpeg"

    b64 = base64.b64encode(data).decode()
    img = "data:%s;base64,%s" % (ctype, b64)
    payload = {
        "model": c["model"],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt or
                 "用一两句中文说清这张图里有什么：主体、文字、氛围。只描述，不评价。"},
                {"type": "image_url", "image_url": {"url": img}},
            ],
        }],
        "max_tokens": 400,
    }
    base = c["base_url"].rstrip("/")
    if not base.endswith("/chat/completions"):
        base = base + "/chat/completions"
    req = urllib.request.Request(
        base, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + c["api_key"]}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        txt = (d.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        return txt.strip() or None
    except Exception as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:120]
        except Exception:
            pass
        return "（识图失败：%s%s）" % (str(e)[:60], (" " + body) if body else "")


if __name__ == "__main__":
    print("已配置:", configured())
    if len(sys.argv) > 1:
        print(describe(sys.argv[1]))
