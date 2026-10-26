# -*- coding: utf-8 -*-
"""网页版"视频通话"：播放预置视频，播到剩 2 秒自动挂断。

- /c/<token>  通话页（token 一次性，默认 10 分钟过期）
- /v/<name>   视频文件（支持 Range，手机浏览器必需）
- /api/end/<token>  挂断上报
"""
import json
import os
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state")
VDIR = os.path.join(STATE, "video")
TOKENS = os.path.join(STATE, "calls.json")
CFG = os.path.join(HERE, "llm.json")
PORT = int(os.environ.get("CALL_PORT") or 8081)

TAROT_END = 2.0  # 提前多少秒挂断


def cfg():
    try:
        with open(CFG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def videos():
    if not os.path.isdir(VDIR):
        return []
    out = []
    for n in sorted(os.listdir(VDIR)):
        if n.lower().endswith((".mp4", ".webm", ".mov", ".m4v")):
            out.append(n)
    return out


def load_tokens():
    try:
        with open(TOKENS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_tokens(d):
    os.makedirs(STATE, exist_ok=True)
    with open(TOKENS, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)


def new_token(qq=None, video=None, ttl=600):
    d = load_tokens()
    now = time.time()
    d = {k: v for k, v in d.items() if v.get("exp", 0) > now}
    tok = secrets.token_urlsafe(18)
    vs = videos()
    if not video or video not in vs:
        video = vs[0] if vs else ""
    d[tok] = {"qq": str(qq or ""), "video": video,
              "exp": now + ttl, "used": 0, "created": now}
    save_tokens(d)
    base = (cfg().get("call_base") or "").rstrip("/")
    return tok, ("%s/c/%s" % (base, tok) if base else "/c/%s" % tok)


PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>视频通话</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0b0d12;color:#fff;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
 overflow:hidden;height:100vh}
video{position:fixed;inset:0;width:100%;height:100%;object-fit:cover;background:#000}
.bar{position:fixed;left:0;right:0;top:0;padding:22px 20px;display:flex;align-items:center;gap:12px;
 background:linear-gradient(180deg,rgba(0,0,0,.55),transparent);z-index:5}
.av{width:44px;height:44px;border-radius:50%;background:#4c6fff;display:grid;place-items:center;
 font-weight:700;font-size:16px}
.nm{font-size:16px;font-weight:600}
.st{font-size:12px;color:#c9d1e6;margin-top:2px}
.tm{margin-left:auto;font-variant-numeric:tabular-nums;font-size:15px;color:#dfe5f5}
.ctr{position:fixed;left:0;right:0;bottom:38px;display:flex;justify-content:center;gap:28px;z-index:5}
.btn{width:64px;height:64px;border-radius:50%;border:0;display:grid;place-items:center;
 font-size:22px;cursor:pointer;background:rgba(255,255,255,.16);color:#fff;backdrop-filter:blur(8px)}
.btn.hang{background:#ef4444}
.over{position:fixed;inset:0;background:rgba(8,10,16,.92);display:none;place-items:center;z-index:9;
 flex-direction:column;gap:14px}
.over.on{display:grid}
.over .t{font-size:20px;font-weight:700}
.over .s{font-size:13px;color:#9aa4bf}
.hint{position:fixed;left:0;right:0;bottom:8px;text-align:center;font-size:11px;color:#5f6780;z-index:5}
</style></head><body>
<video id="v" playsinline preload="auto" src="/v/__VIDEO__"></video>
<div class="bar">
  <div class="av">云</div>
  <div><div class="nm">助手</div><div class="st" id="st">视频通话中…</div></div>
  <div class="tm" id="tm">00:00</div>
</div>
<div class="ctr">
  <button class="btn" onclick="toggleMute()" id="mb">🎤</button>
  <button class="btn hang" onclick="hangup(true)">📵</button>
</div>
<div class="hint">播放结束前 2 秒自动挂断</div>
<div class="over" id="ov">
  <div class="t">通话已结束</div>
  <div class="s" id="os">时长 00:00</div>
  <div class="s">—— 助手 ——</div>
</div>
<script>
var v=document.getElementById('v'), tm=document.getElementById('tm'),
    st=document.getElementById('st'), ov=document.getElementById('ov'),
    os=document.getElementById('os'), done=false, t0=Date.now();
function fmt(s){s=Math.max(0,Math.floor(s));var m=Math.floor(s/60);
  return (m<10?'0':'')+m+':'+((s%60)<10?'0':'')+(s%60);}
function tick(){ if(done) return; tm.textContent=fmt((Date.now()-t0)/1000); }
setInterval(tick, 500);
function endCall(){
  if(done) return; done=true;
  try{ v.pause(); }catch(e){}
  st.textContent='已挂断';
  os.textContent='时长 '+fmt((Date.now()-t0)/1000);
  ov.classList.add('on');
  try{ fetch('/api/end/__TOKEN__'); }catch(e){}
}
function hangup(manual){ endCall(); }
function toggleMute(){
  v.muted=!v.muted;
  document.getElementById('mb').textContent = v.muted ? '🔇' : '🎤';
}
v.addEventListener('loadedmetadata', function(){
  st.textContent='视频通话中…';
  v.play().catch(function(){
    // 有些浏览器禁止自动播放，等用户点一下
    st.textContent='点一下屏幕接听';
    document.body.addEventListener('click', function once(){
      v.play(); st.textContent='视频通话中…';
      document.body.removeEventListener('click', once);
    }, {once:true});
  });
});
// 核心：播到剩 2 秒就挂断，避免露馅
v.addEventListener('timeupdate', function(){
  if(done || !v.duration || !isFinite(v.duration)) return;
  if(v.duration - v.currentTime <= 2.0){ endCall(); }
});
v.addEventListener('ended', endCall);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    server_version = "call/1.0"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        import urllib.request as _u
        import urllib.parse as _up
        path = _up.urlparse(self.path).path
        if not (path.startswith("/api/") or path.startswith("/app")):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        target = "http://127.0.0.1:3005" + path
        q = _up.urlparse(self.path).query
        if q:
            target += "?" + q
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            req = _u.Request(target, data=body, method="POST",
                             headers={"Content-Type": self.headers.get("Content-Type")
                                      or "application/json"})
            with _u.urlopen(req, timeout=60) as r:
                data = r.read()
                ctype = r.headers.get("Content-Type", "application/json")
            return self._send(200, data, ctype)
        except Exception as e:
            return self._send(502, "转发失败: %s" % e, "text/plain; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/call", "/call/"):
            # 固定入口：打开就接通（自动发一个一次性 token 再跳转）
            tok, _ = new_token(qq="", ttl=120)
            self.send_response(302)
            self.send_header("Location", "/c/" + tok)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if path.startswith("/c/"):
            tok = path[3:]
            d = load_tokens()
            rec = d.get(tok)
            if not rec or rec.get("exp", 0) < time.time():
                return self._send(410, "<h2 style='font-family:sans-serif'>这个通话链接已过期</h2>")
            rec["used"] = rec.get("used", 0) + 1
            d[tok] = rec
            save_tokens(d)
            name = rec.get("video") or (videos()[0] if videos() else "")
            return self._send(200, PAGE.replace("__VIDEO__", name).replace("__TOKEN__", tok))
        if path == "/app" or path.startswith("/app/") or path.startswith("/api/"):
            # 把网页版（聊天+看板）反代出去
            import urllib.request as _u
            import urllib.parse as _up
            target = "http://127.0.0.1:3005" + path
            if path == "/app":
                target = "http://127.0.0.1:3005/app/"
            q = _up.urlparse(self.path).query
            if q:
                target += "?" + q
            try:
                body = None
                if self.command == "POST":
                    n = int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(n) if n else b""
                req = _u.Request(target, data=body, method=self.command,
                                 headers={"Content-Type": self.headers.get("Content-Type") or "application/json"})
                with _u.urlopen(req, timeout=60) as r:
                    data = r.read()
                    ctype = r.headers.get("Content-Type", "text/html; charset=utf-8")
                return self._send(200, data, ctype)
            except Exception as e:
                return self._send(502, "后端转发失败: %s" % e, "text/plain; charset=utf-8")
        if path in ("/apk", "/Edgechat.apk"):
            fp = os.path.join(STATE, "qr", "Edgechat.apk")
            if not os.path.isfile(fp):
                return self._send(404, "apk not found", "text/plain; charset=utf-8")
            with open(fp, "rb") as f:
                data = f.read()
            return self._send(200, data, "application/vnd.android.package-archive")
        if path.startswith("/img/"):
            name = os.path.basename(path[5:])
            # 二维码类：每次请求都实时从容器取最新码，零延迟（否则静态副本最多晚 1 分钟，码会失效）
            qr_map = {"main.png": "napcat", "dot.png": "napcat-dot", "2bx.png": "napcat-2bx"}
            if name in qr_map:
                import subprocess
                tmp = "/tmp/_liveqr_%s_%d" % (name, int(time.time() * 1000))
                try:
                    subprocess.run(
                        ["sudo", "docker", "cp",
                         "%s:/app/napcat/cache/qrcode.png" % qr_map[name], tmp],
                        capture_output=True, timeout=10)
                    if os.path.isfile(tmp):
                        with open(tmp, "rb") as f:
                            data = f.read()
                        try:
                            os.remove(tmp)
                        except Exception:
                            pass
                        return self._send(200, data, "image/png")
                except Exception:
                    pass
            fp = os.path.join(STATE, "qr", name)
            if not os.path.isfile(fp):
                return self._send(404, "not found", "text/plain; charset=utf-8")
            with open(fp, "rb") as f:
                data = f.read()
            return self._send(200, data, "image/png")
        if path.startswith("/v/"):
            name = os.path.basename(path[3:])
            fp = os.path.join(VDIR, name)
            if not os.path.isfile(fp):
                return self._send(404, "not found")
            return self._serve_file(fp)
        if path.startswith("/api/end/"):
            return self._send(200, json.dumps({"ok": True}))
        if path == "/ping":
            return self._send(200, json.dumps({"ok": True, "videos": videos()},
                                              ensure_ascii=False), "application/json")
        return self._send(404, "not found")

    def _serve_file(self, fp):
        size = os.path.getsize(fp)
        ctype = "video/mp4"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            try:
                s, _, e = rng[6:].partition("-")
                if s:
                    start = int(s)
                if e:
                    end = int(e)
                start = max(0, min(start, size - 1))
                end = max(start, min(end, size - 1))
            except Exception:
                start, end = 0, size - 1
        length = end - start + 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if rng:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        with open(fp, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(65536, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except Exception:
                    break
                left -= len(chunk)


def main():
    os.makedirs(VDIR, exist_ok=True)
    print("call server on :%d, videos=%s" % (PORT, videos()))
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
