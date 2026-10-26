#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""名片点赞（多账号）：每天 05:00 给各账号配置的目标各点 N 个赞。
配置（llm.json）：
  like_card = {targets, times}                        机器号(3000) 点赞目标（面板选人）
  like_card_accounts = [{account, port, targets, times}, ...]  其他登录小号各自的目标
"""
import urllib.request, json, os, time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state")
MAIN_NAPCAT = "http://127.0.0.1:3000"
DEFAULT_TARGETS = [10001, 10002, 10003]


def load_cfg():
    try:
        with open(os.path.join(HERE, "llm.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def log(msg):
    try:
        os.makedirs(STATE, exist_ok=True)
        with open(os.path.join(STATE, "like_log.txt"), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def napcat(base, path, payload=None, timeout=12):
    req = urllib.request.Request(base + "/" + path,
                                 data=json.dumps(payload or {}).encode(),
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def like_one(base, tag, qq, want, per_call=10, gap=2.5):
    """给一个人点 want 个赞。

    QQ 规则（实测）：
      - 单次调用最多 10 个（times>10 返回"点赞数无效"）
      - 每人每天有上限（普通 10 / VIP 20 / SVIP 50 左右）
      - 超了返回"今日同一好友点赞数已达上限"
    所以按 10 个一批循环，遇到上限就停。
    """
    done = 0
    tries = 0
    while done < want and tries < 8:
        tries += 1
        n = min(per_call, want - done)
        try:
            r = napcat(base, "send_like", {"user_id": int(qq), "times": n})
        except Exception as e:
            log("[%s] 点赞 %s 异常: %s" % (tag, qq, e))
            break
        msg = (r.get("message") or r.get("wording") or "")
        if r.get("retcode") == 0:
            done += n
            log("[%s] 点赞 %s +%d（累计 %d/%d）" % (tag, qq, n, done, want))
        elif "上限" in msg:
            log("[%s] 点赞 %s 到上限了（已点 %d/%d）：%s" % (tag, qq, done, want, msg))
            break
        elif "无效" in msg:
            # 单次数量不被接受，退到 5 再试
            if n > 5:
                per_call = 5
                continue
            log("[%s] 点赞 %s 参数无效：%s" % (tag, qq, msg))
            break
        else:
            log("[%s] 点赞 %s 失败：%s" % (tag, qq, msg[:80]))
            break
        time.sleep(gap)
    return done


def do_likes(base, tag, targets, times, per_target=None):
    ok, fail = 0, 0
    detail = []
    per_target = per_target or {}
    for qq in targets:
        want = int(per_target.get(str(qq)) or per_target.get(qq) or times)
        got = like_one(base, tag, qq, want)
        detail.append("%s:%d/%d" % (qq, got, want))
        if got > 0:
            ok += 1
        else:
            fail += 1
    log("[%s] 本轮完成 ok=%d fail=%d 明细=%s" % (tag, ok, fail, " ".join(detail)))
    return ok, fail


def main():
    cfg = load_cfg()
    feats = cfg.get("features") or {}
    if not feats.get("like_card"):
        log("名片点赞插件关闭，跳过")
        return
    lc = cfg.get("like_card") or {}
    times = int(lc.get("times") or 10)
    tot_ok = tot_fail = 0
    # 机器号(3000)
    m_ok, m_fail = do_likes(MAIN_NAPCAT, "主号", lc.get("targets") or DEFAULT_TARGETS,
                            times, lc.get("per_target") or {})
    tot_ok += m_ok; tot_fail += m_fail
    # 其他登录小号
    for acct in cfg.get("like_card_accounts") or []:
        base = acct.get("base") or ("http://127.0.0.1:%d" % int(acct.get("port") or 3003))
        tgt = acct.get("targets") or []
        n = int(acct.get("times") or times)
        a_ok, a_fail = do_likes(base, str(acct.get("account")),
                                tgt, n, acct.get("per_target") or {})
        tot_ok += a_ok; tot_fail += a_fail
    print("like done ok=%d fail=%d" % (tot_ok, tot_fail))


if __name__ == "__main__":
    main()
