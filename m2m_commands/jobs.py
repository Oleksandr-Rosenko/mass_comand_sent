"""Фонові завдання: перевірка команд групи, масове створення, масова відправка з очікуванням відповідей.

Кожне завдання працює у своєму потоці й обробляє об'єкти пачками: у пачці — кілька паралельних запитів,
між пачками — пауза. Усі поля для інтерфейсу можна читати з будь-якого потоку.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .client import M2MClient
from .errors import ApiError

# ---------------------------------------------------------------- статуси (ключ → підпис в інтерфейсі й CSV)
QUEUED, RUNNING, CREATED, SKIPPED, ERROR = "queued", "running", "created", "skipped", "error"
SENT, ANSWERED, NO_ANSWER, NOT_SENT = "sent", "answered", "noanswer", "notsent"

LABELS = {
    QUEUED: "В черзі", RUNNING: "В роботі", CREATED: "Створено", SKIPPED: "Пропущено", ERROR: "Помилка",
    SENT: "Чекаємо", ANSWERED: "Відповів", NO_ANSWER: "Без відповіді", NOT_SENT: "Не надіслано",
}

PERM_CREATE = "Створення команд"
PERM_EXECUTE = "Виконання команд"

# активність об'єкта за часом останнього повідомлення
ONLINE, TODAY, STALE = "online", "today", "stale"
ACTIVITY_LABELS = {ONLINE: "Онлайн", TODAY: "За добу", STALE: "Давніше"}


# ---------------------------------------------------------------- дані
@dataclass
class Device:
    id: int
    name: str
    imei: str = ""
    last_message: datetime | None = None
    can_create: bool | None = None    # None — права невідомі, вирішить сервер
    can_execute: bool | None = None

    @classmethod
    def from_api(cls, d: dict) -> "Device":
        perms = d.get("permissions") or {}
        return cls(
            id=int(d["id"]),
            name=d.get("name") or str(d["id"]),
            imei=str(d.get("uniqueId") or ""),
            last_message=parse_dt(d.get("lastMessageDate") or d.get("lastUpdate")),
            can_create=perms.get("CREATE_COMMANDS") if isinstance(perms, dict) else None,
            can_execute=perms.get("EXECUTE_COMMANDS") if isinstance(perms, dict) else None,
        )


@dataclass
class Command:
    id: int
    name: str
    text: str

    @classmethod
    def from_api(cls, d: dict) -> "Command":
        return cls(id=int(d["id"]), name=(d.get("description") or "").strip(),
                   text=str((d.get("attributes") or {}).get("data") or ""))


@dataclass
class LoadCfg:
    batch_size: int = 50
    pause: float = 2.0
    workers: int = 4

    def batches(self, n: int) -> int:
        return math.ceil(n / max(1, self.batch_size)) if n else 0


def parse_dt(v) -> datetime | None:
    """ISO-дата з API → datetime у UTC. Дата без зсуву вважається UTC."""
    if not v:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def activity(last_message: datetime | None, online_min: float, now: datetime | None = None) -> str:
    """online — повідомлення не давніше online_min хв, today — за останню добу, stale — давніше або ніколи."""
    if last_message is None:
        return STALE
    age = (now or now_utc()) - last_message
    if age <= timedelta(minutes=online_min):
        return ONLINE
    return TODAY if age <= timedelta(days=1) else STALE


def by_priority(targets: list[tuple["Device", int]]) -> list[tuple["Device", int]]:
    """Свіжіші об'єкти першими: вони відповідають одразу, «сплячі» — в кінці черги."""
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(targets, key=lambda t: t[0].last_message or oldest, reverse=True)


def find_by_name(commands: list[Command], name: str) -> list[Command]:
    key = name.strip()
    return [c for c in commands if c.name == key]


def clean_message(msg: str | None) -> str:
    """Текст відповіді трекера без HTML-посилань, які платформа вставляє в сповіщення."""
    text = re.sub(r"<[^>]+>", "", msg or "")
    return re.sub(r"\s+", " ", text).strip()


def result_device_id(item: dict) -> int | None:
    if item.get("deviceId") is not None:
        return int(item["deviceId"])
    meta = item.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = None
    if isinstance(meta, dict) and meta.get("deviceId") is not None:
        return int(meta["deviceId"])
    return None


def describe_error(e: ApiError, perm: str) -> str:
    if e.status == 403:
        return f"403 — немає права «{perm}»"
    if e.status == 404:
        return "404 — об'єкт або команду не знайдено"
    return str(e)


# ---------------------------------------------------------------- база
class BaseJob:
    """Потік + пачки + зупинка. Нащадки заповнюють self.rows: {device_id: dict}."""

    def __init__(self, client: M2MClient, cfg: LoadCfg):
        self.client = client
        self.cfg = cfg
        self.status = "idle"        # idle | running | done | stopped | error
        self.message = ""
        self.rows: dict[int, dict] = {}
        self.batch_no = 0
        self.batch_total = 0
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        with self._lock:
            for r in self.rows.values():
                out[r["status"]] = out.get(r["status"], 0) + 1
        return out

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.rows.values()]

    def _set(self, did: int, **kw) -> None:
        with self._lock:
            self.rows[did].update(kw)

    def start(self) -> None:
        self._stop.clear()
        self.status, self.message = "running", ""
        self.started_at, self.finished_at = time.time(), None
        self._thread = threading.Thread(target=self._main, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _main(self) -> None:
        try:
            self._run()
            if self.status == "running":
                self.status = "stopped" if self._stop.is_set() else "done"
        except Exception as e:  # noqa: BLE001 — показуємо будь-яку помилку в інтерфейсі, а не валимо потік
            self.status, self.message = "error", f"{type(e).__name__}: {e}"
        finally:
            self.finished_at = time.time()

    def _run(self) -> None:  # pragma: no cover — перевизначається
        raise NotImplementedError

    def _run_batches(self, ids: list[int], fn, stop: threading.Event | None = None, between=None) -> bool:
        """fn(did) для кожного об'єкта, пачками. False — зупинено до кінця."""
        stop = stop or self._stop
        size = max(1, int(self.cfg.batch_size))
        self.batch_total = math.ceil(len(ids) / size) if ids else 0
        for b in range(self.batch_total):
            if stop.is_set():
                return False
            self.batch_no = b + 1
            chunk = ids[b * size:(b + 1) * size]
            with ThreadPoolExecutor(max_workers=max(1, int(self.cfg.workers))) as ex:
                list(ex.map(lambda did: None if stop.is_set() else fn(did), chunk))
            if between:
                between()
            if b < self.batch_total - 1 and self.cfg.pause > 0 and stop.wait(self.cfg.pause):
                return False
        return not stop.is_set()


# ---------------------------------------------------------------- 0. перевірка команд групи
class ScanJob(BaseJob):
    """Читає список команд кожного об'єкта групи. Результат: self.commands {device_id: [Command]}."""

    def __init__(self, client: M2MClient, group: dict, devices: list[Device], cfg: LoadCfg):
        super().__init__(client, cfg)
        self.group = group
        self.devices = {d.id: d for d in devices}
        self.commands: dict[int, list[Command]] = {}
        self.errors: dict[int, str] = {}
        self.rows = {d.id: {"id": d.id, "status": QUEUED} for d in devices}
        self.done_at: datetime | None = None

    @property
    def processed(self) -> int:
        return len(self.commands) + len(self.errors)

    @property
    def total(self) -> int:
        return len(self.devices)

    def _one(self, did: int) -> None:
        try:
            cmds = [Command.from_api(c) for c in self.client.device_commands(did) if c.get("id") is not None]
            with self._lock:
                self.commands[did] = cmds
                self.errors.pop(did, None)
                self.rows[did]["status"] = CREATED
        except ApiError as e:
            with self._lock:
                self.errors[did] = describe_error(e, "Перегляд команд")
                self.rows[did]["status"] = ERROR

    def _run(self) -> None:
        todo = [did for did in self.devices if did not in self.commands]
        self._run_batches(todo, self._one)
        self.done_at = now_utc()

    def update(self, did: int, commands: list[Command]) -> None:
        """Оновлення з інших завдань (після створення команди)."""
        with self._lock:
            self.commands[did] = commands
            self.errors.pop(did, None)

    # ---- зведення для інтерфейсу
    def names(self) -> list[tuple[str, int]]:
        """Імена команд у групі з кількістю об'єктів, де вони є: [(name, count)], найпоширеніші спершу."""
        cnt: dict[str, int] = {}
        with self._lock:
            for cmds in self.commands.values():
                for n in {c.name for c in cmds}:
                    cnt[n] = cnt.get(n, 0) + 1
        return sorted(cnt.items(), key=lambda x: (-x[1], x[0].lower()))

    def variants(self, name: str) -> list[tuple[str, int]]:
        """Різні тексти команди з таким ім'ям: [(text, count)], найпоширеніші спершу."""
        cnt: dict[str, int] = {}
        with self._lock:
            for cmds in self.commands.values():
                for t in {c.text for c in find_by_name(cmds, name)}:
                    cnt[t] = cnt.get(t, 0) + 1
        return sorted(cnt.items(), key=lambda x: -x[1])


# ---------------------------------------------------------------- 1. масове створення
class CreateJob(BaseJob):
    """Створює команду на об'єктах. Якщо команда з таким ім'ям уже є — пропускає (дублів не робить)."""

    def __init__(self, client: M2MClient, group: dict, devices: list[Device], name: str, text: str,
                 cfg: LoadCfg, scan: ScanJob | None = None):
        super().__init__(client, cfg)
        self.group, self.name, self.text, self.scan = group, name.strip(), text, scan
        self.devices = {d.id: d for d in devices}
        self.rows = {d.id: {"id": d.id, "name": d.name, "imei": d.imei, "status": QUEUED, "detail": "",
                            "command_id": None} for d in devices}

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def processed(self) -> int:
        c = self.counts()
        return c.get(CREATED, 0) + c.get(SKIPPED, 0) + c.get(ERROR, 0)

    def _existing(self, did: int) -> list[Command] | None:
        cmds = [Command.from_api(c) for c in self.client.device_commands(did) if c.get("id") is not None]
        if self.scan is not None:
            self.scan.update(did, cmds)
        return cmds

    def _one(self, did: int) -> None:
        dev = self.devices[did]
        if dev.can_create is False:
            self._set(did, status=SKIPPED, detail=f"Немає права «{PERM_CREATE}»")
            return
        self._set(did, status=RUNNING, detail="")
        try:
            existing = self._existing(did)
        except ApiError as e:
            self._set(did, status=ERROR, detail="Не вдалося прочитати команди об'єкта: "
                      + describe_error(e, "Перегляд команд"))
            return
        try:
            same = find_by_name(existing, self.name)
            if same:
                c = same[0]
                note = "" if c.text == self.text else " · інший текст"
                self._set(did, status=SKIPPED, detail=f"«{self.name}» вже є · ID {c.id}{note}", command_id=c.id)
                return
            try:
                created = self.client.create_command(did, self.name, self.text)
            except ApiError as e:
                if not e.uncertain:
                    raise
                # Сервер не підтвердив — перевіряємо, чи команда все ж створилась, щоб не робити дубль.
                same = find_by_name(self._existing(did), self.name)
                if not same:
                    raise
                self._set(did, status=CREATED, detail=f"ID команди {same[0].id} (перевірено після збою)",
                          command_id=same[0].id)
                return
            cid = created.get("id")
            if self.scan is not None and cid is not None:
                cmds = list(self.scan.commands.get(did, []))
                cmds.append(Command(id=int(cid), name=self.name, text=self.text))
                self.scan.update(did, cmds)
            self._set(did, status=CREATED, detail=f"ID команди {cid}" if cid is not None else "Створено",
                      command_id=cid)
        except ApiError as e:
            self._set(did, status=ERROR, detail=describe_error(e, PERM_CREATE))

    def _run(self) -> None:
        todo = [did for did, r in self.rows.items() if r["status"] in (QUEUED, RUNNING, ERROR)]
        self._run_batches(todo, self._one)
        with self._lock:  # зупинено посеред пачки — повертаємо «в роботі» в чергу
            for r in self.rows.values():
                if r["status"] == RUNNING:
                    r["status"] = QUEUED

    def retry_failed(self) -> None:
        """Повторити помилки й необроблені."""
        with self._lock:
            for r in self.rows.values():
                if r["status"] == ERROR:
                    r["status"], r["detail"] = QUEUED, ""
        self.start()


# ---------------------------------------------------------------- 2. масова відправка
class DispatchJob(BaseJob):
    """Надсилає команду на об'єкти пачками й чекає «Результат команди» в історії сповіщень.

    Точка відліку — найбільший id у історії COMMAND_RESULT перед стартом: відповіддю вважається перший
    новіший запис по цьому об'єкту, створений не раніше ніж за 2 хв до відправки на нього.
    Результати бачать усі, хто має доступ до об'єкта, тож якщо хтось інший у той самий час шле команди
    на ці об'єкти, їхні відповіді теж можуть потрапити в звіт.
    """

    CLOCK_SLACK = timedelta(minutes=2)

    def __init__(self, client: M2MClient, group: dict, name: str, text: str,
                 targets: list[tuple[Device, int]], skipped: list[tuple[Device, str]], cfg: LoadCfg,
                 wait_minutes: float = 15, poll_every: float = 15, online_min: float = 10):
        """targets надсилаються в переданому порядку (для пріоритету — відсортуйте by_priority)."""
        super().__init__(client, cfg)
        self.group, self.name, self.text = group, name, text
        self.wait_minutes, self.poll_every = wait_minutes, max(3.0, poll_every)
        self.online_min = online_min
        self.phase = "idle"   # idle | baseline | sending | waiting | done
        self.baseline_id: int | None = None
        self.last_id = 0
        self.wait_started: float | None = None
        self.wait_until: float | None = None
        self.poll_error = ""
        self.last_poll: float | None = None
        self.started_wall = now_utc()
        self._stop_wait = threading.Event()
        self._poll_lock = threading.Lock()
        self.rows = {}
        for n, (dev, cid) in enumerate(targets, 1):
            self.rows[dev.id] = self._row(dev, QUEUED, command_id=cid, order=n)
        for dev, reason in skipped:
            self.rows[dev.id] = self._row(dev, SKIPPED, detail=reason)

    def _row(self, dev: Device, status: str, detail: str = "", command_id: int | None = None,
             order: int | None = None) -> dict:
        return {"id": dev.id, "name": dev.name, "imei": dev.imei, "last_message": dev.last_message,
                "activity": activity(dev.last_message, self.online_min, self.started_wall), "order": order,
                "status": status, "detail": detail, "answer": "", "command_id": command_id,
                "sent_at": None, "answered_at": None}

    def activity_stats(self) -> dict[str, dict[str, int]]:
        """По кожній групі активності: скільки надіслали й скільки відповіли."""
        out = {k: {"sent": 0, "answered": 0} for k in ACTIVITY_LABELS}
        with self._lock:
            for r in self.rows.values():
                if r["status"] in (SENT, ANSWERED, NO_ANSWER):
                    out[r["activity"]]["sent"] += 1
                    if r["status"] == ANSWERED:
                        out[r["activity"]]["answered"] += 1
        return out

    # ---- лічильники для інтерфейсу
    @property
    def targets(self) -> int:
        return sum(1 for r in self.rows.values() if r["command_id"] is not None)

    @property
    def dispatched(self) -> int:
        c = self.counts()
        return c.get(SENT, 0) + c.get(ANSWERED, 0) + c.get(NO_ANSWER, 0)

    @property
    def wait_left(self) -> float | None:
        if self.wait_until is None or self.phase != "waiting":
            return None
        return max(0.0, self.wait_until - time.time())

    def stop(self) -> None:
        """Зупинити відправку (те, що вже надіслано, продовжуємо чекати)."""
        self._stop.set()

    def stop_waiting(self) -> None:
        self._stop.set()
        self._stop_wait.set()

    # ---- робота
    def _send_one(self, did: int) -> None:
        r = self.rows[did]
        self._set(did, status=RUNNING)
        sent_at = now_utc()
        try:
            self.client.dispatch(did, int(r["command_id"]))
            self._set(did, status=SENT, sent_at=sent_at, detail="")
        except ApiError as e:
            if e.uncertain:  # могло дійти — слухаємо відповідь, але не повторюємо, щоб не надіслати двічі
                self._set(did, status=SENT, sent_at=sent_at, detail=f"Сервер не підтвердив відправку: {e}")
            else:
                self._set(did, status=ERROR, detail=describe_error(e, PERM_EXECUTE))

    def poll(self) -> int:
        """Читає нові «Результати команд» і проставляє відповіді. Повертає кількість нових відповідей."""
        if self.baseline_id is None:
            return 0
        with self._poll_lock:
            try:
                items = self.client.new_results(self.last_id)
                self.poll_error = ""
            except ApiError as e:
                self.poll_error = f"Не вдалося прочитати відповіді: {e}"
                return 0
            self.last_poll = time.time()
            got = 0
            for it in items:
                self.last_id = max(self.last_id, int(it["id"]))
                did = result_device_id(it)
                with self._lock:
                    r = self.rows.get(did) if did is not None else None
                    if not r or r["status"] not in (SENT, NO_ANSWER) or r["sent_at"] is None:
                        continue
                    created = parse_dt(it.get("createdAt"))
                    if created is not None and created < r["sent_at"] - self.CLOCK_SLACK:
                        continue
                    r.update(status=ANSWERED, answer=clean_message(it.get("message")),
                             answered_at=created or now_utc())
                    got += 1
            return got

    def _pending_answers(self) -> int:
        return self.counts().get(SENT, 0)

    def _maybe_poll(self) -> None:
        if self.last_poll is None or time.time() - self.last_poll >= self.poll_every:
            self.poll()

    def _run(self) -> None:
        if self.baseline_id is None:
            self.phase = "baseline"
            self.message = "Читаю історію сповіщень (точка відліку)"
            self.baseline_id = self.client.latest_result_id()
            self.last_id = self.baseline_id
        self.phase, self.message = "sending", ""
        todo = [did for did, r in self.rows.items() if r["status"] == QUEUED]
        self._run_batches(todo, self._send_one, between=self._maybe_poll)
        with self._lock:
            for r in self.rows.values():
                if r["status"] in (QUEUED, RUNNING):
                    r["status"], r["detail"] = NOT_SENT, "Відправку зупинено"
        # очікування відповідей — навіть якщо відправку зупинили, чекаємо на вже надіслані
        self.phase = "waiting"
        self.wait_started = time.time()
        self.wait_until = self.wait_started + self.wait_minutes * 60
        while not self._stop_wait.is_set():
            self.poll()
            if self._pending_answers() == 0 or time.time() >= self.wait_until:
                break
            self._stop_wait.wait(min(self.poll_every, max(0.5, self.wait_until - time.time())))
        reason = "Очікування зупинено" if self._stop_wait.is_set() else f"Немає відповіді за {self.wait_minutes:g} хв"
        with self._lock:
            for r in self.rows.values():
                if r["status"] == SENT:
                    r["status"] = NO_ANSWER
                    r["detail"] = r["detail"] or reason
        self.phase = "done"
        if self._stop_wait.is_set() or self._stop.is_set():
            self.status = "stopped"

    def not_answered(self) -> list[int]:
        """Об'єкти, яким варто надіслати ще раз: без відповіді, не надіслано, помилка відправки."""
        with self._lock:
            return [did for did, r in self.rows.items()
                    if r["command_id"] is not None and r["status"] in (NO_ANSWER, NOT_SENT, ERROR)]
