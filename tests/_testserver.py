# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Ali Akbar Mahbadi and dlus contributors
"""A small local file server for the tests: byte ranges, ETag, Content-Disposition, throttling and injected failures."""
import http.server
import os
import re
import threading
import time
import urllib.parse


class FileServer:
    def __init__(self, root):
        self.root = root
        self.ranges = True  # answer Range requests with 206
        self.delay = 0.0  # seconds to wait per 64 KB (slow server)
        self.fail_ranges = 0  # this many range requests answer 500 first
        self.corrupt_big = 0  # this many large range answers arrive damaged (small check samples stay correct)
        self.names = {}  # url path -> Content-Disposition file name
        self.hits = []
        srv = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_HEAD(self):
                self.serve(head=True)

            def do_GET(self):
                self.serve(head=False)

            def serve(self, head):
                path = self.path.split("?")[0]
                srv.hits.append((self.command, path, self.headers.get("Range", "")))
                f = os.path.join(srv.root, path.lstrip("/"))
                if not os.path.isfile(f):
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                data = open(f, "rb").read()
                L = len(data)
                rng = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
                if rng and srv.ranges:
                    if srv.fail_ranges > 0:
                        srv.fail_ranges -= 1
                        self.send_response(500)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    a = int(rng.group(1))
                    b = min(int(rng.group(2)) if rng.group(2) else L - 1, L - 1)
                    self.send_response(206)
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (a, b, L))
                else:
                    a, b = 0, L - 1
                    self.send_response(200)
                self.send_header("Content-Length", str(b - a + 1))
                self.send_header("ETag", '"%x-%x"' % (L, int(os.path.getmtime(f))))
                self.send_header("Accept-Ranges", "bytes" if srv.ranges else "none")
                if path in srv.names:
                    self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(srv.names[path]))
                self.end_headers()
                if head:
                    return
                if rng and b - a > 200000 and srv.corrupt_big > 0:
                    srv.corrupt_big -= 1
                    data = data[:a + 1000] + bytes(x ^ 0xFF for x in data[a + 1000:a + 2000]) + data[a + 2000:]
                try:
                    i = a
                    while i <= b:
                        j = min(b + 1, i + 65536)
                        self.wfile.write(data[i:j])
                        i = j
                        if srv.delay:
                            time.sleep(srv.delay)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        class S(http.server.ThreadingHTTPServer):
            daemon_threads = True

        self.srv = S(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def url(self, name):
        return "http://127.0.0.1:%d/%s" % (self.port, name)

    def put(self, name, data):
        p = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def stop(self):
        self.srv.shutdown()
