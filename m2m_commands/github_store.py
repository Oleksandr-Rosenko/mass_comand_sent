"""Збереження статистики в репозиторій GitHub, в окрему гілку (за замовчуванням `stats`).

Окрема гілка — свідомо: Streamlit Community Cloud перезбирає застосунок після кожного пушу в гілку, з якої він
запущений (`main`), а це обірвало б розсилку посеред роботи. Гілка `stats` містить лише папку `stats/`
(створюється «з нуля», без коду застосунку). Кожне збереження — один коміт з усіма файлами команди.
Якщо одночасно зберігає хтось інший, коміт не пройде fast-forward — тоді перечитуємо дані, зливаємо ще раз
і пробуємо знову. Гілку `main` модуль не читає й не змінює.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Callable

import requests

from .stats import files_for


class GitHubError(RuntimeError):
    pass


class _Conflict(GitHubError):
    pass


class GitHubStore:
    def __init__(self, token: str, repo: str, branch: str = "stats", api: str = "https://api.github.com",
                 timeout: float = 30):
        self.token, self.repo, self.branch = token.strip(), repo.strip().strip("/"), branch.strip()
        self.api = api.rstrip("/")
        self.timeout = timeout
        if not self.branch or self.branch in ("main", "master"):
            raise GitHubError("Статистику не можна писати в main/master — вкажіть окрему гілку, напр. stats")

    # ---------------------------------------------------------------- HTTP
    def _req(self, method: str, path: str, json_data=None, params=None, raw: bool = False) -> requests.Response:
        headers = {"Authorization": f"Bearer {self.token}", "X-GitHub-Api-Version": "2022-11-28",
                   "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json"}
        try:
            r = requests.request(method, f"{self.api}/repos/{self.repo}{path}", headers=headers, json=json_data,
                                 params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise GitHubError(f"GitHub недоступний: {e}") from e
        if r.status_code in (401, 403):
            raise GitHubError(f"GitHub відмовив у доступі ({r.status_code}): перевірте GITHUB_TOKEN і його права "
                              "(Contents: Read and write для цього репозиторію)")
        return r

    @staticmethod
    def _fail(r: requests.Response, what: str):
        try:
            msg = r.json().get("message", "")
        except ValueError:
            msg = r.text[:200]
        raise GitHubError(f"GitHub: {what} — HTTP {r.status_code} {msg}".strip())

    # ---------------------------------------------------------------- читання
    def head(self) -> str | None:
        """sha останнього коміту гілки або None, якщо гілки ще немає."""
        r = self._req("GET", f"/git/ref/heads/{self.branch}")
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            self._fail(r, "читання гілки")
        return r.json()["object"]["sha"]

    def read_text(self, path: str, ref: str) -> str | None:
        r = self._req("GET", f"/contents/{path}", params={"ref": ref}, raw=True)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            self._fail(r, f"читання {path}")
        ctype = r.headers.get("Content-Type", "")
        if "json" in ctype and "raw" not in ctype:  # GitHub віддав метадані з base64 замість сирого файлу
            try:
                d = r.json()
            except ValueError:
                d = None
            if isinstance(d, dict) and d.get("encoding") == "base64" and "content" in d:
                return base64.b64decode(d["content"]).decode("utf-8")
        return r.content.decode("utf-8")

    def read_doc(self, folder: str, ref: str | None = None) -> dict | None:
        ref = ref or self.head()
        if not ref:
            return None
        text = self.read_text(f"{folder}/data.json", ref)
        return json.loads(text) if text else None

    def list_folders(self, ref: str | None = None) -> list[str]:
        """Папки команд у stats/."""
        ref = ref or self.head()
        if not ref:
            return []
        r = self._req("GET", "/contents/stats", params={"ref": ref})
        if r.status_code == 404:
            return []
        if r.status_code != 200:
            self._fail(r, "список статистики")
        return [f"stats/{x['name']}" for x in r.json() if x.get("type") == "dir"]

    def read_all(self) -> list[dict]:
        ref = self.head()
        return [d for f in self.list_folders(ref) if (d := self.read_doc(f, ref))]

    # ---------------------------------------------------------------- запис
    def _commit(self, files: dict[str, str], message: str, parent: str | None) -> str:
        tree_items = [{"path": p, "mode": "100644", "type": "blob", "content": c} for p, c in files.items()]
        body = {"tree": tree_items}
        if parent:
            r = self._req("GET", f"/git/commits/{parent}")
            if r.status_code != 200:
                self._fail(r, "читання коміту")
            body["base_tree"] = r.json()["tree"]["sha"]
        r = self._req("POST", "/git/trees", json_data=body)
        if r.status_code != 201:
            self._fail(r, "створення дерева")
        r = self._req("POST", "/git/commits", json_data={"message": message, "tree": r.json()["sha"],
                                                         "parents": [parent] if parent else []})
        if r.status_code != 201:
            self._fail(r, "створення коміту")
        sha = r.json()["sha"]
        if parent:
            r = self._req("PATCH", f"/git/refs/heads/{self.branch}", json_data={"sha": sha, "force": False})
        else:
            r = self._req("POST", "/git/refs", json_data={"ref": f"refs/heads/{self.branch}", "sha": sha})
        if r.status_code in (409, 422):
            raise _Conflict("гілку змінили одночасно")
        if r.status_code not in (200, 201):
            self._fail(r, "оновлення гілки")
        return sha

    def update(self, folder: str, mutate: Callable[[dict | None], dict], message: str, attempts: int = 5) -> dict:
        """Читає data.json, застосовує mutate, записує data.json + CSV одним комітом. Повтор при конфлікті."""
        for i in range(attempts):
            parent = self.head()
            doc = mutate(self.read_doc(folder, parent) if parent else None)
            files = {f"{folder}/{name}": content for name, content in files_for(doc).items()}
            try:
                self._commit(files, message, parent)
                return doc
            except _Conflict:
                time.sleep(1 + i)
        raise GitHubError("Не вдалося зберегти статистику: гілку постійно змінюють одночасно, спробуйте пізніше")
