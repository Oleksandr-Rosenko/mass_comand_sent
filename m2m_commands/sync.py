"""Звірка статистики з історією сповіщень: пізні «Результати команди» зараховуються об'єктам.

Використовується і з застосунку (кнопка / автоматично), і окремим скриптом за розкладом (GitHub Actions):

    M2M_URL=https://my.m2m.eu M2M_EMAIL=... M2M_PASSWORD=... GITHUB_TOKEN=... GITHUB_REPO=owner/repo \\
        python -m m2m_commands.sync

Скрипт лише читає M2M (історію сповіщень) і пише в гілку статистики на GitHub. Команд не створює й не надсилає.
"""

from __future__ import annotations

import copy
import os
import sys

from .client import M2MClient
from .errors import ApiError
from .github_store import GitHubError, GitHubStore
from .stats import apply_history, history_since


def sync_docs(client: M2MClient, store: GitHubStore, docs: dict[str, dict]) -> tuple[int, str]:
    """Дозараховує відповіді з історії в кожну команду (папку) статистики. Повертає (змін, помилка)."""
    since_all = [x for d in docs.values() if (x := history_since(d))]
    if not since_all:
        return 0, ""
    try:
        items = client.results_since(min(since_all))
    except ApiError as e:
        return 0, f"Не вдалося прочитати історію сповіщень: {e}"
    total, err = 0, ""
    for folder, doc in docs.items():
        if apply_history(copy.deepcopy(doc), items) == 0:
            continue  # нічого нового — не комітимо
        res: dict = {}

        def mutate(d, res=res):
            res["n"] = apply_history(d, items) if d else 0
            return d

        try:
            store.update(folder, mutate, f"Статистика «{doc['command']['name']}»: відповіді з історії сповіщень")
            total += res.get("n", 0)
        except GitHubError as e:
            err = str(e)
    return total, err


def main() -> int:
    env = os.environ.get
    missing = [k for k in ("M2M_EMAIL", "M2M_PASSWORD", "GITHUB_TOKEN") if not env(k)]
    if missing:
        print("Не задано: " + ", ".join(missing))
        return 2
    client = M2MClient(env("M2M_URL") or "https://my.m2m.eu")
    client.login(env("M2M_EMAIL"), env("M2M_PASSWORD"))
    store = GitHubStore(env("GITHUB_TOKEN"), env("GITHUB_REPO") or "", env("GITHUB_BRANCH") or "stats",
                        api=env("GITHUB_API") or "https://api.github.com")
    ref = store.head()
    docs = {f: d for f in store.list_folders(ref) if (d := store.read_doc(f, ref))}
    n, err = sync_docs(client, store, docs)
    print(f"Дозараховано відповідей з історії: {n}")
    if err:
        print(err)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
