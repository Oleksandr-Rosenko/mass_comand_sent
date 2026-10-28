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
        print("Не задано секрети: " + ", ".join(missing))
        return 2
    url = env("M2M_URL") or "https://my.m2m.eu"
    email = env("M2M_EMAIL").strip()
    print(f"1/4 Вхід у M2M ({url}) як {email[:3]}***")
    client = M2MClient(url)
    try:
        client.login(email, env("M2M_PASSWORD").strip())
    except ApiError as e:
        print(f"   Помилка входу: {e}. Перевірте M2M_EMAIL / M2M_PASSWORD (без лапок і пробілів); "
              "якщо на акаунті увімкнена двофакторна перевірка — скрипт не зможе увійти.")
        return 1
    print("   OK")
    print(f"2/4 Читаю статистику з GitHub ({env('GITHUB_REPO')}, гілка {env('GITHUB_BRANCH') or 'stats'})")
    try:
        store = GitHubStore(env("GITHUB_TOKEN"), env("GITHUB_REPO") or "", env("GITHUB_BRANCH") or "stats",
                            api=env("GITHUB_API") or "https://api.github.com")
        ref = store.head()
        docs = {f: d for f in store.list_folders(ref) if (d := store.read_doc(f, ref))}
    except GitHubError as e:
        print(f"   Помилка GitHub: {e}")
        return 1
    print(f"   OK: команд у статистиці — {len(docs)}")
    if not docs:
        print("Статистики ще немає — нічого звіряти.")
        return 0
    print("3/4 Читаю історію «Результат команди» і звіряю")
    n, err = sync_docs(client, store, docs)
    print(f"4/4 Дозараховано відповідей з історії: {n}")
    if err:
        print(f"   Помилка: {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
