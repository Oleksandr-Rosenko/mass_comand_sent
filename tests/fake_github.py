"""Мінімальний фейковий GitHub API (git data + contents) для тестів збереження статистики."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = "good-token"


def _sha(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class FakeGitHub:
    def __init__(self, repo="owner/repo"):
        self.repo = repo
        self.lock = threading.Lock()
        self.trees: dict[str, dict[str, str]] = {}
        self.commits: dict[str, dict] = {}
        self.refs: dict[str, str] = {}
        self.log: list[tuple[str, str]] = []
        self.race_once = False  # перед наступним PATCH «інший користувач» встигне закомітити
        main_tree = {"app.py": "print('app')\n"}
        t = _sha(main_tree)
        self.trees[t] = main_tree
        c = _sha({"tree": t, "parents": [], "m": "init"})
        self.commits[c] = {"tree": t, "parents": [], "message": "init"}
        self.refs["main"] = c

    def files(self, branch: str) -> dict[str, str]:
        return dict(self.trees[self.commits[self.refs[branch]]["tree"]])

    def _resolve(self, ref: str) -> dict[str, str] | None:
        sha = self.refs.get(ref, ref)
        c = self.commits.get(sha)
        return self.trees[c["tree"]] if c else None

    def _commit_other(self, branch: str):
        parent = self.refs[branch]
        tree = dict(self.trees[self.commits[parent]["tree"]])
        tree["stats/other.txt"] = "чужий коміт\n"
        t = _sha(tree)
        self.trees[t] = tree
        c = _sha({"tree": t, "parents": [parent], "m": "other"})
        self.commits[c] = {"tree": t, "parents": [parent], "message": "other"}
        self.refs[branch] = c

    def handle(self, method, path, query, body, headers):
        if headers.get("Authorization") != f"Bearer {TOKEN}":
            return 401, {"message": "Bad credentials"}, None
        prefix = f"/repos/{self.repo}"
        if not path.startswith(prefix):
            return 404, {"message": "Not Found"}, None
        p = path[len(prefix):]
        with self.lock:
            m = re.fullmatch(r"/git/ref/heads/(.+)", p)
            if m and method == "GET":
                sha = self.refs.get(m.group(1))
                return (200, {"object": {"sha": sha}}, None) if sha else (404, {"message": "Not Found"}, None)
            m = re.fullmatch(r"/contents/(.+)", p)
            if m and method == "GET":
                files = self._resolve(query.get("ref", ["main"])[0])
                if files is None:
                    return 404, {"message": "No commit"}, None
                fp = m.group(1)
                if fp in files:
                    if "raw" in headers.get("Accept", ""):
                        return 200, None, files[fp]
                    return 200, {"encoding": "base64", "content": base64.b64encode(files[fp].encode()).decode()}, None
                children = {}
                for f in files:
                    if f.startswith(fp + "/"):
                        rest = f[len(fp) + 1:]
                        children[rest.split("/")[0]] = "dir" if "/" in rest else "file"
                if not children:
                    return 404, {"message": "Not Found"}, None
                return 200, [{"name": n, "type": t} for n, t in sorted(children.items())], None
            m = re.fullmatch(r"/git/commits/([0-9a-f]+)", p)
            if m and method == "GET":
                c = self.commits.get(m.group(1))
                return (200, {"sha": m.group(1), "tree": {"sha": c["tree"]}}, None) if c else (404, {}, None)
            if p == "/git/trees" and method == "POST":
                tree = dict(self.trees[body["base_tree"]]) if body.get("base_tree") else {}
                for it in body["tree"]:
                    tree[it["path"]] = it["content"]
                t = _sha(tree)
                self.trees[t] = tree
                return 201, {"sha": t}, None
            if p == "/git/commits" and method == "POST":
                c = _sha({"tree": body["tree"], "parents": body["parents"], "m": body["message"], "n": len(self.commits)})
                self.commits[c] = {"tree": body["tree"], "parents": body["parents"], "message": body["message"]}
                return 201, {"sha": c}, None
            m = re.fullmatch(r"/git/refs/heads/(.+)", p)
            if m and method == "PATCH":
                branch = m.group(1)
                if self.race_once:
                    self.race_once = False
                    self._commit_other(branch)
                new = self.commits.get(body["sha"])
                if not new or (not body.get("force") and self.refs.get(branch) not in new["parents"]):
                    return 422, {"message": "Update is not a fast forward"}, None
                self.refs[branch] = body["sha"]
                return 200, {"object": {"sha": body["sha"]}}, None
            if p == "/git/refs" and method == "POST":
                branch = body["ref"].removeprefix("refs/heads/")
                if branch in self.refs:
                    return 422, {"message": "Reference already exists"}, None
                self.refs[branch] = body["sha"]
                return 201, {"object": {"sha": body["sha"]}}, None
        return 404, {"message": f"no route {method} {p}"}, None

    def serve(self, port: int = 0):
        gh = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self, method):
                u = urlparse(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                with gh.lock:
                    gh.log.append((method, u.path))
                code, data, raw = gh.handle(method, u.path, parse_qs(u.query), body, dict(self.headers))
                payload = raw.encode() if raw is not None else json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8" if raw is not None else "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self._do("GET")

            def do_POST(self):
                self._do("POST")

            def do_PATCH(self):
                self._do("PATCH")

            def do_PUT(self):
                self._do("PUT")

            def do_DELETE(self):
                self._do("DELETE")

        srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, f"http://127.0.0.1:{srv.server_address[1]}"


if __name__ == "__main__":
    srv, url = FakeGitHub().serve(8766)
    print(f"Фейковий GitHub: {url}  (токен {TOKEN}, репозиторій owner/repo)")
    threading.Event().wait()
