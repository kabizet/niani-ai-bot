# -*- coding: utf-8 -*-
"""把星座运势并到早报里发。

用法：news_brief.py 在发送时按人追加；也可以单独跑。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import divination


def zodiac_line(qq):
    """返回这个人的星座行，没有生日就返回 None。"""
    try:
        with open(os.path.join(HERE, "llm.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return None
    if not (cfg.get("features") or {}).get("zodiac", True):
        return None
    for item in (cfg.get("zodiac") or []):
        if str(item.get("qq")) == str(qq) and item.get("enabled", True):
            return divination.lucky(qq, item.get("birthday"))
    return None


if __name__ == "__main__":
    for q in sys.argv[1:]:
        print(q, "->", zodiac_line(q))
