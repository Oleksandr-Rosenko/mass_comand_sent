"""Перевірка клієнта й завдань на фейковому API (без мережі назовні)."""

from __future__ import annotations

import time

import pytest

from m2m_commands.client import M2MClient
from m2m_commands.errors import ApiError
from m2m_commands.jobs import (
    ANSWERED, CREATED, ERROR, NO_ANSWER, ONLINE, QUEUED, SKIPPED, STALE, TODAY, CreateJob, Device, DispatchJob,
    LoadCfg, ScanJob, activity, by_priority, clean_message, find_by_name,
)
from tests.fake_api import FakeM2M

NAME, TEXT = "NTP", "runcmd:@com_tsync_disallow:1"


def connect(api: FakeM2M) -> tuple[M2MClient, object]:
    srv, url = api.serve()
    cl = M2MClient(url, retries=2)
    cl.login("user@example.com", "demo123")
    return cl, srv


def devices_of(cl: M2MClient, gid=7) -> list[Device]:
    return [Device.from_api(d) for d in cl.group_devices(gid)]


def scan(cl, devs, cfg):
    job = ScanJob(cl, {"id": 7, "name": "Генератори"}, devs, cfg)
    job.start()
    job.wait(60)
    assert job.status == "done", job.message
    return job


def test_login_wrong_password():
    srv, url = FakeM2M(n_devices=2).serve()
    with pytest.raises(ApiError, match="Невірний"):
        M2MClient(url).login("a@b.c", "wrong-pass")
    srv.shutdown()


def test_token_refresh_on_401():
    api = FakeM2M(n_devices=5, expire_token_after=3)
    cl, srv = connect(api)
    for _ in range(10):
        assert len(cl.group_devices(7)) == 5
    assert api.token_n > 1
    srv.shutdown()


def test_scan_and_create_534_in_batches_without_duplicates():
    no_create, have_cmd, other = set(range(4, 534, 41)), set(range(3, 534, 33)), {44, 144, 244}
    api = FakeM2M(no_create=no_create, have_cmd=have_cmd, other_text=other,
                  flaky_create=(10, 60), uncertain_create=(25, 125))
    cl, srv = connect(api)
    devs = devices_of(cl)
    assert len(devs) == 534
    cfg = LoadCfg(batch_size=50, pause=0.05, workers=4)
    sc = scan(cl, devs, cfg)
    assert sc.batch_total == 11
    assert dict(sc.names())[NAME] == len(have_cmd | other)
    assert dict(sc.variants(NAME)) == {TEXT: len(have_cmd), TEXT + "_OLD": len(other)}

    job = CreateJob(cl, {"id": 7, "name": "Генератори"}, devs, NAME, TEXT, cfg, scan=sc)
    job.start()
    job.wait(120)
    assert job.status == "done", job.message
    c = job.counts()
    no_right = len(no_create)
    have = len((have_cmd | other) - no_create)
    created = 534 - no_right - have
    assert c.get(SKIPPED) == no_right + have
    assert c.get(CREATED) == created
    assert c.get(ERROR, 0) == 0
    assert api.max_active <= 4  # не більше паралельних запитів, ніж задано
    # жодного дубля: на кожному об'єкті рівно одна «NTP» (крім тих, де вже була зі старим текстом)
    for did, cmds in api.commands.items():
        assert len(find_by_name([type("C", (), {"name": c["description"]})() for c in cmds], NAME)) <= 1, did
    # збій 500 після фактичного створення не призвів до повторного POST
    assert api.create_calls[25] == 1 and job.rows[25]["status"] == CREATED
    assert "перевірено після збою" in job.rows[25]["detail"]
    # 503 повторено й створено
    assert api.create_calls[10] == 2 and job.rows[10]["status"] == CREATED
    # перевірка групи оновилась: створені команди видно для вкладки відправки
    assert dict(sc.variants(NAME))[TEXT] == len(have_cmd) + created
    srv.shutdown()


def test_create_stop_and_retry():
    api = FakeM2M(n_devices=120)
    cl, srv = connect(api)
    devs = devices_of(cl)
    job = CreateJob(cl, {"id": 7, "name": "Г"}, devs, NAME, TEXT, LoadCfg(batch_size=20, pause=0.3, workers=2))
    job.start()
    time.sleep(0.4)
    job.stop()
    job.wait(30)
    assert job.status == "stopped"
    left = job.counts().get(QUEUED, 0)
    assert 0 < left < 120
    job.retry_failed()
    job.wait(60)
    assert job.status == "done"
    assert job.counts().get(CREATED) == 120
    assert all(n == 1 for n in api.create_calls.values())
    srv.shutdown()


@pytest.mark.parametrize("ascending", [False, True])
def test_dispatch_and_results(ascending):
    api = FakeM2M(n_devices=60, have_cmd=range(1, 61), silent=(5, 6), dispatch_403=(7,), ascending=ascending,
                  answer_delay=0.3)
    cl, srv = connect(api)
    devs = {d.id: d for d in devices_of(cl)}
    targets = [(devs[i], next(c["id"] for c in api.commands[i] if c["description"] == NAME)) for i in range(1, 61)]
    job = DispatchJob(cl, {"id": 7, "name": "Г"}, NAME, TEXT, targets, [], LoadCfg(batch_size=25, pause=0.05, workers=4),
                      wait_minutes=0.08, poll_every=0.5)
    job.poll_every = 0.5  # мінімум у коді — 3 с, для тесту швидше
    job.start()
    job.wait(60)
    c = job.counts()
    assert c.get(ANSWERED) == 57, c
    assert c.get(NO_ANSWER) == 2 and c.get(ERROR) == 1
    assert job.rows[1]["answer"] == "TSYNC is now allowed from NTP NITZ"
    assert job.rows[7]["detail"].startswith("403")
    assert sorted(job.not_answered()) == [5, 6, 7]
    assert len(api.dispatched) == 59  # кожному рівно один раз
    # старі результати з історії (до старту) не зараховані
    assert job.baseline_id == 250
    srv.shutdown()


def test_late_answer_picked_up_by_manual_poll():
    api = FakeM2M(n_devices=3, have_cmd=(1, 2, 3), answer_delay=2.5)
    cl, srv = connect(api)
    devs = {d.id: d for d in devices_of(cl)}
    targets = [(devs[i], api.commands[i][-1]["id"]) for i in (1, 2, 3)]
    job = DispatchJob(cl, {"id": 7, "name": "Г"}, NAME, TEXT, targets, [], LoadCfg(pause=0), wait_minutes=0.01)
    job.start()
    job.wait(30)
    assert job.counts().get(NO_ANSWER) == 3
    time.sleep(2.8)
    assert job.poll() == 3
    assert job.counts().get(ANSWERED) == 3
    srv.shutdown()


def test_stop_waiting():
    api = FakeM2M(n_devices=4, have_cmd=(1, 2, 3, 4), silent=(1, 2, 3, 4))
    cl, srv = connect(api)
    devs = {d.id: d for d in devices_of(cl)}
    targets = [(devs[i], api.commands[i][-1]["id"]) for i in (1, 2, 3, 4)]
    job = DispatchJob(cl, {"id": 7, "name": "Г"}, NAME, TEXT, targets, [], LoadCfg(pause=0), wait_minutes=10)
    job.start()
    for _ in range(100):
        if job.phase == "waiting":
            break
        time.sleep(0.05)
    job.stop_waiting()
    job.wait(10)
    assert job.status == "stopped"
    assert job.counts().get(NO_ANSWER) == 4
    assert job.rows[1]["detail"].startswith("Очікування зупинено")
    srv.shutdown()


def test_waiting_shows_who_came_online():
    """Під час очікування перечитуємо «останнє повідомлення»: видно, хто вийшов на зв'язок, але не відповів."""
    from datetime import datetime, timezone
    api = FakeM2M(n_devices=3, have_cmd=(1, 2, 3), silent=(1, 2, 3))
    cl, srv = connect(api)
    devs = {d.id: d for d in devices_of(cl)}
    targets = [(devs[i], api.commands[i][-1]["id"]) for i in (1, 2, 3)]
    job = DispatchJob(cl, {"id": 7, "name": "Г"}, NAME, TEXT, targets, [], LoadCfg(pause=0), wait_minutes=0.1,
                      activity_every=0.5)
    job.poll_every = 0.3
    job.start()
    for _ in range(100):
        if job.phase == "waiting":
            break
        time.sleep(0.05)
    time.sleep(0.2)
    api.devices[2]["lastMessageDate"] = datetime.now(timezone.utc).isoformat()  # об'єкт 2 «прокинувся»
    for _ in range(40):
        if job.waiting_summary()["woke"] == 1:
            break
        time.sleep(0.1)
    assert job.waiting_summary() == {"waiting": 3, "woke": 1, "silent": 2}
    job.wait(30)
    assert "виходив на зв'язок, але не відповів" in job.rows[2]["detail"]
    assert "на зв'язок не виходив" in job.rows[1]["detail"]
    srv.shutdown()


def test_priority_online_first():
    api = FakeM2M(n_devices=30, have_cmd=range(1, 31), answer_delay=0.05)
    cl, srv = connect(api)
    devs = {d.id: d for d in devices_of(cl)}
    targets = by_priority([(devs[i], api.commands[i][-1]["id"]) for i in range(1, 31)])
    acts = [activity(d.last_message, 10) for d, _ in targets]
    # онлайн → за добу → давніше, без перемішування
    assert acts == sorted(acts, key=[ONLINE, TODAY, STALE].index)
    job = DispatchJob(cl, {"id": 7, "name": "Г"}, NAME, TEXT, targets, [], LoadCfg(batch_size=10, pause=0, workers=1),
                      wait_minutes=0.02, online_min=10)
    job.start()
    job.wait(30)
    sent_order = [did for did, _ in api.dispatched]
    assert sent_order == [d.id for d, _ in targets]
    stats = job.activity_stats()
    assert stats[STALE]["sent"] == len([i for i in range(1, 31) if i % 9 == 0])
    assert sum(v["answered"] for v in stats.values()) == 30
    srv.shutdown()


def test_activity_buckets():
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    assert activity(now - timedelta(minutes=9), 10, now) == ONLINE
    assert activity(now - timedelta(minutes=11), 10, now) == TODAY
    assert activity(now - timedelta(hours=30), 10, now) == STALE
    assert activity(None, 10, now) == STALE


ALLOWED = {
    ("POST", "/api/login"),
    ("POST", "/api/token/refresh"),
    ("GET", "/api/device/groups/lightweight/all"),
    ("GET", "/api/group/{id}/devices"),
    ("GET", "/api/device/{id}/commands"),
    ("GET", "/api/notification/history"),
    ("POST", "/api/device/{id}/commands"),             # створення команди
    ("POST", "/api/device/{id}/command/{id}/dispatch"),  # відправка команди
}


def test_full_cycle_makes_only_allowed_requests():
    """Повний цикл не робить нічого, крім читання, створення команди й відправки: жодних PUT/DELETE/PATCH."""
    import re as _re
    api = FakeM2M(n_devices=40, have_cmd=(3,), other_text=(4,), no_create=(5,), flaky_create=(6,),
                  uncertain_create=(7,), dispatch_403=(8,), silent=(9,), expire_token_after=50)
    cl, srv = connect(api)
    cl.groups()
    devs = devices_of(cl)
    cfg = LoadCfg(batch_size=10, pause=0, workers=4)
    sc = scan(cl, devs, cfg)
    cj = CreateJob(cl, {"id": 7, "name": "Г"}, devs, NAME, TEXT, cfg, scan=sc)
    cj.start()
    cj.wait(60)
    targets = [(d, c.id) for d in devs for c in sc.commands.get(d.id, []) if c.name == NAME and c.text == TEXT]
    dj = DispatchJob(cl, {"id": 7, "name": "Г"}, NAME, TEXT, targets, [], cfg, wait_minutes=0.02)
    dj.start()
    dj.wait(60)
    dj.poll()
    seen = {(m, _re.sub(r"/\d+", "/{id}", p)) for m, p in api.log}
    assert seen <= ALLOWED, seen - ALLOWED
    assert not any(m in ("PUT", "DELETE", "PATCH") for m, _ in api.log)
    # нічого не змінено в чужих командах: стара «NTP» з іншим текстом і «01. GETVER» лишились як були
    assert [c["attributes"]["data"] for c in api.commands[4] if c["description"] == NAME] == [TEXT + "_OLD"]
    assert all(api.commands[i][0]["description"] == "01. GETVER" for i in api.commands)
    # команда надіслана лише тим, у кого є «NTP» з потрібним текстом, і кожному рівно раз
    sent_to = [d for d, _ in api.dispatched]
    assert len(sent_to) == len(set(sent_to)) == len(targets) - 1  # об'єкт 8 відхилено сервером (403)
    assert 4 not in sent_to and 8 not in sent_to
    assert dj.rows[8]["status"] == ERROR
    srv.shutdown()


def test_dispatch_503_is_not_resent():
    api = FakeM2M(n_devices=3, have_cmd=(1, 2, 3), silent=(1, 2, 3))
    api.dispatch_503 = {2}
    cl, srv = connect(api)
    devs = {d.id: d for d in devices_of(cl)}
    targets = [(devs[i], api.commands[i][-1]["id"]) for i in (1, 2, 3)]
    job = DispatchJob(cl, {"id": 7, "name": "Г"}, NAME, TEXT, targets, [], LoadCfg(pause=0), wait_minutes=0.01)
    job.start()
    job.wait(30)
    assert [d for d, _ in api.dispatched].count(2) == 1  # не надіслали вдруге
    assert "не підтвердив" in job.rows[2]["detail"]
    srv.shutdown()


def test_clean_message():
    assert clean_message('Готово <a href="/x">тут</a>  ок') == "Готово тут ок"
