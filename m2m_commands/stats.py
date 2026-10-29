"""Статистика застосування команди по групах: хто прийняв команду (трекер відповів), а хто ні і чому.

Одна команда (ім'я + текст) — одна папка `stats/<ім'я>-<хеш тексту>/` з трьома файлами:
`data.json` (джерело правди), `groups.csv` і `objects.csv` (GitHub показує їх як таблиці).
Злиття ідемпотентне: той самий запуск можна зберігати кілька разів (наприклад, після дочитаних пізніх відповідей),
лічильники не подвоюються. Хто хоч раз прийняв команду — лишається «прийняв».
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from datetime import timedelta

from .jobs import (ANSWERED, ERROR, LABELS, NO_ANSWER, NOT_SENT, SENT, SKIPPED, clean_message, now_utc, parse_dt,
                   result_device_id)

try:
    TZ = ZoneInfo("Europe/Kyiv")
except Exception:  # noqa: BLE001 — старіша база часових зон
    TZ = ZoneInfo("Europe/Kiev")

ACCEPTED = "Прийняв"
NOT_ACCEPTED = {NO_ANSWER: "Надіслано, відповіді немає — повторити", ERROR: "Помилка відправки — повторити",
                NOT_SENT: "Не надіслано (відправку зупинено) — повторити", SENT: "Чекаємо відповідь"}


def command_dir(name: str, text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_.") or "command"
    return f"stats/{slug}-{hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]}"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def fmt_local(v) -> str:
    dt = parse_dt(v) if not isinstance(v, datetime) else v
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M") if dt else ""


def new_doc(name: str, text: str) -> dict:
    return {"version": 1, "command": {"name": name, "text": text}, "updated_at": None, "groups": {}}


def merge_run(doc: dict | None, *, name: str, text: str, group: dict, run_id: str, started_at: datetime,
              rows: list[dict]) -> dict:
    """Додає результати одного запуску (рядки DispatchJob.snapshot()) у статистику групи."""
    doc = doc or new_doc(name, text)
    g = doc["groups"].setdefault(str(group["id"]), {"name": group.get("name", ""), "objects": 0, "runs": {},
                                                    "last_run_at": None, "devices": {}})
    g["name"] = group.get("name") or g["name"]
    if group.get("deviceIds"):
        g["objects"] = len(group["deviceIds"])
    sent = answered = 0
    for r in rows:
        rec = g["devices"].setdefault(str(r["id"]), {
            "name": r.get("name") or "", "imei": r.get("imei") or "", "accepted": False, "status": "", "detail": "",
            "answer": "", "answered_at": None, "last_sent": None, "sent_runs": [], "last_message": None,
        })
        rec["name"] = r.get("name") or rec["name"]
        rec["imei"] = r.get("imei") or rec["imei"]
        lm = r.get("last_message")
        if lm and (not rec["last_message"] or lm > parse_dt(rec["last_message"])):
            rec["last_message"] = _iso(lm)
        if r.get("sent_at"):
            sent += 1
            if run_id not in rec["sent_runs"]:
                rec["sent_runs"].append(run_id)
            if not rec["last_sent"] or r["sent_at"] > parse_dt(rec["last_sent"]):
                rec["last_sent"] = _iso(r["sent_at"])
        st = r["status"]
        if st == ANSWERED:
            answered += 1
            prev = parse_dt(rec["answered_at"])
            new = r.get("answered_at")
            if not rec["accepted"] or (new and (prev is None or new >= prev)):
                rec.update(accepted=True, status=ACCEPTED, detail="", answer=r.get("answer") or "",
                           answered_at=_iso(new) or rec["answered_at"])
        elif not rec["accepted"]:
            if st == SKIPPED:
                detail = r.get("detail") or ""
                if detail.startswith("Вже прийняв команду"):  # прийняв раніше (в іншій групі / запуску)
                    rec.update(accepted=True, status=ACCEPTED, detail=f"{detail} — раніше, в іншому запуску")
                elif not rec["sent_runs"]:  # ще жодного разу не надсилали — показуємо причину пропуску
                    rec.update(status=detail or "Пропущено", detail="")
            else:
                rec.update(status=NOT_ACCEPTED.get(st, LABELS.get(st, st)), detail=r.get("detail") or "")
    g["runs"][run_id] = {"started_at": _iso(started_at), "sent": sent, "answered": answered}
    g["last_run_at"] = max((x["started_at"] for x in g["runs"].values() if x["started_at"]), default=None)
    doc["updated_at"] = _iso(now_utc())
    return doc


def first_send(g: dict, rec: dict) -> datetime | None:
    """Коли цьому об'єкту в цій групі вперше надсилали команду (початок найранішого запуску з відправкою)."""
    starts = [parse_dt(g["runs"][r]["started_at"]) for r in rec.get("sent_runs", []) if r in g.get("runs", {})]
    starts = [x for x in starts if x]
    return min(starts) if starts else parse_dt(rec.get("last_sent"))


def history_since(doc: dict) -> datetime | None:
    """З якого моменту читати історію: найраніша відправка серед об'єктів, що ще не прийняли."""
    out = [first_send(g, rec) for g in doc.get("groups", {}).values() for rec in g["devices"].values()
           if not rec["accepted"] and rec.get("sent_runs")]
    out = [x for x in out if x]
    return min(out) - timedelta(minutes=2) if out else None


def apply_history(doc: dict, items: list[dict], slack: timedelta = timedelta(minutes=2)) -> int:
    """Зараховує «Результати команди» з історії об'єктам, яким надсилали команду і які ще не прийняли.
    Відповідь рахується, якщо вона прийшла після першої відправки цієї команди на об'єкт. Повертає кількість змін."""
    by_dev: dict[int, list] = {}
    for it in items:
        did, dt = result_device_id(it), parse_dt(it.get("createdAt"))
        if did is not None and dt is not None:
            by_dev.setdefault(did, []).append((dt, it))
    changed = 0
    for g in doc.get("groups", {}).values():
        for did, rec in g["devices"].items():
            if rec["accepted"] or not rec.get("sent_runs"):
                continue
            start = first_send(g, rec)
            cands = sorted(((dt, it) for dt, it in by_dev.get(int(did), []) if start is None or dt >= start - slack),
                           key=lambda x: x[0])
            if cands:
                dt, it = cands[0]
                rec.update(accepted=True, status=ACCEPTED, detail="Зараховано з історії сповіщень",
                           answer=clean_message(it.get("message")), answered_at=_iso(dt))
                changed += 1
    if changed:
        doc["updated_at"] = _iso(now_utc())
    return changed


def history_diagnostics(docs: list[dict], items: list[dict], slack: timedelta = timedelta(minutes=2)) -> dict:
    """Чому відповіді з історії зараховані чи ні — для блоку «Діагностика звірки»."""
    sent: dict[int, datetime | None] = {}
    accepted: set[int] = set()
    for doc in docs:
        for g in doc.get("groups", {}).values():
            for did, rec in g["devices"].items():
                if rec["accepted"]:
                    accepted.add(int(did))
                elif rec.get("sent_runs"):
                    st = first_send(g, rec)
                    prev = sent.get(int(did))
                    sent[int(did)] = st if prev is None or (st and st < prev) else prev
    out = {"read": len(items), "no_device": 0, "no_date": 0, "not_sent": 0, "already": 0, "too_early": 0,
           "match": 0, "sample": []}
    for it in items:
        did, dt = result_device_id(it), parse_dt(it.get("createdAt"))
        if len(out["sample"]) < 5:
            out["sample"].append({"id": it.get("id"), "deviceId": it.get("deviceId"), "deviceName": it.get("deviceName"),
                                  "createdAt": it.get("createdAt"), "message": clean_message(it.get("message"))[:60]})
        if did is None:
            out["no_device"] += 1
        elif dt is None:
            out["no_date"] += 1
        elif did in accepted:
            out["already"] += 1
        elif did not in sent:
            out["not_sent"] += 1
        elif sent[did] is not None and dt < sent[did] - slack:
            out["too_early"] += 1
        else:
            out["match"] += 1
    return out


# ---------------------------------------------------------------- зведення
def group_summary(g: dict) -> dict:
    accepted = sum(1 for d in g["devices"].values() if d["accepted"])
    objects = g.get("objects") or len(g["devices"])
    return {"objects": objects, "accepted": accepted, "not_accepted": max(0, objects - accepted),
            "percent": round(accepted / objects * 100) if objects else 0, "runs": len(g["runs"]),
            "last_run_at": g.get("last_run_at")}


def device_rows(g: dict) -> list[dict]:
    """Об'єкти групи: спершу ті, що прийняли (свіжіші відповіді вище), потім ті, що ні."""
    items = list(g["devices"].items())
    items.sort(key=lambda kv: (not kv[1]["accepted"],
                               -(parse_dt(kv[1]["answered_at"]).timestamp() if kv[1]["answered_at"] else 0),
                               kv[1]["status"], kv[1]["name"].lower()))
    return [{
        "Прийняв": "Так" if d["accepted"] else "Ні", "Об'єкт": d["name"], "IMEI": d["imei"], "ID об'єкта": int(did),
        "Статус": d["status"], "Деталі": d["detail"], "Відповідь трекера": d["answer"],
        "Коли відповів": fmt_local(d["answered_at"]), "Скільки разів надсилали": len(d["sent_runs"]),
        "Останнє надсилання": fmt_local(d["last_sent"]), "Останнє повідомлення": fmt_local(d["last_message"]),
    } for did, d in items]


def _csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def groups_table(doc: dict) -> list[dict]:
    out = []
    for gid, g in sorted(doc["groups"].items(), key=lambda kv: kv[1].get("last_run_at") or "", reverse=True):
        s = group_summary(g)
        out.append({"Група": g["name"], "ID групи": int(gid), "Об'єктів": s["objects"], "Прийняли": s["accepted"],
                    "Не прийняли": s["not_accepted"], "% прийняли": s["percent"], "Запусків": s["runs"],
                    "Останній запуск": fmt_local(s["last_run_at"])})
    return out


def objects_table(doc: dict) -> list[dict]:
    out = []
    for _, g in sorted(doc["groups"].items(), key=lambda kv: kv[1]["name"].lower()):
        out += [{"Група": g["name"], **r} for r in device_rows(g)]
    return out


def files_for(doc: dict) -> dict[str, str]:
    """Вміст файлів папки команди: data.json + таблиці для перегляду на GitHub."""
    return {
        "data.json": json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True),
        "groups.csv": _csv(groups_table(doc)),
        "objects.csv": _csv(objects_table(doc)),
    }
