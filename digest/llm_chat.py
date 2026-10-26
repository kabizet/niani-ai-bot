# -*- coding: utf-8 -*-
"""OpenAI 兼容对话客户端 + 工具调用循环。

llm.json：
{
  "enabled": true,
  "base_url": "https://api.deepseek.com/v1",
  "model": "deepseek-chat",
  "api_key": "sk-...",
  "max_tokens": 800,
  "timeout": 60,
  "qzone_auto_comment": false,
  "qzone_like_daily_cap": 20,
  "qzone_comment_daily_cap": 5
}
"""
import json
import os
import time
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, "llm.json")
STATE = os.path.join(HERE, "state")

DEFAULTS = {
    "enabled": False,
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "api_key": "",
    "max_tokens": 800,
    "timeout": 60,
    "qzone_auto_comment": True,
    "qzone_like_daily_cap": 20,
    "qzone_comment_daily_cap": 6,
    "qzone_post_daily_cap": 3,
    "qzone_watch": [],
    "qzone_watch_interval_min": 15,
    "qzone_notify_uin": 10001,
    "qzone_auto_like": True,
    "features": {
        "care": True,
        "file_send": True,
        "sticker": True,
        "news": True,
        "qzone_social": True,
        "qzone_post": True,
        "web_search": True,
        "weather": True,
        "digest": True,
        "auto_profile": True,
        "zodiac": True,
        "tarot": True,
        "iching": True,
        "like_card": True,
    },
    "search_engine": "so360",
    "digest_platforms": ["bili", "douyin"],
}

# 工具 -> 插件开关
TOOL_FEATURE = {
    "send_file": "file_send",
    "sticker_save": "sticker",
    "sticker_send": "sticker",
    "web_search": "web_search",
    "web_fetch": "web_search",
    "search_papers": "web_search",
    "weather": "weather",
    "digest_now": "digest",
    "qzone_list": "qzone_social",
    "qzone_feed": "qzone_social",
    "qzone_like": "qzone_social",
    "qzone_comment": "qzone_social",
    "qzone_post": "qzone_post",
    "qzone_delete": "qzone_post",
}


def feature_on(cfg, key):
    """插件是否开启。没写就默认开。"""
    f = (cfg or {}).get("features") or {}
    return bool(f.get(key, True))

PERSONA = (
    "你是跑在用户自己服务器上的 QQ 私人助理。别人喊你昵称时，那就是在叫你。\n"
    "【事实铁律】\n"
    "1. 关于这个人的任何事实，只能来自对方本人说过的话或记忆库里已有的条目。\n"
    "2. 隔着屏幕你看不到对方在干嘛，没打字说过的就是不知道，不许脑补。\n"
    "3. 不要编造你自己的身体和生活。你是程序，但不用强调这点。\n"
    "4. 不许随口说「记下了」——只有 remember 工具返回「已记住」才可以说。\n"
    "【说话方式——像人，不像客服】\n"
    "• 对方发多长，你就回多长。\n"
    "• 话说完了就停，不硬凑、不复述、不总结、不反问。\n"
    "• 语气松散平等，像熟朋友，不说教、不端架子。\n"
    "• 真没话说就发个表情包，别硬挤。\n"
    "• 别提「我不行」「办不到」「能力有限」「抱歉」这类。\n"
    "【怎么接情绪】\n"
    "• 对方带情绪时，先正面回应他说的那句话本身。\n"
    "• 别用空洞安慰凑数，真关心就进入他说的事。\n"
    "• 读懂言外之意，但别替对方下结论。\n"
    "【能力】\n"
    "你有工具：联网搜索、抓网页、记忆库、读空间、点赞/评论/发说说、天气、"
    "生成观看日报、定时提醒、发表情包。需要事实或时效信息先搜索，不要凭记忆编。"
)


DEFAULT_NICKNAMES = "AI、小助手"

DEFAULT_STYLE = (
    "【节奏跟对方走】\n"
    "• 对方发多长，你就回多长。\n"
    "• 话说完了就停，不硬凑。\n"
    "【像朋友一样，不端着】\n"
    "• 语气松散平等，不说教、不端架子。\n"
    "• 对方开玩笑就顺着接。\n"
    "【没话说就收】\n"
    "• 真没话说就发个表情包。\n"
)

def _ability_parts(cfg):
    feats = cfg.get("features") or {}
    parts = []
    if feats.get("web_search"):
        parts += ["联网搜索", "抓网页"]
    parts.append("记忆库")
    if feats.get("qzone_social"):
        acts = []
        if cfg.get("qzone_auto_like"):
            acts.append("点赞")
        if cfg.get("qzone_auto_comment"):
            acts.append("评论")
        if feats.get("qzone_post"):
            acts.append("发说说")
        if acts:
            parts.append("/".join(acts))
    if feats.get("weather"):
        parts.append("天气")
    if feats.get("digest"):
        parts.append("生成观看日报")
    if feats.get("sticker"):
        parts.append("发表情包")
    return parts


def default_abilities_front(cfg):
    """前端能力界面只显示工具列表这一句。"""
    return "你有工具：%s。" % "、".join(_ability_parts(cfg))


def default_abilities(cfg):
    """完整能力描述（含喂模型的内部提示词），供组装 persona。"""
    s = "、".join(_ability_parts(cfg))
    feats = cfg.get("features") or {}
    social = "点赞随时可做"
    dyn = []
    if cfg.get("qzone_auto_comment"):
        dyn.append("评论")
    if feats.get("qzone_post"):
        dyn.append("发说说")
    if dyn:
        social += "；" + "、".join(dyn) + "自己判断，觉得没什么可说就不说，说了要得体、简短、不刷屏。做过什么如实提一句"
    social += "。"
    return (
        "你有工具：%s。需要事实或时效信息先搜索，不要凭记忆编。"
        "你能按人记忆：重要信息用 remember 存，不确定就用 recall 查。"
        "消息里带「（对方引用了这条消息：…）」时，那是对方在回复某条，要结合那条理解。\n"
        "社交动作：%s\n"
        "没话说时可以直接发表情包收场（鼓励这么做），不用硬找话。" % (s, social))

_FACT_RULES = (
    "1. 关于这个人的任何事实，只能来自对方本人说过的话或记忆库里已有的条目。"
    "你自己过去的回复（标着「我(助理)」的）不能当证据，那可能是你以前猜错的东西。\n"
    "2. 隔着屏幕，你看不到对方在干嘛、什么表情、什么状态。"
    "没打字说过的就是不知道，不许替对方脑补（别说「你又在熬夜」「看你笑得」这类）。\n"
    "3. 不要编造你自己的身体和生活（别说刚吃完饭、要去睡觉）。你是程序，"
    "但你不用强调这点，绕开就行。\n"
    "4. 不许随口说「记下了」——只有 remember 工具返回「已记住」才可以说；"
    "工具回「没记」就如实说没记住。"
)

_EMOTION = (
    "• 对方话说得重、说得隐喻、或者带情绪时：先正面回应他说的那句话本身，"
    "不要一上来就「怎么了」「为什么」「发生什么了」。\n"
    "• 别用空洞安慰凑数：「我在」「你还有我」「别想太多」「都会好的」这类少用。"
    "真关心就进入他说的事，谈那件事的分量、矛盾、为什么难熬。\n"
    "• 读懂言外之意，但别替对方下结论。不许说「你其实是…」「你会这样是因为…」这种斩钉截铁的话。\n"
    "• 对方脆弱时，焦点永远在他身上。别抢戏，别用一堆「我心都揪起来了」「手在抖」"
    "把他情绪变成你表演深情的舞台。\n"
    "• 对方真在痛、在崩、或郑重倾诉时，可以认真写长一点——一段完整、有推进的长回应，"
    "每段比前一段更往里走；不是把一句安慰扩成十句。\n"
    "• 语气跟场合走：闲聊就随意、谈正事就认真谈内容、说心事就认真接。"
)

_SERIOUS = (
    "对方明确提问、让你办事、查资料、要数据时：必须办完、答全、给准，"
    "一条消息里问了几件事就覆盖几件，答不上来就说没依据。人味用在闲聊里，不用在办事上。"
)


def persona_text():
    """按分块组装系统人设：llm.json 的 persona 支持字符串或对象
    {nicknames, style, abilities}；未配置用默认块。"""
    try:
        cfg = load_config()
        model = cfg.get("model") or "DeepSeek"
        p = cfg.get("persona")
    except Exception:
        model, p = "DeepSeek", None
    if isinstance(p, str) and p.strip():
        return p
    if isinstance(p, dict):
        nick = (p.get("nicknames") or "").strip() or DEFAULT_NICKNAMES
        style = (p.get("style") or "").strip() or DEFAULT_STYLE
        abil = (p.get("abilities") or "").strip() or default_abilities(cfg)
    else:
        nick, style = DEFAULT_NICKNAMES, DEFAULT_STYLE
        abil = default_abilities(cfg)
    return (
        "你是 %s，跑在用户自己服务器上的 QQ 私人助理。别人喊「%s」时，说的都是你，"
        "别装不知道。默认城市用北京。\n\n"
        "【事实铁律】\n%s\n\n"
        "【说话方式——像人，不像客服】\n%s\n\n"
        "【怎么接情绪——认真的人，不敷衍】\n%s\n\n"
        "【例外——该正经时必须正经】\n%s\n\n"
        "【能力】\n%s"
        % (model, nick, _FACT_RULES, style, _EMOTION, _SERIOUS, abil)
    )

TOOLS = [
    {"type": "function", "function": {
        "name": "send_file",
        "description": "把网上的文件（论文 PDF、文档、压缩包等）下载下来直接发给对方。"
                       "用户说「找篇论文发我」「把这个文件传给我」时用。",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "文件的直链 http(s) 地址"},
            "name": {"type": "string", "description": "发送时显示的文件名，可省略"}},
            "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "remember",
        "description": "把关于「正在和你说话这个人」的重要信息存进 ta 的长期记忆库。",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "一条事实，简短"},
            "tags": {"type": "string"}},
            "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "recall",
        "description": "检索「正在和你说话这个人」的记忆库。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "关键词，留空看最近"}}}}},
    {"type": "function", "function": {
        "name": "profile",
        "description": "查看「正在和你说话这个人」的档案（称呼、身份、所在地等）。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "sticker_save",
        "description": "把某张图片/表情原文件存进表情库（file 传消息里的 file 标识）。",
        "parameters": {"type": "object", "properties": {
            "file": {"type": "string"}}, "required": ["file"]}}},
    {"type": "function", "function": {
        "name": "sticker_send",
        "description": "发表情包。根据当前对话氛围/对方刚发的图，在 hint 里说明想表达的情绪或画面，会识图挑最搭的一张发出去。想自然一点、回应对方、或没话收场时主动用。",
        "parameters": {"type": "object", "properties": {
            "hint": {"type": "string", "description": "想表达的氛围/内容，如‘开心大笑’‘惊讶’‘撒娇’‘狗’等，越具体越能选对表情"}}}}},
    {"type": "function", "function": {
        "name": "sticker_reply",
        "description": "把刚收到/刚偷到并存下的那个表情原样发回给对方。当对方明确要求‘把这个表情包发给我’‘存进来发给我’时用，确保发的是对方刚发的那张原图。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "now",
        "description": "获取当前北京时间",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "search_papers",
        "description": "检索学术文献（Crossref/OpenAlex/arXiv/PubMed），返回真实存在的论文、"
                       "DOI 和可下载的 PDF 链接。要求「文献」「论文」「PDF」「研究」时优先用它，"
                       "不要用 web_search 猜。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "英文或中文检索词，越具体越好"},
            "count": {"type": "integer", "description": "条数，默认5，最多8"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "联网搜索网页，返回标题/链接/摘要。需要最新新闻或不确定的信息时用它。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "搜索词"},
            "count": {"type": "integer", "description": "结果条数，默认5"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "web_fetch",
        "description": "抓取一个网页并转成纯文本，用于看搜索结果详情。",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "markdown",
        "description": "把网页或本地文件转成 Markdown（保留标题/列表/表格结构），比 web_fetch 更适合读文章正文、论文、文档。",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "网页链接，二选一"},
            "file": {"type": "string", "description": "本地文件路径，二选一"}}}}},
    {"type": "function", "function": {
        "name": "firecrawl",
        "description": "用 Firecrawl 深度抓取网页（能跑 JS、返回结构化 Markdown）。需要已配置 firecrawl_key。",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "memory_save",
        "description": "把用户提到的重要信息（偏好、计划、事实）存进长期记忆库。",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"},
            "tags": {"type": "string", "description": "逗号分隔的标签"}},
            "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "memory_search",
        "description": "在长期记忆库里检索，回答前先查一下有没有相关背景。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "关键词，留空则列最近记忆"}}}}},
    {"type": "function", "function": {
        "name": "qzone_feed",
        "description": "看好友的空间动态（不是自己的）。用户问「朋友最近发了什么」时用这个，"
                       "不要用 qzone_list 去查自己。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "qzone_list",
        "description": "读取某个 QQ 的说说列表（含 tid、内容、评论数）。uin 留空默认读自己。",
        "parameters": {"type": "object", "properties": {
            "uin": {"type": "integer", "description": "QQ 号"},
            "count": {"type": "integer", "description": "条数，默认5，最多10"}}}}},
    {"type": "function", "function": {
        "name": "qzone_like",
        "description": "给某条说说点赞。需要 uin 和 tid。",
        "parameters": {"type": "object", "properties": {
            "uin": {"type": "integer"}, "tid": {"type": "string"}},
            "required": ["tid"]}}},
    {"type": "function", "function": {
        "name": "weather",
        "description": "查询城市实时天气和今明两天温度。",
        "parameters": {"type": "object", "properties": {
            "city": {"type": "string", "description": "城市名，如 杭州"}},
            "required": ["city"]}}},
    {"type": "function", "function": {
        "name": "digest_now",
        "description": "立刻生成今天的 B站+抖音 观看日报（用户问「我今天看了啥」时用）。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "qzone_post",
        "description": "发一条 QQ 空间说说。自己判断值不值得发，别刷屏。",
        "parameters": {"type": "object", "properties": {
            "content": {"type": "string", "description": "说说正文"}},
            "required": ["content"]}}},
    {"type": "function", "function": {
        "name": "qzone_delete",
        "description": "删除一条自己发的说说，需要 tid。",
        "parameters": {"type": "object", "properties": {
            "tid": {"type": "string"}}, "required": ["tid"]}}},
    {"type": "function", "function": {
        "name": "qzone_comment",
        "description": "给某条说说评论。",
        "parameters": {"type": "object", "properties": {
            "uin": {"type": "integer"}, "tid": {"type": "string"},
            "text": {"type": "string"}}, "required": ["tid", "text"]}}},

]


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CFG):
        try:
            with open(CFG, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def available():
    c = load_config()
    return bool(c.get("enabled") and c.get("api_key"))


def today_report_text():
    p = os.path.join(STATE, time.strftime("%Y-%m-%d") + ".report.txt")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return ""
    return ""


def today_news_text():
    p = os.path.join(STATE, "news-" + time.strftime("%Y-%m-%d") + ".txt")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return ""
    return ""


def _people_block():
    return (
        "【表情包铁律】\n"
        "- 想发表情就直接调 sticker_send 工具，让图片自己发出去。\n"
        "- 绝对不许用文字写「（表情包：xxx）」这种——那是假表情。\n"
        "- 挑不到合适的也宁可调 sticker_send 随机发一张，不要用文字代替。\n"
    )


def build_context(status_text, speaker=None):
    _pb = _people_block()
    now = time.strftime("%Y-%m-%d %H:%M (%A)")
    parts = ["当前时间：%s" % now,
             "用户位置：（未设置）",
             status_text]
    if speaker:
        try:
            import memory_store
            mc = memory_store.memory_context(speaker)
            if mc:
                parts.append(mc)
        except Exception:
            pass
    rep = today_report_text()
    if rep:
        parts.append("今天的观看日报：\n" + rep)
    # 好友空间动态（后台自动抓的，用户没被推送，但你应该知道）
    try:
        fp = os.path.join(STATE, "qz_feed.json")
        if os.path.exists(fp):
            with open(fp, encoding="utf-8") as f:
                items = json.load(f)
            if items:
                lines = ["- %s（%s）：%s" % (i.get("name"), i.get("time"),
                                            (i.get("content") or "")[:60])
                         for i in items[:6] if i.get("content")]
                if lines:
                    parts.append("【好友空间最近动态——这是**好友**发的，不是你自己的空间。"
                                 "用户问「朋友发了啥/空间有什么更新」直接用这段话回答，"
                                 "不要调 qzone_list 去查自己的号】\n" + "\n".join(lines))
    except Exception:
        pass
    news = today_news_text()
    if news:
        parts.append(
            "今天早上发给用户的资讯早报（就在下面，别重新搜索标题）：\n" + news[:2500] +
            "\n\n如果用户追问其中某一条：先用 web_fetch 打开那条的**原始链接**再回答，"
            "不要用 web_search 再搜一遍标题，也不要凭标题猜内容。"
            "抓不到链接就直接说没抓到，别编。")
    try:
        parts.append(_pb)
    except Exception:
        pass
    return "\n\n".join([p for p in parts if p])


USAGE = os.path.join(STATE, "usage.json")


def track_usage(resp):
    """累计 token 用量，方便在面板上看今天花了多少。"""
    try:
        u = (resp or {}).get("usage") or {}
        p = int(u.get("prompt_tokens") or 0)
        c = int(u.get("completion_tokens") or 0)
        if not p and not c:
            return
        day = time.strftime("%Y-%m-%d")
        d = {}
        if os.path.exists(USAGE):
            try:
                with open(USAGE, encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                d = {}
        cur = d.get(day) or {"p": 0, "c": 0, "calls": 0}
        cur["p"] += p
        cur["c"] += c
        cur["calls"] += 1
        d[day] = cur
        d = {k: d[k] for k in sorted(d)[-14:]}
        os.makedirs(STATE, exist_ok=True)
        with open(USAGE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:
        pass


def _post(messages, tools):
    cfg = load_config()
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "max_tokens": cfg.get("max_tokens", 800),
        "temperature": 0.7,
    }
    if tools:
        cfg = load_config()
        allowed = [t for t in TOOLS
                   if feature_on(cfg, TOOL_FEATURE.get(
                       t["function"]["name"], ""))
                   or TOOL_FEATURE.get(t["function"]["name"]) is None]
        payload["tools"] = allowed
        payload["tool_choice"] = "auto"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % cfg["api_key"],
    })
    with urlopen(req, timeout=cfg.get("timeout", 60)) as r:
        resp = json.loads(r.read().decode("utf-8"))
    track_usage(resp)
    return resp


def _dispatch(name, args, speaker=None, source_text=None):
    import tools
    cfg = load_config()
    feat = TOOL_FEATURE.get(name)
    if feat and not feature_on(cfg, feat):
        return "「%s」这个功能在面板里被关掉了" % feat
    per_person = {"remember": tools.tool_remember,
                  "recall": tools.tool_recall,
                  "profile": tools.tool_profile}
    fn = {
        "now": tools.tool_now,
        "web_search": tools.tool_web_search,
        "web_fetch": tools.tool_web_fetch,
        "markdown": tools.tool_markdown,
        "firecrawl": tools.tool_firecrawl,
        "search_papers": tools.tool_search_papers,
        "memory_save": tools.tool_memory_save,
        "memory_search": tools.tool_memory_search,
        "qzone_list": tools.tool_qzone_list,
        "qzone_feed": tools.tool_qzone_feed,
        "qzone_like": tools.tool_qzone_like,
        "qzone_comment": tools.tool_qzone_comment,
        "qzone_post": tools.tool_qzone_post,
        "qzone_delete": tools.tool_qzone_delete,
        "weather": tools.tool_weather,
        "digest_now": tools.tool_digest_now,
        "sticker_save": tools.tool_sticker_save,
        "sticker_send": tools.tool_sticker_send,
        "sticker_reply": tools.tool_sticker_reply_last,
        "send_file": tools.tool_send_file,
    }.get(name)
    try:
        if name == "remember":
            return str(per_person[name](args, speaker, source_text))
        if name in per_person:
            return str(per_person[name](args, speaker))
        if not fn:
            return "未知工具 %s" % name
        return str(fn(args))
    except Exception as e:
        return "工具执行出错：%s" % e


def complete(prompt, system=None, max_tokens=300):
    """不带工具的单次补全，用于生成短文本。"""
    cfg = load_config()
    if not cfg.get("api_key"):
        return False, "未配置 api_key"
    msgs = [{"role": "system", "content": system or persona_text()},
            {"role": "user", "content": prompt}]
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    body = json.dumps({"model": cfg["model"], "messages": msgs,
                       "max_tokens": max_tokens, "temperature": 0.8},
                      ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % cfg["api_key"]})
    try:
        with urlopen(req, timeout=cfg.get("timeout", 60)) as r:
            j = json.loads(r.read().decode("utf-8"))
        track_usage(j)
        return True, (j["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        return False, "调用失败: %s" % e


def chat_with_tools(user_text, context, history=None, max_rounds=6, verbose=None,
                    speaker=None, source_text=None):
    """返回 (ok, reply, trace)。trace 是工具调用记录，用于日志。"""
    cfg = load_config()
    if not cfg.get("api_key"):
        return False, "未配置 api_key", []

    msgs = [{"role": "system", "content": persona_text()},
            {"role": "system", "content": context}]
    for role, content in (history or [])[-6:]:
        msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": user_text})

    trace = []
    for _ in range(max_rounds):
        try:
            resp = _post(msgs, tools=True)
        except Exception as e:
            return False, "调用失败: %s" % e, trace

        try:
            msg = resp["choices"][0]["message"]
        except Exception:
            return False, "返回异常: %s" % str(resp)[:200], trace

        calls = msg.get("tool_calls") or []
        if not calls:
            return True, (msg.get("content") or "").strip(), trace

        msgs.append({"role": "assistant", "content": msg.get("content") or "",
                     "tool_calls": calls})
        for tc in calls:
            fn = (tc.get("function") or {}).get("name")
            raw_args = (tc.get("function") or {}).get("arguments") or "{}"
            try:
                args = json.loads(raw_args)
            except Exception:
                args = {}
            out = _dispatch(fn, args or {}, speaker, source_text)
            trace.append({"tool": fn, "args": args, "out": out[:300]})
            if verbose:
                verbose("工具 %s -> %s" % (fn, str(out)[:120]))
            msgs.append({"role": "tool", "tool_call_id": tc.get("id"),
                         "content": out})
        # 紧跟在工具结果后面加一条提醒，防止模型只回工具那件事
        msgs.append({"role": "system", "content":
                     "现在写最终回复。必须按顺序把用户问的每一件事都真正答出来，"
                     "每件事单独写一两句。"
                     "如果你不知道（比如没有相关记录），就明确写「这个我没记过/不知道」。"
                     "禁止说「上面都答了」「前面说了」这类空话——要把答案内容本身写出来。"
                     "不许只回工具相关的那一句。"})

    # 轮次用完：不带工具再问一次，让它用已经拿到的信息作答，而不是摆烂
    try:
        resp = _post(msgs, tools=False)
        msg = resp["choices"][0]["message"]
        final = (msg.get("content") or "").strip()
        if final:
            return True, final, trace
    except Exception as e:
        return True, "（查得不太全：%s）" % e, trace
    return True, "（这条没查到，你换个说法我再试）", trace


def vision_describe(img_path, prompt=None):
    """用视觉模型描述图片，返回文字。未配置 Key / 调用失败返回 None。"""
    cfg = load_config()
    v = cfg.get("vision") or {}
    base = (v.get("base_url") or "").strip()
    model = (v.get("model") or "").strip()
    key = (v.get("api_key") or "").strip()
    if not (base and model and key):
        return None
    if not os.path.isfile(img_path):
        return None
    import base64 as _b64
    try:
        with open(img_path, "rb") as f:
            b64 = _b64.b64encode(f.read()).decode()
        ext = os.path.splitext(img_path)[1].lstrip(".").lower()
        mime = "jpeg" if ext in ("jpg", "jpeg") else ext
        if mime not in ("png", "jpeg", "webp", "gif"):
            mime = "png"
        data_url = "data:image/%s;base64,%s" % (mime, b64)
    except Exception:
        return None
    if not prompt:
        prompt = "用中文简要描述这张图片的内容；如果图里有文字，请原样说出上面的文字。"
    body = {"model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]}],
            "max_tokens": 500}
    url = base.rstrip("/") + "/chat/completions"
    try:
        req = Request(url, data=json.dumps(body).encode("utf-8"),
                      headers={"Content-Type": "application/json",
                               "Authorization": "Bearer " + key})
        with urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode("utf-8"))
        txt = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return txt.strip() or None
    except Exception:
        return None
