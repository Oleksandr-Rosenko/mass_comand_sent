"""Реєстр активних запусків на рівні процесу застосунку (спільний для всіх вкладок і сесій).

Не дає запустити друге створення чи відправку на тій самій групі, поки йде перше (подвійний клік, друга вкладка,
оновлення сторінки), і дозволяє новій сесії «підхопити» запуск, що вже йде у фоні.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_jobs: dict[tuple, object] = {}


def _alive(job) -> bool:
    return job is not None and getattr(job, "running", False)


def claim(kind: str, server: str, group_id: int, job) -> object | None:
    """Реєструє job, якщо на цій групі немає активного запуску цього типу. Повертає активний чужий запуск або None."""
    key = (kind, server, int(group_id))
    with _lock:
        cur = _jobs.get(key)
        if _alive(cur) and cur is not job:
            return cur
        _jobs[key] = job
        return None


def active(kind: str, server: str, group_id: int | None = None) -> list:
    """Активні запуски цього типу на сервері (усі групи або одна)."""
    with _lock:
        return [j for (k, s, g), j in _jobs.items()
                if k == kind and s == server and (group_id is None or g == int(group_id)) and _alive(j)]
