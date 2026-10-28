"""Клієнт API платформи M2M: вхід, групи, команди об'єктів, історія результатів команд. Потокобезпечний."""

from __future__ import annotations

import threading
import time

import requests

from .errors import ApiError

COMMAND_TYPE = "CUSTOM"     # «Надіслати довільне повідомлення (custom_msg)» — єдиний тип у платформі
NAME_MAX = 50               # обмеження форми «Нова команда» на платформі
TEXT_MAX = 255              # attributes.data, maxLength у свагері
RESULT_TYPE = "COMMAND_RESULT"
HISTORY_PER_PAGE = 100

_RETRY_ANY = (429, 500, 502, 503, 504)   # для читання: повторюємо завжди
# Для створення/відправки — лише 429 (сервер відхилив через ліміт, точно не виконував). Будь-яка 5xx
# вважається «невідомо, чи виконано»: відправку не повторюємо, створення — лише після перевірки (jobs.py).
_RETRY_WRITE = (429,)


def items_of(d) -> list:
    """Список з відповіді: або сам список, або поле items/content/data."""
    if isinstance(d, list):
        return d
    if isinstance(d, dict):
        return d.get("items") or d.get("content") or d.get("data") or []
    return []


class M2MClient:
    """Окрема requests.Session на потік, автооновлення токена при 401, повтори з паузою."""

    def __init__(self, base_url: str, retries: int = 2, timeout: float = 60):
        self.base_url = base_url.strip().rstrip("/")
        self.retries = retries
        self.timeout = timeout
        self.token: str | None = None
        self.refresh_token: str | None = None
        self.email: str | None = None
        self.user_id: int | None = None
        self._password: str | None = None  # лише в пам'яті сесії — для повторного входу, ніде не зберігається
        self._lock = threading.Lock()
        self._local = threading.local()

    # ---------------------------------------------------------------- транспорт
    def _session(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            self._local.s = s
        return s

    def _send(self, method, path, params=None, json_data=None, token=None):
        headers = {"Accept": "application/json", "Cache-Control": "no-cache"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return self._session().request(
            method, f"{self.base_url}{path}", headers=headers, params=params, json=json_data, timeout=self.timeout
        )

    def login(self, email: str, password: str) -> None:
        try:
            r = self._send("POST", "/api/login", json_data={"email": email, "password": password})
        except requests.RequestException as e:
            raise ApiError(f"Сервер недоступний: {e}") from e
        if r.status_code in (400, 401, 403):
            raise ApiError("Невірний email або пароль", r.status_code)
        if r.status_code >= 400:
            raise ApiError(f"Вхід не вдався: HTTP {r.status_code} {r.text[:200]}", r.status_code)
        d = r.json() if r.content else {}
        self.token = d.get("token") or d.get("accessToken")
        self.refresh_token = d.get("refreshToken")
        self.email, self._password = email, password
        self.user_id = (d.get("user") or {}).get("id")
        if not self.token:
            raise ApiError("Сервер не повернув токен")

    def _refresh(self, used_token) -> bool:
        with self._lock:
            if self.token != used_token:
                return True  # інший потік уже оновив
            attempts = []
            if self.refresh_token:
                attempts.append(("/api/token/refresh", {"refreshToken": self.refresh_token}))
            if self.email and self._password:
                attempts.append(("/api/login", {"email": self.email, "password": self._password}))
            for path, body in attempts:
                try:
                    r = self._send("POST", path, json_data=body)
                    if r.status_code < 400 and r.content:
                        d = r.json()
                        t = d.get("token") or d.get("accessToken")
                        if t:
                            self.token = t
                            self.refresh_token = d.get("refreshToken") or self.refresh_token
                            return True
                except (requests.RequestException, ValueError):
                    pass
            return False

    @staticmethod
    def _pause(attempt: int, r: requests.Response | None = None) -> None:
        delay = 1.5 * (attempt + 1)
        if r is not None:
            try:
                delay = max(delay, min(float(r.headers.get("Retry-After", 0)), 30.0))
            except ValueError:
                pass
        time.sleep(delay)

    def request(self, method, path, params=None, json_data=None, idempotent: bool = True):
        """JSON-відповідь або {}. Неідемпотентні запити (створення, відправка) повторюються лише тоді,
        коли сервер точно їх не виконав — щоб не створити дубль і не надіслати команду двічі."""
        retry_codes = _RETRY_ANY if idempotent else _RETRY_WRITE
        for attempt in range(self.retries + 1):
            last = attempt >= self.retries
            tok = self.token
            try:
                r = self._send(method, path, params=params, json_data=json_data, token=tok)
            except requests.ConnectTimeout as e:   # з'єднання не встановлено — запит точно не дійшов
                if not last:
                    self._pause(attempt)
                    continue
                raise ApiError(f"Сервер не відповідає: {e}") from e
            except requests.RequestException as e:
                if idempotent and not last:
                    self._pause(attempt)
                    continue
                raise ApiError(f"Обрив з'єднання: {type(e).__name__}", uncertain=not idempotent) from e
            if r.status_code == 401 and not last and self._refresh(tok):
                continue
            if r.status_code in retry_codes and not last:
                self._pause(attempt, r)
                continue
            if r.status_code >= 400:
                raise ApiError(self._error_text(r), r.status_code,
                               uncertain=not idempotent and r.status_code >= 500 and r.status_code not in _RETRY_WRITE)
            if not r.content:
                return {}
            try:
                return r.json()
            except ValueError:
                return {}
        raise ApiError("Не вдалося виконати запит")  # pragma: no cover — цикл завжди повертає або кидає

    @staticmethod
    def _error_text(r: requests.Response) -> str:
        msg = ""
        try:
            d = r.json()
            if isinstance(d, dict):
                msg = str(d.get("message") or d.get("error") or "")
        except ValueError:
            msg = r.text[:200]
        return f"{r.status_code} — {msg}".strip(" —") if msg else f"HTTP {r.status_code}"

    # ---------------------------------------------------------------- групи й об'єкти
    def groups(self) -> list[dict]:
        """Усі групи об'єктів користувача: [{id, name, deviceIds}]."""
        out = []
        for g in items_of(self.request("GET", "/api/device/groups/lightweight/all")):
            if g.get("id") is None:
                continue
            out.append({"id": g["id"], "name": g.get("name") or f"Група {g['id']}",
                        "deviceIds": list(g.get("deviceIds") or [])})
        return sorted(out, key=lambda g: g["name"].lower())

    def find_user_id(self, email: str) -> int | None:
        """id користувача за email: сам користувач або один з його дочірніх користувачів."""
        email = email.strip().lower()
        if self.email and self.email.lower() == email and self.user_id is not None:
            return int(self.user_id)
        for u in items_of(self.request("GET", "/api/users", params={"search": email})):
            if str(u.get("email") or "").lower() == email and u.get("id") is not None:
                return int(u["id"])
        return None

    def groups_with_creator(self, per_page: int = 200) -> list[dict]:
        """Усі групи з полями creatorId / creatorName (посторінково)."""
        out, page = [], 1
        while True:
            d = self.request("GET", "/api/device/groups", params={"page": page, "per_page": per_page})
            items = items_of(d)
            out += items
            pages = int(d.get("pages") or 0) if isinstance(d, dict) else 0
            if not items or (pages and page >= pages) or (not pages and len(items) < per_page):
                return out
            page += 1

    def group_devices(self, group_id: int) -> list[dict]:
        """Об'єкти групи з назвою, IMEI, часом останнього повідомлення й правами."""
        return items_of(self.request("GET", f"/api/group/{group_id}/devices"))

    # ---------------------------------------------------------------- команди
    def device_commands(self, device_id: int) -> list[dict]:
        return items_of(self.request("GET", f"/api/device/{device_id}/commands"))

    def create_command(self, device_id: int, name: str, text: str) -> dict:
        body = {"description": name, "type": COMMAND_TYPE, "attributes": {"data": text}}
        return self.request("POST", f"/api/device/{device_id}/commands", json_data=body, idempotent=False) or {}

    def dispatch(self, device_id: int, command_id: int) -> None:
        self.request("POST", f"/api/device/{device_id}/command/{command_id}/dispatch", idempotent=False)

    # ---------------------------------------------------------------- результати команд
    def history_page(self, page: int, per_page: int = HISTORY_PER_PAGE) -> dict:
        d = self.request("GET", "/api/notification/history",
                         params={"page": page, "per_page": per_page, "type": RESULT_TYPE})
        pages = int(d.get("pages") or 0) if isinstance(d, dict) else 0
        return {"items": items_of(d), "pages": pages}

    def new_results(self, after_id: int, per_page: int = HISTORY_PER_PAGE, max_pages: int = 40) -> list[dict]:
        """Записи «Результат команди» з id > after_id (новіші за точку відліку), від старих до нових.
        Порядок сторінок визначається автоматично; гортаємо, поки не дійдемо до вже відомих записів."""
        first = self.history_page(1, per_page)
        items, pages = first["items"], max(first["pages"], 1)
        ids = [int(i["id"]) for i in items if i.get("id") is not None]
        ascending = len(ids) >= 2 and ids[0] < ids[-1]
        order = range(pages, 0, -1) if ascending else range(1, pages + 1)
        found: dict[int, dict] = {}
        for n, page in enumerate(order):
            if n >= max_pages:
                break
            chunk = items if page == 1 else self.history_page(page, per_page)["items"]
            chunk_ids = [int(i["id"]) for i in chunk if i.get("id") is not None]
            for it in chunk:
                if it.get("id") is not None and int(it["id"]) > after_id:
                    found[int(it["id"])] = it
            if not chunk_ids or min(chunk_ids) <= after_id:
                break
        return [found[k] for k in sorted(found)]

    def latest_result_id(self) -> int:
        """Найбільший id у історії «Результат команди» — точка відліку перед відправкою."""
        first = self.history_page(1, HISTORY_PER_PAGE)
        ids = [int(i["id"]) for i in first["items"] if i.get("id") is not None]
        if len(ids) >= 2 and ids[0] < ids[-1] and first["pages"] > 1:  # старі спочатку — беремо останню сторінку
            last = self.history_page(first["pages"], HISTORY_PER_PAGE)["items"]
            ids += [int(i["id"]) for i in last if i.get("id") is not None]
        return max(ids) if ids else 0
