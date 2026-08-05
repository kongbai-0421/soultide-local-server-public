"""灵魂潮汐 HTTP 抓包代理脚本
用法: python capture_proxy.py [port]
代理会自动记录所有 HTTP 请求/响应到 proxy_capture.log
"""
import http.server, urllib.request, sys, os, json, base64, time

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy_capture.log")

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_PUT(self): self._handle("PUT")

    def _handle(self, method):
        cl = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(cl) if cl > 0 else b""

        # Try to decode body
        body_str = body.decode("utf-8", errors="replace")[:4096]

        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}")
            f.write(f"\n[{time.strftime('%H:%M:%S')}] [{method}] {self.path}")
            f.write(f"\nHost: {self.headers.get('Host', '')}")
            f.write(f"\nHeaders: {dict(self.headers)}")
            if body:
                f.write(f"\nBody ({len(body)} bytes):")
                # Try to decode base64 fields
                for part in body_str.split("&"):
                    for prefix in ["data=", "sign="]:
                        if part.startswith(prefix):
                            val = part[len(prefix):]
                            f.write(f"\n  {prefix}{val}")
                            if prefix == "data=" and len(val) > 20:
                                try:
                                    decoded = base64.b64decode(urllib.parse.unquote(val)).decode("utf-8")
                                    f.write(f"\n  -> decoded: {decoded[:2000]}")
                                except:
                                    f.write(f"\n  -> (base64 decode failed)")
                if body_str:
                    f.write(f"\n  raw: {body_str[:2000]}")

        # Forward to actual server
        url = self.path
        req = urllib.request.Request(url, data=body or None, headers=dict(self.headers), method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            rb = resp.read()
            resp_str = rb.decode("utf-8", errors="replace")[:4096]
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"\nResponse ({resp.status}, {len(rb)} bytes): {resp_str}\n")
            self.send_response(resp.status)
            for k, v in resp.headers.items():
                if k.lower() not in ("transfer-encoding",):
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(rb)
        except Exception as e:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"\nForward error: {e}\n")
            self.send_response(502)
            self.end_headers()
            self.wfile.write(b'{"error":"proxy error"}')

    def log_message(self, fmt, *args): pass

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8890
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(f"HTTP Capture Proxy started on port {port}\n")
        f.write(f"Target: 灵魂潮汐 (Soul Tide) Login Protocol Capture\n")
        f.write(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    s = http.server.HTTPServer(("0.0.0.0", port), H)
    print(f"Proxy running on port {port}. Log: {LOG_FILE}")
    try:
        s.serve_forever()
    except KeyboardInterrupt:
        s.shutdown()
