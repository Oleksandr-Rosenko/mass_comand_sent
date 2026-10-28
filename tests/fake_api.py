"""Фейковий API M2M для тестів і локальної демонстрації: група генераторів, команди, історія результатів.

Запуск для ручної перевірки інтерфейсу:  python -m tests.fake_api  (слухає http://127.0.0.1:8765,
логін — будь-який email, пароль «demo123»).
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class FakeM2M:
    def __init__(self, n_devices=534, no_create=(), no_execute=(), have_cmd=(), other_text=(), silent=(),
                 flaky_create=(), uncertain_create=(), dispatch_403=(), ascending=False, answer_delay=0.2,
                 name="NTP", text="runcmd:@com_tsync_disallow:1", expire_token_after=None):
        self.lock = threading.Lock()
        self.devices = {}
        for i in range(1, n_devices + 1):
            age = timedelta(days=3) if i % 9 == 0 else timedelta(hours=5) if i % 5 == 0 else timedelta(minutes=2)
            self.devices[i] = {
                "id": i, "name": f"Генератор {i:03d}", "uniqueId": f"8600000000{i:05d}",
                "lastMessageDate": (datetime.now(timezone.utc) - age).isoformat(),
                "permissions": {"CREATE_COMMANDS": i not in no_create, "EXECUTE_COMMANDS": i not in no_execute},
            }
        self.commands = {i: [{"id": 1000 + i, "description": "01. GETVER", "type": "CUSTOM",
                              "attributes": {"data": "getver"}}] for i in self.devices}
        self.next_cmd = 100000
        for i in have_cmd:
            self._add(i, name, text)
        for i in other_text:
            self._add(i, name, text + "_OLD")
        self.silent, self.flaky, self.uncertain = set(silent), set(flaky_create), set(uncertain_create)
        self.dispatch_403 = set(dispatch_403)
        self.ascending, self.answer_delay = ascending, answer_delay
        self.history = [{"id": i, "type": "COMMAND_RESULT", "deviceId": 1, "deviceName": "Генератор 001",
                         "message": "старий результат", "createdAt": "2026-10-01T10:00:00Z"} for i in range(1, 251)]
        self.next_hist = 251
        self.dispatched: list[tuple[int, int]] = []
        self.create_calls: dict[int, int] = {}
        self.active = 0
        self.max_active = 0
        self.requests = 0
        self.flaky_hits: set[int] = set()
        self.token_n = 0
        self.expire_token_after = expire_token_after
        self.token_uses = 0

    def _add(self, did, name, text):
        self.next_cmd += 1
        c = {"id": self.next_cmd, "description": name, "type": "CUSTOM", "attributes": {"data": text}}
        self.commands[did].append(c)
        return c

    def _answer_later(self, did):
        def run():
            time.sleep(self.answer_delay)
            with self.lock:
                self.history.append({
                    "id": self.next_hist, "type": "COMMAND_RESULT", "deviceId": did,
                    "deviceName": self.devices[did]["name"],
                    "message": "TSYNC is now allowed from NTP NITZ",
                    "metadata": json.dumps({"deviceId": did, "positionId": 5}),
                    "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                })
                self.next_hist += 1
        threading.Thread(target=run, daemon=True).start()

    # ---------------------------------------------------------------- HTTP
    def handle(self, method, path, query, body, auth):
        if path == "/api/login":
            if body.get("password") != "demo123":
                return 401, {"message": "Bad credentials"}
            self.token_n += 1
            self.token_uses = 0
            return 200, {"token": f"tok{self.token_n}", "refreshToken": "ref", "user": {"id": 1}}
        if path == "/api/token/refresh":
            self.token_n += 1
            self.token_uses = 0
            return 200, {"token": f"tok{self.token_n}", "refreshToken": "ref"}
        if auth != f"Bearer tok{self.token_n}":
            return 401, {"message": "Unauthorized"}
        self.token_uses += 1
        if self.expire_token_after and self.token_uses > self.expire_token_after:
            self.token_n += 1  # токен «протух»
            return 401, {"message": "Token expired"}

        if path == "/api/device/groups/lightweight/all":
            return 200, [{"id": 7, "name": "Генератори", "deviceIds": list(self.devices)},
                         {"id": 8, "name": "Тест", "deviceIds": [1, 2]}]
        m = re.fullmatch(r"/api/group/(\d+)/devices", path)
        if m:
            ids = list(self.devices) if m.group(1) == "7" else [1, 2]
            return 200, [self.devices[i] for i in ids]
        m = re.fullmatch(r"/api/device/(\d+)/commands", path)
        if m:
            did = int(m.group(1))
            if method == "GET":
                return 200, list(self.commands[did])
            with self.lock:
                self.create_calls[did] = self.create_calls.get(did, 0) + 1
                if did in self.flaky and did not in self.flaky_hits:
                    self.flaky_hits.add(did)
                    return 503, {"message": "Service Unavailable"}
                if not self.devices[did]["permissions"]["CREATE_COMMANDS"]:
                    return 403, {"message": "Access denied"}
                c = self._add(did, body["description"], body["attributes"]["data"])
                if did in self.uncertain and did not in self.flaky_hits:
                    self.flaky_hits.add(did)
                    return 500, {"message": "Internal error"}  # створилось, але сервер відповів помилкою
                return 200, c
        m = re.fullmatch(r"/api/device/(\d+)/command/(\d+)/dispatch", path)
        if m:
            did, cid = int(m.group(1)), int(m.group(2))
            if did in self.dispatch_403:
                return 403, {"message": "Access denied"}
            if not any(c["id"] == cid for c in self.commands[did]):
                return 404, {"message": "Command not found"}
            with self.lock:
                self.dispatched.append((did, cid))
            if did not in self.silent:
                self._answer_later(did)
            return 200, None
        if path == "/api/notification/history":
            per = int(query.get("per_page", ["10"])[0])
            page = int(query.get("page", ["1"])[0])
            with self.lock:
                items = sorted(self.history, key=lambda x: x["id"], reverse=not self.ascending)
            pages = max(1, -(-len(items) // per))
            return 200, {"pages": pages, "page": page, "total": len(items),
                         "items": items[(page - 1) * per: page * per]}
        return 404, {"message": f"no route {path}"}

    def serve(self, port=0) -> tuple[ThreadingHTTPServer, str]:
        api = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self, method):
                u = urlparse(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                with api.lock:
                    api.active += 1
                    api.requests += 1
                    api.max_active = max(api.max_active, api.active)
                try:
                    time.sleep(0.002)
                    code, data = api.handle(method, u.path, parse_qs(u.query), body, self.headers.get("Authorization"))
                finally:
                    with api.lock:
                        api.active -= 1
                raw = b"" if data is None else json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._do("GET")

            def do_POST(self):
                self._do("POST")

        srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def demo() -> FakeM2M:
    """Набір як на мокапі: 534 генератори, 13 без права створення, 16 уже з «NTP», 3 з іншим текстом."""
    return FakeM2M(
        no_create=range(4, 534, 41), have_cmd=range(3, 534, 33), other_text=(44, 144, 244),
        silent=set(range(9, 534, 9)) | {5, 70, 205}, dispatch_403=(17, 117), flaky_create=range(10, 534, 50),
        uncertain_create=(25, 125), answer_delay=1.5,
    )


if __name__ == "__main__":
    srv, url = demo().serve(8765)
    print(f"Фейковий API: {url}  (пароль demo123)")
    threading.Event().wait()
