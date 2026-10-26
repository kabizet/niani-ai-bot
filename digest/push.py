#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用推送脚本：把文字/文件推到号主 QQ。

用法：
  python3 push.py "备份完成，1.6M"
  python3 push.py "今日报告" /home/youruser/digest/logs/monitor.log
  echo "内容" | python3 push.py -
"""
import json
import os
import sys
import urllib.request

NAPCAT = "http://127.0.0.1:3000"
OWNER = 10001


def send_text(text, qq=OWNER):
    body = json.dumps({"user_id": qq, "message": str(text)[:4000]}).encode()
    req = urllib.request.Request(NAPCAT + "/send_private_msg", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def send_file(path, qq=OWNER, name=None):
    import bot_server as bs
    return bs.upload_file("private", qq, path, name or os.path.basename(path))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    if not args:
        print(__doc__)
        sys.exit(1)
    if args[0] == "-":
        text = sys.stdin.read()
    else:
        text = args[0]
    print(send_text(text).get("retcode"))
    if len(args) > 1 and os.path.isfile(args[1]):
        r = send_file(args[1])
        print("file:", (r or {}).get("retcode"))
