"""QQ 桥：接 OneBot（NapCat / LLOneBot）的 WebSocket，私聊收发。

只做三件事：连、白名单过滤、把消息交给内核再回出去。
不处理群、不处理图片/语音（先只认文字），断线自动重连。
"""
from __future__ import annotations
import json, os, socket, struct, threading, time

UA = "Aster-QQBridge/1.0"


class Bridge:
    def __init__(self, url: str, whitelist: list, on_msg, log=print):
        self.url = url
        self.wl = [str(x).strip() for x in (whitelist or []) if str(x).strip()]
        self.on_msg = on_msg
        self.log = log
        self.sock = None
        self.alive = False
        self.last_error = ""
        self.self_id = ""

    # ---------- WebSocket 客户端（手写，够用） ----------
    def _handshake(self) -> None:
        host, port, path = self._parse(self.url)
        s = socket.create_connection((host, port), timeout=20)
        import base64
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
               f"Sec-WebSocket-Version: 13\r\nUser-Agent: {UA}\r\n\r\n")
        s.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                raise RuntimeError("握手失败")
            buf += chunk
        if b" 101 " not in buf.split(b"\r\n")[0] + b" ":
            raise RuntimeError("没升级成 WebSocket：" + buf.split(b"\r\n")[0].decode("latin1"))
        self.sock = s
        self.sock.settimeout(60)

    @staticmethod
    def _parse(url: str):
        u = url.replace("ws://", "").replace("wss://", "")
        hostport, _, path = u.partition("/")
        host, _, port = hostport.partition(":")
        return host or "127.0.0.1", int(port or 3001), "/" + path if path else "/"

    def _send_text(self, text: str) -> None:
        data = text.encode()
        mask = os.urandom(4)
        n = len(data)
        head = b"\x81"
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read_frame(self) -> bytes:
        hdr = self.sock.recv(2)
        if len(hdr) < 2:
            raise ConnectionError("连接断了")
        op = hdr[0] & 0x0F
        n = hdr[1] & 0x7F
        if n == 126:
            n = struct.unpack(">H", self.sock.recv(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self.sock.recv(8))[0]
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("连接断了")
            data += chunk
        if op == 0x9:          # ping → 回 pong
            return b""
        return data

    # ---------- 收发 ----------
    def _call(self, action: str, params: dict) -> None:
        try:
            self._send_text(json.dumps({"action": action, "params": params}, ensure_ascii=False))
        except Exception as e:
            self.last_error = str(e)[:80]

    def reply_private(self, user_id: str, text: str) -> None:
        self._call("send_private_msg", {"user_id": int(user_id), "message": text[:1500]})

    def loop(self) -> None:
        while True:
            try:
                self._handshake()
                self.alive = True
                self.last_error = ""
                self.log("qq bridge 连上了：" + self.url)
                while True:
                    raw = self._read_frame()
                    if not raw:
                        continue
                    try:
                        ev = json.loads(raw.decode("utf-8", "replace"))
                    except Exception:
                        continue
                    self._handle(ev)
            except Exception as e:
                self.alive = False
                self.last_error = str(e)[:120]
                self.log("qq bridge 断开：" + self.last_error + "，5 秒后重连")
                time.sleep(5)

    def _handle(self, ev: dict) -> None:
        if ev.get("post_type") != "message":
            return
        if ev.get("message_type") != "private":
            return
        uid = str(ev.get("user_id") or "")
        if self.wl and uid not in self.wl:
            return
        text = ""
        for seg in (ev.get("message") or []):
            if seg.get("type") == "text":
                text += str((seg.get("data") or {}).get("text") or "")
        text = text.strip()
        if not text:
            return
        try:
            out = self.on_msg(text, uid)
        except Exception as e:
            out = "出错了：" + str(e)[:60]
        if out:
            self.reply_private(uid, out)


def start(url: str, whitelist: list, on_msg, log=print) -> Bridge:
    b = Bridge(url, whitelist, on_msg, log)
    threading.Thread(target=b.loop, daemon=True).start()
    return b
