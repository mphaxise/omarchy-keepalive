"""Threaded Unix-socket fake Herdr server for tests.

Answers exactly the wire protocol HerdrClient assumes (and marks VERIFY ON
RIG) in omarchy-agent-session-core: one JSON object per line in,
{"id", "result"} or {"id", "error": {...}} back, newline-delimited.

Not a test module itself (no Test* classes), so `unittest discover` does not
collect it; it is imported by test_core.py.
"""

import contextlib
import json
import socket
import threading


class FakeHerdrServer:
    def __init__(self, socket_path):
        self.socket_path = str(socket_path)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.socket_path)
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = None
        self.lock = threading.Lock()
        self.received = []  # list of (method, params)
        self.results = {}   # method -> value, or callable(params) -> value
        self.errors = {}    # method -> (code, message)

    def start(self):
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        with contextlib.suppress(OSError):
            self._sock.close()

    def set_result(self, method, value):
        self.results[method] = value

    def set_error(self, method, code, message):
        self.errors[method] = (code, message)

    def calls(self, method):
        with self.lock:
            return [p for m, p in self.received if m == method]

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            f = conn.makefile("r", encoding="utf-8")
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                except json.JSONDecodeError:
                    continue
                method = req.get("method")
                params = req.get("params") or {}
                with self.lock:
                    self.received.append((method, params))
                if method in self.errors:
                    code, message = self.errors[method]
                    reply = {"id": req.get("id"), "error": {"code": code, "message": message}}
                else:
                    result = self.results.get(method, {})
                    if callable(result):
                        result = result(params)
                    reply = {"id": req.get("id"), "result": result}
                with contextlib.suppress(OSError):
                    conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                conn.close()
