# QQ AI 聊天机器人

跑在 Linux 服务器上的 QQ 机器人，接 NapCat（OneBot v11），支持私聊、群聊、自定义人设、长期记忆和一堆日常功能。

---

## 界面预览

网页控制台（`digest/dashboard.py` + `panel.html`），可以开关功能、看状态：

![控制台](docs/panel-home.jpg)

插件页：

![插件](docs/panel-plugins.jpg)

---

## 功能

- **早报** — 每天定时抓最新资讯，推送到指定 QQ
- **观看日报** — 抓抖音、B 站的观看记录，生成每天的专属观看日报
- **占卜** — 塔罗、周易，带每日运势
- **空间互动** — 看好友动态、点赞、评论、发说说
- **名片点赞** — 定时给好友名片点赞
- **表情包** — 自动存图，按对话氛围挑图回复
- **文件发送** — 下载网上的文件直接发出去
- **自动画像** — 记住每个人的称呼、身份、偏好
- **关心问候** — 按设定时间主动问候
- **联网搜索** — 需要最新信息时联网查

---

## 环境要求

- Linux（Ubuntu 22.04 实测），Python 3.10+
- Docker + NapCat（OneBot v11）
- 一个 LLM 接口（DeepSeek 或任意 OpenAI 兼容）

---

## 快速开始

```bash
git clone <repo-url> qq-ai-bot
cd qq-ai-bot
chmod +x setup.sh
./setup.sh
```

`setup.sh` 会自动探测机器配置（系统/架构/CPU/内存/磁盘/时区/Python），生成 `machine.env` 和 `digest/llm.json`，并检查依赖。

编辑 `digest/llm.json`，至少填这几项：

```json
{
  "api_key": "你的 LLM Key",
  "persona": {
    "nicknames": "机器人昵称",
    "style": "人设，随便写"
  },
  "digest_recipients": [接收日报的QQ号],
  "news_recipients": [接收早报的QQ号]
}
```

启动：

```bash
python3 digest/bot_server.py
```

---

## 使用教程

**1. 起 NapCat**

```bash
docker run -d --name napcat \
  -e NAPCAT_GID=0 \
  -p 3000:3000 -p 3002:3002 \
  -v /opt/napcat/config:/app/napcat/config \
  mlikiowa/napcat-docker:latest
```

扫容器里的二维码登录 QQ（`docker logs napcat` 里会有链接或图片）。

**2. 让 NapCat 连机器人**

NapCat 后台 → 网络配置 → 新建「反向 WebSocket」：

- 地址：`ws://127.0.0.1:3002`
- 消息格式：`array`

**3. 起机器人**

```bash
python3 digest/bot_server.py
```

看到 `ws connected` 就通了。

**4. 用**

- 私聊机器人直接说话
- 群里 @ 它才回
- 发「日报」看今天的 B 站/抖音观看报告
- 发「塔罗」「周易」占卜
- 引用一张表情包再发「文件」，能拿到那张的原图
- 网页控制台：`python3 digest/dashboard.py`，浏览器打开 `http://服务器IP:8080`，可以开关功能、看状态

**5. 开机自启**

```bash
sudo cp systemd/qqbot.service.example /etc/systemd/system/qqbot.service
sudo systemctl enable --now qqbot
```

---

## 配置说明（`digest/llm.json`）

| 字段 | 说明 |
|---|---|
| `api_key` / `base_url` / `model` | LLM 接口 |
| `persona.nicknames` / `persona.style` | 昵称 + 人设 |
| `digest_recipients` | 接收日报的 QQ |
| `news_recipients` | 接收早报的 QQ |
| `news_time` / `digest_time` | 早报 / 日报推送时间 |
| `features.*` | 各功能开关 |
| `max_tokens` / `timeout` | 生成参数 |

---

## 目录结构

```
.
├── README.md
├── setup.sh                 自动探测设备配置 + 初始化
├── machine.env.example
├── digest/
│   ├── bot_server.py        主机器人（HTTP 3001 / 反向 WS 3002）
│   ├── llm_chat.py          人设 + 工具调用
│   ├── tools.py             工具集
│   ├── memory_store.py      长期记忆
│   ├── digest_core.py       日报
│   ├── news_brief.py        早报
│   ├── divination.py        占卜
│   ├── zodiac.py            星座
│   ├── vision.py            识图
│   ├── call.py              APK / 二维码
│   ├── dashboard.py         网页控制台后端
│   ├── panel.html           网页控制台前端
│   ├── accounts.py          多号管理
│   ├── monitor.py           进程守护
│   ├── push.py              主动推送
│   ├── run_digest.py        日报任务
│   ├── like_card.py         名片点赞
│   ├── ssh_guard.py         SSH 白名单
│   ├── llm.example.json
│   └── requirements.txt
└── systemd/
    └── qqbot.service.example
```

---

## 许可

MIT。
