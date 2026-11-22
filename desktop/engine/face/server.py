#!/usr/bin/env python3
"""脸的小服务：同一份 state 既给画面看，也给云台动。

  python3 app/engine/face/server.py --port 8710
  # 然后 Chrome 打开 http://127.0.0.1:8710/
  # 别处可以 POST /set {"mood":"angry","talk":1,"look":-1}
"""
from __future__ import annotations
import argparse, json, os, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = {"mood": "idle", "talk": 0, "look": 0, "at": 0}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/state"):
            return self._json(STATE)
        p = self.path.split("?")[0]
        if p in ("/", "/index.html", "/face.html"):
            try:
                b = open(os.path.join(HERE, "face.html"), "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
                return
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self.path.startswith("/set"):
            return self._json({"error": "not found"}, 404)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            return self._json({"error": "bad json"}, 400)
        for k in ("mood", "talk", "look"):
            if k in body:
                STATE[k] = body[k]
        STATE["at"] = time.time()
        return self._json(STATE)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8710)
    a = ap.parse_args()
    print(f"脸：http://127.0.0.1:{a.port}/")
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
