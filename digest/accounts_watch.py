# -*- coding: utf-8 -*-
"""独立的多账号看门狗循环（不动 bot_server）。"""
import sys
import time

sys.path.insert(0, "/home/youruser/digest")
import accounts


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open("/home/youruser/digest/logs/accounts.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    log("多账号看门狗启动")
    while True:
        try:
            accounts.watch(log=log)
        except Exception as e:
            log("检查异常: %s" % e)
        time.sleep(30)
