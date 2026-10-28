"""Статистика по групах і збереження в гілку stats на (фейковому) GitHub."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from m2m_commands.github_store import GitHubError, GitHubStore
from m2m_commands.jobs import ANSWERED, ERROR, NO_ANSWER, SKIPPED
from m2m_commands.stats import command_dir, device_rows, files_for, group_summary, merge_run
from tests.fake_github import TOKEN, FakeGitHub

NAME, TEXT = "NTP", "runcmd:@com_tsync_disallow:1"
T0 = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
GROUP = {"id": 7, "name": "Генератори", "deviceIds": [1, 2, 3, 4]}


def row(did, status, answer="", minutes=0, detail="", sent=True):
    return {"id": did, "name": f"Генератор {did}", "imei": f"86{did}", "status": status, "detail": detail,
            "answer": answer, "last_message": T0, "sent_at": T0 + timedelta(minutes=minutes) if sent else None,
            "answered_at": T0 + timedelta(minutes=minutes, seconds=5) if status == ANSWERED else None}


RUN1 = [row(1, ANSWERED, "TSYNC is now allowed"), row(2, NO_ANSWER, detail="на зв'язок не виходив"),
        row(3, ERROR, detail="403 — немає права"), row(4, SKIPPED, detail="Немає команди «NTP»", sent=False)]
RUN2 = [row(2, ANSWERED, "TSYNC is now allowed", minutes=30), row(1, NO_ANSWER, minutes=30)]


def merged():
    doc = merge_run(None, name=NAME, text=TEXT, group=GROUP, run_id="r1", started_at=T0, rows=RUN1)
    return merge_run(doc, name=NAME, text=TEXT, group=GROUP, run_id="r2", started_at=T0 + timedelta(minutes=30),
                     rows=RUN2)


def test_merge_accumulates_and_accepted_sticks():
    g = merged()["groups"]["7"]
    s = group_summary(g)
    assert (s["objects"], s["accepted"], s["not_accepted"], s["runs"]) == (4, 2, 2, 2)
    assert g["devices"]["1"]["accepted"]  # у другому запуску не відповів, але раніше прийняв
    assert g["devices"]["2"]["accepted"] and g["devices"]["2"]["status"] == "Прийняв"
    assert g["devices"]["3"]["status"] == "Помилка відправки"
    assert g["devices"]["4"]["status"] == "Немає команди «NTP»"
    assert len(g["devices"]["1"]["sent_runs"]) == 2


def test_merge_is_idempotent():
    doc = merged()
    again = merge_run(json.loads(json.dumps(doc)), name=NAME, text=TEXT, group=GROUP, run_id="r2",
                      started_at=T0 + timedelta(minutes=30), rows=RUN2)
    assert again["groups"]["7"]["devices"]["1"]["sent_runs"] == ["r1", "r2"]
    assert group_summary(again["groups"]["7"]) == group_summary(doc["groups"]["7"])


def test_device_rows_accepted_first():
    rows = device_rows(merged()["groups"]["7"])
    assert [r["Прийняв"] for r in rows] == ["Так", "Так", "Ні", "Ні"]
    assert rows[0]["Об'єкт"] == "Генератор 2"  # свіжіша відповідь вище


def test_files_for_csv_tables():
    files = files_for(merged())
    groups = list(csv.DictReader(io.StringIO(files["groups.csv"])))
    assert groups[0]["Група"] == "Генератори" and groups[0]["Прийняли"] == "2" and groups[0]["% прийняли"] == "50"
    objects = list(csv.DictReader(io.StringIO(files["objects.csv"])))
    assert [o["Прийняв"] for o in objects] == ["Так", "Так", "Ні", "Ні"]
    assert json.loads(files["data.json"])["command"] == {"name": NAME, "text": TEXT}


@pytest.fixture()
def gh():
    fake = FakeGitHub()
    srv, url = fake.serve()
    yield fake, GitHubStore(TOKEN, fake.repo, "stats", api=url)
    srv.shutdown()


def test_github_save_creates_separate_branch_and_never_touches_main(gh):
    fake, store = gh
    main_before = fake.refs["main"]
    folder = command_dir(NAME, TEXT)
    store.update(folder, lambda d: merge_run(d, name=NAME, text=TEXT, group=GROUP, run_id="r1", started_at=T0,
                                             rows=RUN1), "stats")
    other = {"id": 8, "name": "Тест", "deviceIds": [9]}
    store.update(folder, lambda d: merge_run(d, name=NAME, text=TEXT, group=other, run_id="r3", started_at=T0,
                                             rows=[row(9, ANSWERED, "ok")]), "stats")
    assert fake.refs["main"] == main_before
    files = fake.files("stats")
    assert "app.py" not in files  # гілка лише зі статистикою
    assert set(files) == {f"{folder}/data.json", f"{folder}/groups.csv", f"{folder}/objects.csv"}
    docs = store.read_all()
    assert len(docs) == 1 and set(docs[0]["groups"]) == {"7", "8"}
    assert not any(m in ("PUT", "DELETE") for m, _ in fake.log)
    assert all("/heads/main" not in p for _, p in fake.log)


def test_github_retries_on_concurrent_commit(gh):
    fake, store = gh
    folder = command_dir(NAME, TEXT)
    store.update(folder, lambda d: merge_run(d, name=NAME, text=TEXT, group=GROUP, run_id="r1", started_at=T0,
                                             rows=RUN1), "stats")
    fake.race_once = True
    store.update(folder, lambda d: merge_run(d, name=NAME, text=TEXT, group=GROUP, run_id="r2",
                                             started_at=T0 + timedelta(minutes=30), rows=RUN2), "stats")
    files = fake.files("stats")
    assert "stats/other.txt" in files  # чужий коміт не затерто
    doc = json.loads(files[f"{folder}/data.json"])
    assert group_summary(doc["groups"]["7"])["accepted"] == 2


def test_github_bad_token_and_main_branch_refused(gh):
    fake, store = gh
    bad = GitHubStore("wrong", fake.repo, "stats", api=store.api)
    with pytest.raises(GitHubError, match="GITHUB_TOKEN"):
        bad.head()
    with pytest.raises(GitHubError, match="main"):
        GitHubStore(TOKEN, fake.repo, "main", api=store.api)
