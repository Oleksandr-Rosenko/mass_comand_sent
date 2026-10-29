"""Streamlit-застосунок «Масові команди M2M»: створення команди на групі об'єктів і масова відправка з результатами."""

from __future__ import annotations

import html
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from m2m_commands.client import NAME_MAX, TEXT_MAX, M2MClient
from m2m_commands.errors import ApiError
from m2m_commands import registry
from m2m_commands.github_store import GitHubError, GitHubStore
from m2m_commands.sync import sync_docs
from m2m_commands.jobs import (
    ACTIVITY_LABELS, ANSWERED, CREATED, ERROR, LABELS, NO_ANSWER, NOT_SENT, ONLINE, PERM_CREATE, PERM_EXECUTE,
    QUEUED, RUNNING, SENT, SKIPPED, STALE, TODAY, CreateJob, Device, DispatchJob, LoadCfg, ScanJob, activity,
    by_priority, Command, days_ago, describe_error, find_by_name, now_utc, parse_dt,
)
from m2m_commands.stats import (apply_history, command_dir, device_index, fmt_local, group_view,
                                history_since, merge_run)

try:
    LOCAL_TZ = ZoneInfo("Europe/Kyiv")
except Exception:  # noqa: BLE001 — старіша база часових зон
    LOCAL_TZ = ZoneInfo("Europe/Kiev")

st.set_page_config(page_title="Масові команди M2M", layout="wide")
st.markdown(
    """
    <style>
    .block-container {max-width: 1080px; padding-top: 2.6rem;}
    [data-testid="stMetricValue"] {font-size: 1.9rem;}
    section[data-testid="stSidebar"] {width: 240px !important; min-width: 240px !important;}
    [data-testid="stSidebar"] [data-testid="stNumberInput"] input {background: #FFFFFF;}
    </style>
    """,
    unsafe_allow_html=True,
)

S = st.session_state
for k, v in {"client": None, "groups": [], "devices": {}, "scans": {}, "create_job": None, "create_stage": "form",
             "dispatch_job": None, "dispatch_stage": "form", "prev_dispatch": {}, "runs": {}, "last_name": "",
             "d_wait": 15, "d_online": 5, "d_priority": True,
             "d_skip_acc": True}.items():
    S.setdefault(k, v)
# Streamlit забуває значення полів, які в цьому проході не намальовані (напр., форма під час прогресу).
# Перезапис зберігає їх між екранами.
TABS = ["1 · Створення команди", "2 · Відправка і результати", "3 · Статистика"]

# Перехід між вкладками кнопкою: запам'ятовуємо, куди йти, і застосовуємо на початку наступного проходу,
# поки віджети ще не створені (інакше Streamlit не дозволить змінити їхні значення).
_nav = S.pop("nav_pending", None)
if _nav:
    S["nav"] = _nav["tab"]
    if _nav.get("gid") is not None:
        S["g_dispatch"] = _nav["gid"]
        S["auto_scan"] = _nav["gid"]  # на кроці 2 одразу прочитати команди групи, без зайвого кліку
    if _nav.get("name"):
        S["last_name"] = _nav["name"]
    if _nav.get("text") is not None:
        S["go_text"] = _nav["text"]
    job_ = S.get("dispatch_job")
    if _nav["tab"] == TABS[1] and not (job_ is not None and job_.running):
        S["dispatch_stage"] = "form"

for k in ("g_create", "c_name", "c_text", "g_dispatch", "d_wait", "d_online", "d_mode", "d_priority", "d_skip_acc"):
    if k in S:
        S[k] = S[k]

ACT_COLORS = {ONLINE: "#2E9E5B", TODAY: "#E8A93B", STALE: "#B8BFCC"}
ACT_TEXT = {ONLINE: "#1E7A43", TODAY: "#8A5A00", STALE: "#5F6573"}

STATUS_COLORS = {
    LABELS[CREATED]: ("#DFF3E4", "#14632B"), LABELS[ANSWERED]: ("#DFF3E4", "#14632B"),
    LABELS[SKIPPED]: ("#ECEEF2", "#454A56"), LABELS[NOT_SENT]: ("#ECEEF2", "#454A56"),
    LABELS[ERROR]: ("#FDE2E1", "#9B1C1C"), LABELS[NO_ANSWER]: ("#FDE2E1", "#9B1C1C"),
    LABELS[SENT]: ("#FFF1CC", "#6E4600"), LABELS[QUEUED]: ("#FFF1CC", "#6E4600"),
    LABELS[RUNNING]: ("#E1EEFB", "#155A91"),
    "Так": ("#DFF3E4", "#14632B"), "Ні": ("#FDE2E1", "#9B1C1C"),
}


# ---------------------------------------------------------------- допоміжне
def secret(key: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(key, default))
    except Exception:  # noqa: BLE001 — secrets.toml може бути відсутнім
        return default


# Команда за замовчуванням — підставляється у форму створення; змінюється в Secrets без правок коду.
DEFAULT_NAME = secret("DEFAULT_COMMAND_NAME", "NTP")
DEFAULT_TEXT = secret("DEFAULT_COMMAND_TEXT", "runcmd:@com_tsync_disallow:1")
S.setdefault("c_name", DEFAULT_NAME)
S.setdefault("c_text", DEFAULT_TEXT)


def fmt_n(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def plural(n: int, forms: tuple[str, str, str]) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


OBJ = ("об'єкт", "об'єкти", "об'єктів")
OBJ_LOC = ("об'єкті", "об'єктах", "об'єктах")


def n_obj(n: int, forms=OBJ) -> str:
    return f"{fmt_n(n)} {plural(n, forms)}"


def fmt_time(dt: datetime | None, with_date: bool = False) -> str:
    if not dt:
        return "—"
    return dt.astimezone(LOCAL_TZ).strftime("%d.%m.%Y %H:%M:%S" if with_date else "%H:%M:%S")


def ago(dt: datetime | None) -> str:
    if not dt:
        return "—"
    sec = (now_utc() - dt).total_seconds()
    if sec < 90:
        return "щойно"
    if sec < 3600:
        return f"{int(sec // 60)} хв тому"
    if sec < 86400:
        return f"{int(sec // 3600)} год тому"
    return f"{int(sec // 86400)} дн тому"


def seen_cell(k: str, dt: datetime | None) -> str:
    """Активність + час в одній комірці: «● Онлайн · 3 хв тому», «● Сьогодні · 16:05», «● 5 дн тому · 23.10 14:20»."""
    if dt is None:
        return "● ніколи"
    loc = dt.astimezone(LOCAL_TZ)
    if k == ONLINE:
        return f"● {ACTIVITY_LABELS[ONLINE]} · {ago(dt)}"
    if k == TODAY:
        return f"● {ACTIVITY_LABELS[TODAY]} · {loc:%H:%M}"
    return f"● {days_ago(dt)} · {loc:%d.%m %H:%M}"


def last_seen(dt: datetime | None) -> str:
    """«28.10 14:52 · 7 хв тому»."""
    if not dt:
        return "—"
    return f"{dt.astimezone(LOCAL_TZ).strftime('%d.%m %H:%M')} · {ago(dt)}"


def mmss(sec: float | None) -> str:
    if sec is None:
        return "—"
    sec = int(sec)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def host_label(url: str) -> str:
    return urlparse(url if "//" in url else f"//{url}").hostname or url


# Навантаження — фіксоване, перевірене на групі з 534 об'єктів; підходить і для 100, і для 500+:
# пачки по 50 об'єктів, у пачці до 4 запитів одночасно, 2 с паузи між пачками.
BATCH_SIZE, WORKERS, PAUSE_S, RETRIES = 50, 4, 2.0, 2
# Відправка йде через той самий сервіс команд (gps.m2m.eu), що віддавав 502 під навантаженням, — обережніше:
DISPATCH_BATCH, DISPATCH_WORKERS, DISPATCH_PAUSE_S = 25, 1, 10.0   # по одній команді за раз
BIG_GROUP = 1000          # від скількох об'єктів таблиця під час запуску спрощується
POLL_EVERY_S = 60        # як часто читати нові «Результати команд» (лише нове з історії)
ACTIVITY_EVERY_S = 300   # як часто під час очікування оновлювати «останнє повідомлення» (1 запит на групу)
ACTIVITY_EVERY_BIG_S = 900  # для груп >BIG_GROUP — список усіх об'єктів групи важкий, тому рідше


def activity_every(n_objects: int) -> int:
    return ACTIVITY_EVERY_BIG_S if n_objects > BIG_GROUP else ACTIVITY_EVERY_S


def go_to(tab: int, gid: int | None = None, name: str | None = None, text: str | None = None) -> None:
    """Перейти на вкладку (0, 1, 2) з уже вибраними групою й командою."""
    S.nav_pending = {"tab": TABS[tab], "gid": gid, "name": name, "text": text}
    st.rerun()


def dispatch_cfg() -> LoadCfg:
    return LoadCfg(batch_size=DISPATCH_BATCH, pause=DISPATCH_PAUSE_S, workers=DISPATCH_WORKERS)


def load_cfg() -> LoadCfg:
    return LoadCfg(batch_size=BATCH_SIZE, pause=PAUSE_S, workers=WORKERS)


def any_running() -> bool:
    jobs = [S.create_job, S.dispatch_job, *S.scans.values()]
    return any(j is not None and j.running for j in jobs)


def styled(df: pd.DataFrame, col: str = "Статус"):
    if not len(df) or col not in df.columns:
        return df

    def paint(v):
        bg, fg = STATUS_COLORS.get(str(v).split(" · ")[0], ("", ""))
        return f"background-color: {bg}; color: {fg}; font-weight: 600" if bg else ""

    def paint_act(v):
        if not isinstance(v, str) or not v.startswith("●"):
            return ""
        k = ONLINE if v.startswith(f"● {ACTIVITY_LABELS[ONLINE]}") else TODAY if v.startswith(
            f"● {ACTIVITY_LABELS[TODAY]}") else STALE
        return f"color: {ACT_TEXT[k]}; font-weight: 600"

    sty = df.style.map(paint, subset=[col])
    for c in ("Активність", "Останнє повідомлення"):
        if c in df.columns:
            sty = sty.map(paint_act, subset=[c])
    return sty


def act_label(k: str, last_message: datetime | None = None) -> str:
    """● Онлайн / ● Сьогодні / ● вчора, ● 5 дн тому, ● ніколи."""
    return f"● {days_ago(last_message)}" if k == STALE else f"● {ACTIVITY_LABELS[k]}"


def activity_bar(counts: dict[str, int], online_min: int) -> None:
    """Смуга розподілу об'єктів за давністю останнього повідомлення + легенда."""
    total = sum(counts.values()) or 1
    names = {ONLINE: f"Онлайн (≤ {online_min} хв)", TODAY: "Сьогодні", STALE: "Раніше / ніколи"}
    segs = "".join(f"<div style='width:{counts.get(k, 0) / total * 100:.3f}%;background:{ACT_COLORS[k]}'></div>"
                   for k in ACTIVITY_LABELS if counts.get(k))
    legend = "".join(
        f"<span style='display:inline-flex;align-items:center;gap:6px;margin:0 20px 4px 0'>"
        f"<span style='width:10px;height:10px;border-radius:50%;background:{ACT_COLORS[k]}'></span>"
        f"{names[k]} — <b>{fmt_n(counts.get(k, 0))}</b></span>" for k in ACTIVITY_LABELS)
    st.markdown(
        f"<div role='img' aria-label='Розподіл об&#39;єктів за активністю' style='display:flex;height:12px;"
        f"border-radius:6px;overflow:hidden;background:#E6EAF1;margin:2px 0 10px'>{segs}</div>"
        f"<div style='font-size:14px;color:#31333F;display:flex;flex-wrap:wrap'>{legend}</div>",
        unsafe_allow_html=True,
    )


# Ширина колонок у таблицях: вузькі службові колонки компактні, текст відповіді — широкий.
COL_WIDTHS = {
    "№": 36, "Прийняв": 64, "Статус": 140, "Об'єкт": 115, "IMEI": 125, "Останнє повідомлення": 150,
    "Коли відповів": 125, "Разів надіслано": 110, "Деталі": 260,
    "Відповідь трекера / деталі": 330, "Відповідь / причина": 330,
}


COL_HELP = {
    "Разів надіслано": "Скільки запусків надсилали команду цьому об'єкту (у цій групі). 1 — один раз; "
                       "більше — надсилали повторно, бо раніше не було відповіді. 0 — не надсилали.",
    "Відповідь / причина": "Для тих, хто прийняв, — відповідь трекера; для решти — чому ні і що робити. "
                           "Повні деталі — у «CSV групи».",
}


def col_cfg(df: pd.DataFrame) -> dict:
    return {c: st.column_config.Column(width=w, help=COL_HELP.get(c)) for c, w in COL_WIDTHS.items()
            if c in df.columns}


def drop_empty(df: pd.DataFrame, cols=("IMEI",)) -> pd.DataFrame:
    """Ховає колонки, порожні в усіх рядках (напр., IMEI, якщо API його не віддає)."""
    empty = [c for c in cols if c in df.columns and len(df) and not df[c].astype(str).str.strip().astype(bool).any()]
    return df.drop(columns=empty) if empty else df


def csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False, sep=";").encode("utf-8-sig")  # utf-8-sig + ";" — відкривається в Excel


# ---------------------------------------------------------------- дані групи
def group_by_id(gid: int) -> dict:
    return next((g for g in S.groups if g["id"] == gid), {"id": gid, "name": f"Група {gid}", "deviceIds": []})


def group_select(key: str) -> int | None:
    if not S.groups:
        st.info("Груп об'єктів не знайдено або немає доступу до жодної.")
        return None
    labels = {g["id"]: f"{g['name']} — {n_obj(len(g['deviceIds']))}" for g in S.groups}
    return st.selectbox("Група об'єктів", list(labels), format_func=labels.get, key=key, index=None,
                        placeholder="Оберіть групу")


def get_devices(gid: int, reload: bool = False) -> list[Device] | None:
    if reload or gid not in S.devices:
        try:
            with st.spinner("Завантажую об'єкти групи…"):
                S.devices[gid] = [Device.from_api(d) for d in S.client.group_devices(gid) if d.get("id") is not None]
        except ApiError as e:
            st.error(f"Не вдалося завантажити об'єкти групи: {e}")
            return None
    return S.devices[gid]


def start_scan(gid: int) -> None:
    devices = S.devices.get(gid) or []
    old = S.scans.get(gid)
    if old is not None and old.running:
        return
    job = ScanJob(S.client, group_by_id(gid), devices, load_cfg())
    S.scans[gid] = job
    job.start()


def dup_warning(scan: ScanJob, devices: list[Device], name: str, key: str) -> None:
    """Попередження, якщо на частині об'єктів кілька команд з цим ім'ям (напр., після двох паралельних створень)."""
    if not name:
        return
    dups = scan.duplicates(name)
    if not dups:
        return
    names = {d.id: d.name for d in devices}
    st.warning(f"На {n_obj(len(dups), OBJ_LOC)} є по кілька команд «{name}» (дублі). На відправку це не впливає — "
               "надсилається одна. Апка нічого не видаляє; за потреби приберіть зайві вручну на платформі.")
    with st.expander(f"Об'єкти з дублями «{name}» — {fmt_n(len(dups))}"):
        df = pd.DataFrame([{"Об'єкт": names.get(did, str(did)), "ID об'єкта": did,
                            "ID команд «" + name + "»": ", ".join(map(str, ids))} for did, ids in dups.items()])
        st.dataframe(df, hide_index=True, width="stretch")
        st.download_button("CSV дублів", csv_bytes(df), key=f"dups_{key}", file_name=f"dups_{name}.csv",
                           mime="text/csv")


def scan_block(gid: int, devices: list[Device], prefix: str, intro: str) -> ScanJob | None:
    """Кнопка/прогрес перевірки команд групи. Повертає завершену перевірку або None."""
    scan: ScanJob | None = S.scans.get(gid)
    if scan is None:
        st.caption(intro)
        st.button("Перевірити групу", key=f"{prefix}_scan", on_click=start_scan, args=(gid,),
                  disabled=not devices)
        return None
    if scan.running:
        @st.fragment(run_every=1)
        def live() -> None:
            pct = scan.processed / scan.total if scan.total else 1.0
            st.progress(min(1.0, pct), text=f"Читаю команди об'єктів: {fmt_n(scan.processed)} з {fmt_n(scan.total)}"
                                            f" · пачка {scan.batch_no} з {scan.batch_total}")
            if st.button("Зупинити перевірку", key=f"{prefix}_scan_stop"):
                scan.stop()
            if not scan.running:
                st.rerun()
        live()
        return None
    c1, c2 = st.columns([3, 1], vertical_alignment="center")
    msg = f"Команди перевірено о {fmt_time(scan.done_at)}"
    if scan.errors:
        msg += f" · не вдалося прочитати на {n_obj(len(scan.errors), OBJ_LOC)}"
    c1.caption(msg)
    c2.button("Оновити перевірку", key=f"{prefix}_rescan", on_click=start_scan, args=(gid,),
              disabled=any_running(), width="stretch")
    if scan.status == "stopped" and scan.processed < scan.total:
        st.warning(f"Перевірку зупинено: прочитано {fmt_n(scan.processed)} з {fmt_n(scan.total)}.")
        if st.button("Продовжити перевірку", key=f"{prefix}_scan_cont"):
            scan.start()
            st.rerun()
    if scan.status == "error":
        st.error(scan.message)
    return scan


# ---------------------------------------------------------------- екран входу
def login_page() -> None:
    _, mid, _ = st.columns([1, 1.25, 1])
    with mid:
        st.markdown("<div style='height: 4vh'></div>", unsafe_allow_html=True)
        st.title("M2M · Масові команди")
        st.caption("Створення і відправка команд на групи об'єктів")
        with st.form("login", border=False):
            url = st.text_input("Сервер", value=secret("M2M_URL", "https://my.m2m.eu"))
            email = st.text_input("Email", placeholder="name@company.ua")
            password = st.text_input("Пароль", type="password")
            ok = st.form_submit_button("Увійти", type="primary", width="stretch")
        st.caption("Ті самі дані, що й для входу в my.m2m.eu. Пароль ніде не зберігається.")
        if ok:
            if not (url.strip() and email.strip() and password):
                st.error("Заповніть сервер, email і пароль")
                return
            client = M2MClient(url.strip() if "//" in url else f"https://{url.strip()}")
            try:
                with st.spinner("Входжу…"):
                    client.login(email.strip(), password)
                    groups = client.groups()
            except ApiError as e:
                st.error(str(e))
                return
            S.client, S.groups = client, groups
            st.rerun()


# ---------------------------------------------------------------- бічна панель
def sidebar() -> None:
    client: M2MClient = S.client
    with st.sidebar:
        st.markdown("### M2M · Масові команди")
        st.markdown(
            "<div style='background:#FFFFFF;border-radius:8px;padding:12px 14px;font-size:14px;line-height:1.5;"
            "margin-bottom:8px'><div style='color:#555A66'>Сервер</div>"
            f"<div style='font-weight:600'>{html.escape(host_label(client.base_url))}</div>"
            "<div style='color:#555A66;margin-top:6px'>Користувач</div>"
            f"<div style='font-weight:600;word-break:break-all'>{html.escape(client.email or '')}</div></div>",
            unsafe_allow_html=True,
        )
        busy = any_running()
        client.retries = RETRIES
        st.caption(f"Перевірка й створення: пачки по {BATCH_SIZE}, до {WORKERS} одночасно, пауза {PAUSE_S:g} с. "
                   f"Відправка: пачки по {DISPATCH_BATCH}, до {DISPATCH_WORKERS} одночасно, пауза "
                   f"{DISPATCH_PAUSE_S:g} с. Якщо сервер захлинається — пауза збільшується сама.")
        if st.button("Вийти", disabled=busy, width="stretch"):
            for k in list(S.keys()):
                del S[k]
            st.rerun()
        if st.button("Оновити список груп", disabled=busy, width="stretch"):
            try:
                S.groups, S.devices, S.scans, S.owner_groups = client.groups(), {}, {}, None
            except ApiError as e:
                st.error(str(e))


# ---------------------------------------------------------------- вкладка 1: створення
def running_elsewhere(kind: str) -> None:
    """Показує запуски, що вже йдуть у фоні (з іншої вкладки чи до оновлення сторінки), і дає їх відкрити."""
    mine = S.create_job if kind == "create" else S.dispatch_job
    for j in registry.active(kind, S.client.base_url):
        if j is mine:
            continue
        what = "створення" if kind == "create" else "відправка"
        c1, c2 = st.columns([3, 1], vertical_alignment="center")
        c1.warning(f"На групі «{j.group['name']}» вже йде {what} «{j.name}» "
                   f"(запущено о {datetime.fromtimestamp(j.started_at).astimezone(LOCAL_TZ):%H:%M}). "
                   "Другий запуск на цій групі не стартує, поки цей не завершиться.")
        if c2.button("Показати його", key=f"attach_{kind}_{j.group['id']}", width="stretch"):
            if kind == "create":
                S.create_job, S.create_stage = j, "progress"
            else:
                S.dispatch_job, S.dispatch_stage = j, "progress"
            st.rerun()


def create_tab() -> None:
    job: CreateJob | None = S.create_job
    if job is not None and S.create_stage == "progress":
        create_progress(job)
        return
    running_elsewhere("create")

    gid = group_select("g_create")
    c1, c2 = st.columns(2)
    name = c1.text_input("Ім'я команди *", max_chars=NAME_MAX, key="c_name", placeholder="Наприклад, NTP").strip()
    c2.text_input("Тип команди", value="Надіслати довільне повідомлення (custom_msg)", disabled=True)
    text = st.text_area("Текст команди *", max_chars=TEXT_MAX, key="c_text", height=100,
                        placeholder="Наприклад, runcmd:@com_tsync_disallow:1")
    st.caption("Якщо на об'єкті вже є команда з таким ім'ям — пропускаємо, дубль не створюємо. Канал — Virtual.")
    if gid is None:
        return
    devices = get_devices(gid)
    if devices is None:
        return

    scan = scan_block(gid, devices, "c", "Перевірка прочитає команди кожного об'єкта й покаже, де команда з таким "
                                         "ім'ям уже є. Можна й одразу створювати — дублі все одно перевіряються.")
    if scan and not scan.running:
        dup_warning(scan, devices, name, "c")
    no_right = [d for d in devices if d.can_create is False]
    allowed = [d for d in devices if d.can_create is not False]
    have = [d for d in allowed if scan and name and find_by_name(scan.commands.get(d.id, []), name)]
    to_create = len(allowed) - len(have)

    if scan:
        m = st.columns(4)
        m[0].metric("У групі", fmt_n(len(devices)), border=True)
        m[1].metric("Буде створено", fmt_n(to_create) if name else "—", border=True)
        m[2].metric(f"Вже мають «{name}»" if name else "Вже мають команду", fmt_n(len(have)) if name else "—",
                    border=True)
        m[3].metric("Без права на команди", fmt_n(len(no_right)), border=True)
        with st.expander(f"Об'єкти групи — {n_obj(len(devices))}"):
            rows = []
            for d in devices:
                same = find_by_name(scan.commands.get(d.id, []), name) if name else []
                if d.id in scan.errors:
                    state = f"Не прочитано: {scan.errors[d.id]}"
                elif d.can_create is False:
                    state = "Пропустимо"
                elif same:
                    state = f"Вже є · ID {same[0].id}" + ("" if same[0].text == text else " · інший текст")
                else:
                    state = "Немає — створимо" if name else "—"
                rows.append({"Об'єкт": d.name, "IMEI": d.imei, "Останнє повідомлення": last_seen(d.last_message),
                             f"Право «{PERM_CREATE}»": {True: "Є", False: "Немає"}.get(d.can_create, "—"),
                             f"Команда «{name}»" if name else "Команда": state})
            dfo = drop_empty(pd.DataFrame(rows))
            st.dataframe(dfo, hide_index=True, width="stretch", height=360, column_config=col_cfg(dfo))

    ready = bool(name and text.strip()) and bool(allowed) and not any_running()
    label = f"Створити на {n_obj(to_create, OBJ_LOC)}" if scan and name else f"Створити на {n_obj(len(allowed), OBJ_LOC)}"
    b1, b2 = st.columns([2, 3], vertical_alignment="center")
    if b1.button(label, type="primary", disabled=not ready, width="stretch"):
        job = CreateJob(S.client, group_by_id(gid), devices, name, text, load_cfg(), scan=scan)
        other = registry.claim("create", S.client.base_url, gid, job)
        if other is not None:  # на цій групі вже йде створення (інша вкладка / подвійний клік) — показуємо його
            S.create_job, S.create_stage = other, "progress"
            st.rerun()
        S.create_job, S.create_stage, S.last_name = job, "progress", name
        job.start()
        st.rerun()
    skip_parts = []
    if have:
        skip_parts.append(f"{fmt_n(len(have))} уже мають «{name}»")
    if no_right:
        skip_parts.append(f"{fmt_n(len(no_right))} без права «{PERM_CREATE}»")
    if skip_parts:
        b2.caption(" і ".join(skip_parts) + " будуть пропущені")
    elif not name or not text.strip():
        b2.caption("Заповніть ім'я і текст команди")
    ready_same = [d for d in have if any(c.text == text for c in find_by_name(scan.commands.get(d.id, []), name))] \
        if scan and name else []
    if ready_same:
        if st.button(f"Команда «{name}» вже є на {n_obj(len(ready_same), OBJ_LOC)} — перейти до відправки →",
                     key="c_go_dispatch_ready", disabled=any_running()):
            go_to(1, gid, name, text)


def create_progress(job: CreateJob) -> None:
    run_every = (3 if job.total > BIG_GROUP else 1) if job.running else None

    @st.fragment(run_every=run_every)
    def live() -> None:
        c = job.counts()
        done = c.get(CREATED, 0) + c.get(SKIPPED, 0) + c.get(ERROR, 0)
        with st.container(border=True):
            h1, h2 = st.columns([3, 2], vertical_alignment="bottom")
            h1.markdown(f"#### Створення «{job.name}» у групі «{job.group['name']}»")
            if job.running:
                h2.caption(f"Пачка {job.batch_no} з {job.batch_total} · пауза {job.cfg.pause * job.slowdown:g} с "
                           "між пачками")
            if job.running and job.message:
                st.warning(job.message)
            pct = done / job.total if job.total else 1.0
            st.progress(min(1.0, pct), text=f"{fmt_n(done)} з {n_obj(job.total)} · {pct * 100:.0f}%")
        m = st.columns(4)
        m[0].metric("Створено", fmt_n(c.get(CREATED, 0)), border=True)
        m[1].metric("Пропущено", fmt_n(c.get(SKIPPED, 0)), border=True)
        m[2].metric("Помилки", fmt_n(c.get(ERROR, 0)), border=True)
        m[3].metric("Залишилось", fmt_n(job.total - done), border=True)

        if job.status == "done":
            if c.get(ERROR):
                st.warning(f"Готово з помилками: створено {fmt_n(c.get(CREATED, 0))}, помилок {fmt_n(c[ERROR])}. "
                           "Натисніть «Повторити помилки» або подивіться деталі в таблиці.")
            else:
                st.success(f"Готово: створено {fmt_n(c.get(CREATED, 0))}, пропущено {fmt_n(c.get(SKIPPED, 0))}.")
        elif job.status == "stopped":
            st.warning(f"Зупинено. Не оброблено {n_obj(c.get(QUEUED, 0))} — їх можна дообробити кнопкою «Повторити».")
        elif job.status == "error":
            st.error(job.message)
        if not job.running and job.status in ("done", "stopped") and c.get(CREATED, 0) + c.get(SKIPPED, 0):
            if st.button("Перейти до відправки →", type="primary", key="c_go_dispatch"):
                go_to(1, job.group["id"], job.name, job.text)

        rows = job.snapshot()
        df = drop_empty(pd.DataFrame([{"Об'єкт": r["name"], "IMEI": r["imei"], "Статус": LABELS[r["status"]],
                            "Деталі": r["detail"]} for r in rows]))
        b = st.columns(4)
        if b[0].button("Зупинити", disabled=not job.running, width="stretch", key="c_stop"):
            job.stop()
        left = c.get(ERROR, 0) + c.get(QUEUED, 0)
        if b[1].button(f"Повторити помилки ({fmt_n(left)})", disabled=job.running or not left,
                       width="stretch", key="c_retry"):
            if registry.claim("create", S.client.base_url, job.group["id"], job) is None:
                job.retry_failed()
            st.rerun()
        b[2].download_button("Завантажити звіт CSV", csv_bytes(df), width="stretch",
                             file_name=f"create_{job.name}_{datetime.now(LOCAL_TZ):%Y%m%d_%H%M}.csv",
                             mime="text/csv", disabled=job.running, key="c_csv")
        if b[3].button("← Нове створення", disabled=job.running, width="stretch", key="c_back"):
            S.create_stage = "form"
            st.rerun()

        filters = {"all": ("Усі", None), CREATED: ("Створено", {CREATED}), SKIPPED: ("Пропущено", {SKIPPED}),
                   ERROR: ("Помилки", {ERROR})}
        flt = st.segmented_control("Фільтр", list(filters), default="all", key="c_filter", label_visibility="collapsed",
                                   format_func=lambda k: f"{filters[k][0]} · {fmt_n(len(rows) if k == 'all' else c.get(k, 0))}")
        keep = filters.get(flt or "all", filters["all"])[1]
        view = df[[keep is None or r["status"] in keep for r in rows]] if len(df) else df
        big = job.running and len(view) > BIG_GROUP  # великі групи під час запуску — без розфарбування (швидше)
        st.dataframe(view if big else styled(view), hide_index=True, width="stretch", height=420,
                     column_config=col_cfg(view))
        if run_every and not job.running:
            st.rerun()

    live()


# ---------------------------------------------------------------- вкладка 2: відправка
def accepted_map(name: str, text: str) -> dict[int, str]:
    """id об'єкта → коли він прийняв цю команду (у будь-якій групі, за будь-який запуск)."""
    folder = command_dir(name, text)
    out: dict[int, str] = {}
    gh_docs, _ = load_docs(get_store())
    for docs in (gh_docs, local_docs()):
        for g in (docs.get(folder) or {}).get("groups", {}).values():
            for did, d in g["devices"].items():
                if d.get("accepted"):
                    out[int(did)] = fmt_local(d.get("answered_at")) or out.get(int(did), "")
    return out


def build_targets(devices: list[Device], scan: ScanJob, name: str, text: str, mode: str, online_min: int,
                  prev: DispatchJob | None, accepted: dict[int, str] | None = None):
    targets, skipped = [], []
    accepted = accepted or {}
    limit = now_utc() - timedelta(minutes=online_min)
    retry_ids = set(prev.not_answered()) if prev else set()
    for d in devices:
        if d.id in scan.errors:
            skipped.append((d, f"Не вдалося прочитати команди: {scan.errors[d.id]}"))
            continue
        same = find_by_name(scan.commands.get(d.id, []), name)
        match = [c for c in same if c.text == text]
        if not match:
            skipped.append((d, f"Інший текст команди «{name}»" if same else f"Немає команди «{name}»"))
        elif d.can_execute is False:
            skipped.append((d, f"Немає права «{PERM_EXECUTE}»"))
        elif d.id in accepted:
            skipped.append((d, f"Вже прийняв команду ({accepted[d.id]})" if accepted[d.id] else "Вже прийняв команду"))
        elif mode == "online" and (d.last_message is None or d.last_message < limit):
            skipped.append((d, f"Офлайн: останнє повідомлення {ago(d.last_message)}"))
        elif mode == "retry" and d.id not in retry_ids:
            skipped.append((d, "Відповів у минулому запуску"))
        else:
            targets.append((d, match[0].id))
    return targets, skipped


def dispatch_tab() -> None:
    job: DispatchJob | None = S.dispatch_job
    if job is not None and S.dispatch_stage == "progress":
        dispatch_progress(job)
        return
    running_elsewhere("dispatch")

    if S.get("g_dispatch") is None and S.get("g_create") is not None:
        S.g_dispatch = S.g_create
    gid = group_select("g_dispatch")
    if gid is None:
        return
    devices = get_devices(gid)
    if devices is None:
        return
    if S.pop("auto_scan", None) == gid and S.scans.get(gid) is None and devices:
        start_scan(gid)
    err_name = S.get("last_name") or DEFAULT_NAME
    err_text = S.get("c_text") or DEFAULT_TEXT
    err_devs = stats_error_devices(gid, devices, err_name, err_text)
    if err_devs and not any_running():
        with st.container(border=True):
            st.markdown(f"**{n_obj(len(err_devs), OBJ_LOC)} цієї групи — «Помилка відправки» «{err_name}»** "
                        "(за статистикою)")
            e1, e2 = st.columns([1, 2], vertical_alignment="center")
            if e1.button(f"Надіслати тільки їм ({fmt_n(len(err_devs))})", key=f"d_err_{gid}", width="stretch"):
                resend_fresh(group_by_id(gid), err_name, err_text, err_devs, float(S.get("d_wait", 15)),
                             float(S.get("d_online", 5)), activity_every(len(err_devs)))
            e2.caption("Заново прочитає команди цих об'єктів, за потреби створить і надішле лише їм — "
                       "без перевірки всієї групи.")
    scan = scan_block(gid, devices, "d", "Щоб вибрати команду, прочитаю команди кожного об'єкта групи "
                                         "(пачками, щоб не навантажувати сервер).")
    if scan is None:
        return
    names = scan.names()
    if not names:
        st.info("На об'єктах цієї групи команд немає. Спершу створіть команду на вкладці «1 · Створення команди».")
        return
    name_counts = dict(names)
    default = S.get("last_name") or S.get("c_name", "").strip()
    idx = next((i for i, (n, _) in enumerate(names) if n == default),
               next((i for i, (n, _) in enumerate(names) if n == DEFAULT_NAME), 0))
    # ключ залежить від щойно створеної команди: після створення вона вибирається автоматично
    name = st.selectbox("Команда", [n for n, _ in names], index=idx, key=f"d_cmd_{gid}_{default}_{len(names)}",
                        format_func=lambda n: f"{n} — є на {fmt_n(name_counts[n])} з {n_obj(len(devices))}")
    dup_warning(scan, devices, name, "d")
    variants = scan.variants(name)
    if len(variants) == 1:
        text = variants[0][0]
        st.markdown("<span style='font-size:14px'>Текст команди на об'єктах</span>", unsafe_allow_html=True)
        st.code(text or "(порожній)", language=None)
    else:
        vc = dict(variants)
        tidx = next((i for i, (t, _) in enumerate(variants) if t == S.get("go_text")), 0)
        text = st.selectbox("Текст команди (на об'єктах він різний)", [t for t, _ in variants], index=tidx,
                            key=f"d_txt_{gid}_{name}_{S.get('go_text', '')}",
                            format_func=lambda t: f"{t or '(порожній)'} — {n_obj(vc[t], OBJ_LOC)}")
        others = sum(c for t, c in variants if t != text)
        st.warning(f"На {n_obj(others, OBJ_LOC)} команда «{name}» має інший текст — їх не надсилаємо.")

    auto_sync()  # щоб звірка знала про пізні відповіді
    prev: DispatchJob | None = S.prev_dispatch.get((gid, name, text))

    # активність об'єктів, на які в принципі можна надіслати цю команду
    st.markdown("**Активність об'єктів з цією командою**")
    a1, a2 = st.columns([3, 1], vertical_alignment="bottom")
    online_min = int(a2.number_input("Онлайн — повідомлення не давніше, хв", min_value=1, max_value=1440, key="d_online"))
    candidates, _ = build_targets(devices, scan, name, text, "all", online_min, None)
    act_counts = {k: 0 for k in ACTIVITY_LABELS}
    for d, _ in candidates:
        act_counts[activity(d.last_message, online_min)] += 1
    with a1:
        activity_bar(act_counts, online_min)
    p1, p2 = st.columns([3, 1], vertical_alignment="center")
    priority = p1.toggle("Онлайн — першими, далі за давністю останнього повідомлення",
                         key="d_priority", help="Свіжі об'єкти відповідають одразу, тож результати з'являться "
                                                "швидше, а «сплячі» підуть у кінці черги.")
    if p2.button("Оновити активність", key="d_refresh", width="stretch", disabled=any_running()):
        get_devices(gid, reload=True)
        st.rerun()

    c1, c2 = st.columns(2)
    with c1:
        modes = {"all": "Усім, у кого є команда", "online": f"Лише онлайн (≤ {online_min} хв)",
                 "retry": "Тим, хто не відповів минулого разу"}
        mode = st.radio("Кому надсилати", [m for m in modes if m != "retry" or prev], key="d_mode",
                        format_func=modes.get)
        skip_acc = st.toggle("Не надсилати тим, хто вже прийняв команду", key="d_skip_acc",
                             help="Звірка зі статистикою (вкладка «3»): об'єкти, від яких уже є «Результат команди», "
                                  "пропускаються — зручно для повторної відправки через годину чи завтра")
    with c2:
        wait = st.number_input("Чекати відповідь, хв", min_value=1, max_value=180, key="d_wait")
        st.caption("Після цього об'єкти без відповіді отримають статус «Без відповіді». Пізню відповідь можна "
                   "дочитати кнопкою «Перевірити відповіді ще раз».")

    accepted = accepted_map(name, text) if skip_acc else {}
    targets, skipped = build_targets(devices, scan, name, text, mode, online_min, prev, accepted)
    if skip_acc:
        in_group = sum(1 for d in devices if d.id in accepted)
        st.caption(f"Звірка: у цій групі вже прийняли команду {n_obj(in_group)} — їм не надсилаємо."
                   if in_group else "Звірка: у цій групі ще ніхто не прийняв цю команду.")
    if priority:
        targets = by_priority(targets)
    stale = sum(1 for d, _ in targets if activity(d.last_message, online_min) == STALE)
    if stale:
        st.caption(f"{n_obj(stale)} давно не виходили на зв'язок — за {int(wait)} хв вони, найімовірніше, не "
                   "відповідять. Потім їм можна надіслати ще раз окремим запуском.")
    reasons: dict[str, int] = {}
    for _, r in skipped:
        key = r.split(":")[0] if r.startswith(("Офлайн", "Не вдалося")) else r.split(" (")[0]
        reasons[key] = reasons.get(key, 0) + 1
    b1, b2 = st.columns([2, 3], vertical_alignment="center")
    if b1.button(f"Надіслати на {n_obj(len(targets))}", type="primary", width="stretch",
                 disabled=not targets or any_running()):
        job = DispatchJob(S.client, group_by_id(gid), name, text, targets, skipped, dispatch_cfg(),
                          wait_minutes=float(wait), poll_every=POLL_EVERY_S, online_min=online_min,
                          activity_every=activity_every(len(devices)))
        other = registry.claim("dispatch", S.client.base_url, gid, job)
        if other is not None:  # на цій групі вже йде відправка — показуємо її, другу не запускаємо
            S.dispatch_job, S.dispatch_stage = other, "progress"
            st.rerun()
        S.dispatch_job, S.dispatch_stage = job, "progress"
        register_run(job)
        attach_persist(job)
        job.start()
        st.rerun()
    if reasons:
        b2.caption("Пропускаємо: " + "; ".join(f"{r[:1].lower() + r[1:]} — {fmt_n(n)}" for r, n in
                                                sorted(reasons.items(), key=lambda x: -x[1])))
    if len(targets) > 200:
        batches = -(-len(targets) // DISPATCH_BATCH)
        est = batches * (DISPATCH_PAUSE_S + DISPATCH_BATCH / DISPATCH_WORKERS * 0.4) / 60
        st.caption(f"Відправка займе орієнтовно {est:.0f} хв ({fmt_n(batches)} пачок по {DISPATCH_BATCH}), "
                   f"далі — {int(wait)} хв очікування відповідей. Не перезапускайте апку, поки йде запуск.")


def register_run(job: DispatchJob) -> None:
    S.runs.setdefault((job.group["id"], job.name, job.text), []).append(job)


# ---------------------------------------------------------------- статистика: збереження на GitHub
DEFAULT_REPO = "Oleksandr-Rosenko/mass_comand_sent"


def github_token() -> str:
    """Токен із Secrets: GITHUB_TOKEN (або GH_TOKEN, github_token, [github] token)."""
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "github_token", "GITHUB_PAT"):
        if secret(key).strip():
            return secret(key).strip()
    try:
        return str(st.secrets.get("github", {}).get("token", "")).strip()
    except Exception:  # noqa: BLE001
        return ""


def get_store() -> GitHubStore | None:
    """Сховище статистики на GitHub або None, якщо токен не задано (тоді статистика лише в сесії)."""
    token = github_token()
    if not token:
        return None
    try:
        return GitHubStore(token, secret("GITHUB_REPO", DEFAULT_REPO), secret("GITHUB_BRANCH", "stats"),
                           api=secret("GITHUB_API", "https://api.github.com"))
    except GitHubError as e:
        S.gh_error = str(e)
        return None


def persist(job: DispatchJob, store: GitHubStore | None) -> None:
    """Дописує результати запуску в статистику на GitHub. Працює у фоновому потоці (без session_state)."""
    if store is None:
        job.save_state = ("local", "Статистика лише в цій сесії: GITHUB_TOKEN у Secrets не задано", time.time())
        return
    job.save_state = ("saving", "Зберігаю статистику на GitHub…", time.time())
    rows = job.snapshot()
    answered = sum(1 for r in rows if r["status"] == ANSWERED)
    msg = f"Статистика «{job.name}»: {job.group['name']} — відповіли {answered} з {job.dispatched}"
    cache: dict = {"n": 0, "err": ""}

    def mutate(doc):
        doc = merge_run(doc, name=job.name, text=job.text, group=job.group, run_id=job.run_id,
                        started_at=job.started_wall, rows=rows)
        # пізні відповіді з історії сповіщень (і на цей, і на попередні запуски цієї команди)
        since = history_since(doc)
        if since is not None:
            try:
                if "items" not in cache or cache["since"] > since:
                    cache["items"], cache["since"] = job.client.results_since(since), since
                cache["n"] = apply_history(doc, cache["items"])
            except ApiError as e:
                cache["err"] = str(e)
        return doc

    try:
        store.update(command_dir(job.name, job.text), mutate, msg)
        extra = f"; з історії сповіщень дозараховано відповідей: {cache['n']}" if cache["n"] else ""
        if cache["err"]:
            extra += f"; історію сповіщень прочитати не вдалося: {cache['err']}"
        job.save_state = ("ok", f"Статистику збережено на GitHub (гілка {store.branch}){extra}", time.time())
    except GitHubError as e:
        job.save_state = ("error", str(e), time.time())


def sync_history(store: GitHubStore, docs: dict[str, dict]) -> tuple[int, str]:
    """Дозараховує в статистику відповіді з історії сповіщень (по всіх командах). Повертає (змін, помилка)."""
    info: dict = {}
    n, err = sync_docs(S.client, store, docs, info)
    S.last_sync = time.time()
    S.sync_info = {**info, "at": time.time()}
    if n:
        S.gh_docs = None
    return n, err


AUTO_SYNC_EVERY = 15 * 60  # с: не частіше, щоб не читати історію на кожен клік


def auto_sync() -> None:
    """Тихо звіряє статистику з історією, якщо востаннє це було давніше AUTO_SYNC_EVERY (і нічого не запущено)."""
    store = get_store()
    if store is None or any_running() or time.time() - S.get("last_sync", 0) < AUTO_SYNC_EVERY:
        return
    docs, source = load_docs(store)
    if source != "github" or not docs:
        S.last_sync = time.time()
        return
    with st.spinner("Звіряю статистику з історією сповіщень…"):
        n, err = sync_history(store, docs)
    if n:
        st.toast(f"З історії сповіщень дозараховано відповідей: {n}")
    if err:
        S.sync_error = err


def attach_persist(job: DispatchJob) -> None:
    store = get_store()
    job.save_state = ("pending", "Статистика збережеться після завершення запуску", time.time())
    job.on_finish = lambda j: persist(j, store)


def persist_async(job: DispatchJob) -> None:
    job.save_state = ("saving", "Зберігаю статистику на GitHub…", time.time())
    threading.Thread(target=persist, args=(job, get_store()), daemon=True).start()


def save_status(job: DispatchJob) -> None:
    state, msg, at = job.save_state
    when = datetime.fromtimestamp(at).astimezone(LOCAL_TZ).strftime("%H:%M:%S") if at else ""
    if state == "ok":
        c1, c2 = st.columns([3, 1], vertical_alignment="center")
        c1.caption(f"{msg} о {when}.")
        if c2.button("До статистики →", key="d_go_stats", width="stretch"):
            go_to(2)
    elif state == "error":
        c1, c2 = st.columns([3, 1], vertical_alignment="center")
        c1.warning(f"Статистику не збережено: {msg}")
        if c2.button("Зберегти ще раз", key="d_save_retry", width="stretch"):
            persist_async(job)
            st.rerun()
    elif state in ("saving", "pending", "local"):
        st.caption(msg)
        if state == "local" and st.button("До статистики →", key="d_go_stats_local"):
            go_to(2)


def local_docs() -> dict[str, dict]:
    """Статистика лише із запусків цієї сесії (коли GitHub не налаштовано або недоступний)."""
    docs: dict[str, dict] = {}
    for (_, name, text), jobs in S.runs.items():
        folder = command_dir(name, text)
        for j in jobs:
            docs[folder] = merge_run(docs.get(folder), name=name, text=text, group=j.group, run_id=j.run_id,
                                     started_at=j.started_wall, rows=j.snapshot())
    return docs


def load_docs(store: GitHubStore | None) -> tuple[dict[str, dict], str]:
    if store is None:
        return local_docs(), "local"
    saved = max((j.save_state[2] for js in S.runs.values() for j in js if j.save_state[0] == "ok"), default=0)
    if S.get("gh_docs") is None or S.get("gh_loaded_at", 0) < saved:
        try:
            with st.spinner("Читаю статистику з GitHub…"):
                ref = store.head()
                S.gh_docs = {f: d for f in store.list_folders(ref) if (d := store.read_doc(f, ref))}
            S.gh_loaded_at, S.gh_error = time.time(), ""
        except GitHubError as e:
            S.gh_docs, S.gh_error = None, str(e)
    if S.get("gh_docs") is None:
        return local_docs(), "error"
    return S.gh_docs, "github"


STATS_OWNER = secret("STATS_OWNER_EMAIL", "alona.suprun@kyivstar.net").strip()
STATS_TAGS = [t.strip() for t in secret("STATS_PRIORITY_TAGS", "_OVA,_KS").split(",") if t.strip()]


def owner_group_ids() -> set[int] | None:
    """Групи, створені STATS_OWNER (кеш на сесію). None — фільтр не застосовано (немає email або помилка)."""
    if not STATS_OWNER:
        return None
    if S.get("owner_groups") is None:
        try:
            with st.spinner(f"Шукаю групи користувача {STATS_OWNER}…"):
                uid = S.client.find_user_id(STATS_OWNER)
                groups = S.client.groups_with_creator()
            own = STATS_OWNER.lower()
            S.owner_groups = {int(g["id"]) for g in groups if g.get("id") is not None and (
                (uid is not None and g.get("creatorId") == uid) or str(g.get("creatorName") or "").lower() == own)}
            S.owner_error = "" if uid is not None or S.owner_groups else f"Користувача {STATS_OWNER} не знайдено"
        except ApiError as e:
            S.owner_groups, S.owner_error = None, f"Не вдалося отримати групи {STATS_OWNER}: {e}"
            return None
    return S.owner_groups


def tagged(name: str) -> bool:
    up = name.upper()
    return any(t.upper() in up for t in STATS_TAGS)


def sync_diagnostics() -> None:
    """Блок «Діагностика звірки»: що прочитано з історії і чому відповіді зараховано чи ні."""
    info = S.get("sync_info")
    if not info:
        return
    when = datetime.fromtimestamp(info["at"]).astimezone(LOCAL_TZ).strftime("%H:%M:%S")
    if info.get("error"):
        st.warning(f"Звірка з історією о {when} не вдалася: {info['error']}")
    with st.expander(f"Діагностика звірки з історією · о {when} · прочитано {fmt_n(info.get('read', 0))}, "
                     f"зараховано {fmt_n(info.get('applied', 0))}"):
        if info.get("note"):
            st.caption(info["note"])
        if info.get("since"):
            st.caption(f"Читали «Результати команди» з {fmt_time(info['since'], True)} (перша відправка мінус 2 хв).")
        rows = [("Прочитано записів з історії", info.get("read", 0)),
                ("Підходять для зарахування (після відправки, ще не прийняв)", info.get("match", 0)),
                ("Об'єкт уже «Прийняв»", info.get("already", 0)),
                ("Цьому об'єкту команду не надсилали (за статистикою)", info.get("not_sent", 0)),
                ("Відповідь раніше за відправку", info.get("too_early", 0)),
                ("Без deviceId", info.get("no_device", 0)),
                ("Не вдалося розібрати дату (createdAt)", info.get("no_date", 0))]
        st.dataframe(pd.DataFrame(rows, columns=["Що", "Скільки"]), hide_index=True, width="stretch")
        if info.get("sample"):
            st.caption("Приклад сирих записів з історії (перші 5):")
            st.dataframe(pd.DataFrame(info["sample"]), hide_index=True, width="stretch")


def stats_tab() -> None:
    store = get_store()
    auto_sync()
    docs, source = load_docs(store)
    top1, top2 = st.columns([4, 1], vertical_alignment="center")
    if source == "github":
        loaded = datetime.fromtimestamp(S.gh_loaded_at).astimezone(LOCAL_TZ).strftime("%H:%M:%S")
        url = f"https://github.com/{store.repo}/tree/{store.branch}/stats"
        top1.caption(f"Дані з GitHub, гілка `{store.branch}` · прочитано о {loaded} · [відкрити на GitHub]({url}). "
                     "Оновлюються автоматично після кожного запуску.")
    elif source == "error":
        top1.warning(f"Не вдалося прочитати статистику з GitHub: {S.gh_error}. Нижче — лише запуски з цієї сесії.")
    else:
        top1.info("Статистика зараз лише в межах цієї сесії. Щоб вона зберігалась на GitHub і оновлювалась після "
                  "кожного запуску, додайте GITHUB_TOKEN у Secrets застосунку (інструкція в README).")
    if top2.button("Оновити", key="st_reload", width="stretch"):
        S.gh_docs = None
        st.rerun()
    if source == "github" and docs:
        h1, h2 = st.columns([4, 1], vertical_alignment="center")
        h1.caption("Трекери часто відповідають пізніше, ніж ми чекаємо (команда чекає, поки трекер вийде на "
                   "зв'язок). Кнопка дочитає всі «Результати команди» з історії сповіщень і зарахує їх.")
        if h2.button("Підтягнути з історії", key="st_sync", width="stretch", disabled=any_running()):
            with st.spinner("Читаю історію сповіщень…"):
                n, err = sync_history(store, docs)
            if err:
                st.warning(err)
            st.toast(f"Дозараховано відповідей: {n}" if n else "Нових відповідей в історії немає")
            S.gh_docs = None
            if n:
                st.rerun()
    if not docs:
        st.info("Ще немає жодного запуску — нижче список груп, статистика з'явиться після першої відправки.")
        docs = {command_dir(DEFAULT_NAME, DEFAULT_TEXT): {"command": {"name": DEFAULT_NAME, "text": DEFAULT_TEXT},
                                                          "groups": {}}}

    sync_diagnostics()
    keys = list(docs)
    labels = {k: f"{docs[k]['command']['name']} — {docs[k]['command']['text']}" for k in keys}
    want = command_dir(S.get("last_name") or S.get("c_name", ""), S.get("c_text", ""))
    if len(keys) > 1:
        folder = st.selectbox("Команда", keys, index=keys.index(want) if want in keys else 0, format_func=labels.get,
                              key="st_cmd")
    else:
        folder = keys[0]
        st.markdown(f"**Команда:** {html.escape(docs[folder]['command']['name'])} · "
                    f"`{docs[folder]['command']['text']}`")
    doc = docs[folder]
    name = doc["command"]["name"]
    own = owner_group_ids()
    if own is None:
        if S.get("owner_error"):
            st.warning(f"{S.owner_error}. Показую всі групи.")
    else:
        if S.get("owner_error"):
            st.warning(S.owner_error)
        st.caption(f"Групи користувача **{STATS_OWNER}** ({fmt_n(len(own))}): спершу перевірені, потім з "
                   f"{' / '.join(STATS_TAGS)} у назві, потім решта.")

    def mine(gid) -> bool:
        return own is None or int(gid) in own

    # Статистика по об'єктах з усіх запусків: група показується за своїм складом, хоч би через яку групу
    # надсилали (напр., запуск на велику групу оновлює всі менші, де є ці об'єкти).
    index = device_index(doc)
    views = []
    for g in S.groups:
        if not mine(g["id"]):
            continue
        summary, rows, untouched = group_view(index, [int(i) for i in g["deviceIds"]])
        views.append((g, summary, rows, untouched))
    views.sort(key=lambda v: (not v[1]["checked"], -(parse_dt(v[1]["last_run_at"]).timestamp()
                                                     if v[1]["last_run_at"] else 0),
                              not tagged(v[0]["name"]), v[0]["name"].lower()))
    checked = [v for v in views if v[1]["checked"]]
    # унікальні об'єкти: той самий об'єкт може бути в кількох групах
    uniq = {int(i) for v in checked for i in v[0]["deviceIds"]}
    total_obj = len(uniq)
    total_acc = sum(1 for i in uniq if index.get(i, {}).get("accepted"))
    m = st.columns(3)
    m[0].metric("Груп перевірено", f"{fmt_n(len(checked))} з {fmt_n(len(views))}", border=True)
    m[1].metric("Об'єктів у перевірених групах", fmt_n(total_obj), border=True,
                help="Унікальні об'єкти: якщо об'єкт є в кількох групах, рахується один раз")
    m[2].metric("Прийняли команду", f"{fmt_n(total_acc)}" + (f" ({total_acc / total_obj * 100:.0f}%)"
                                                             if total_obj else ""), border=True)

    all_rows = []
    for g, x, rows, untouched in views:
        all_rows += [{"Група": g["name"], **r} for r in rows]
        if not x["checked"]:
            with st.expander(f"**{g['name']}** · {n_obj(x['objects'])} · :gray[не перевірялась]"):
                st.caption(f"Команду «{name}» об'єктам цієї групи ще не надсилали.")
            continue
        label = (f"**{g['name']}** · {n_obj(x['objects'])} · :green[прийняли {fmt_n(x['accepted'])}] з "
                 f"{fmt_n(x['objects'])} ({x['percent']}%) · надсилали {fmt_n(x['sent'])} · "
                 f"останнє {fmt_local(x['last_run_at'])}")
        with st.expander(label):
            df = pd.DataFrame(rows)
            view = pd.DataFrame([{
                "Прийняв": r["Прийняв"], "Об'єкт": r["Об'єкт"],
                "Відповідь / причина": r["Відповідь трекера"] if r["Прийняв"] == "Так" else r["Статус"],
                "Коли відповів": r["Коли відповів"], "Разів надіслано": r["Скільки разів надсилали"],
            } for r in rows])
            st.dataframe(styled(view, "Прийняв"), hide_index=True, width="stretch", column_config=col_cfg(view),
                         height=min(420, 40 + 35 * max(1, len(rows))))
            c1, c2 = st.columns([3, 1], vertical_alignment="center")
            extra = f" Ще {n_obj(untouched)} групи команду не надсилали." if untouched else ""
            c1.caption("Спершу ті, що прийняли, потім ті, що ні. Враховано всі запуски, через яку б групу не "
                       f"надсилали (у CSV — колонка «Через групу»).{extra}")
            c2.download_button("CSV групи", csv_bytes(df), width="stretch", key=f"st_csv_{g['id']}",
                               file_name=f"{name}_{g['name']}_{datetime.now(LOCAL_TZ):%Y%m%d}.csv", mime="text/csv")
    st.download_button("Завантажити всю статистику (CSV)", csv_bytes(pd.DataFrame(all_rows)),
                       key="st_csv_all", file_name=f"{name}_усі_групи_{datetime.now(LOCAL_TZ):%Y%m%d}.csv",
                       mime="text/csv", disabled=not all_rows)


def resend(job: DispatchJob) -> None:
    ids = set(job.not_answered())
    devices = {d.id: d for d in S.devices.get(job.group["id"], [])}
    targets = []
    for did in ids:
        r = job.rows[did]
        dev = devices.get(did) or Device(id=did, name=r["name"], imei=r["imei"], last_message=r["last_message"])
        targets.append((dev, int(r["command_id"])))
    new = DispatchJob(S.client, job.group, job.name, job.text, by_priority(targets), [], dispatch_cfg(),
                      wait_minutes=job.wait_minutes, poll_every=POLL_EVERY_S,
                      online_min=job.online_min, activity_every=job.activity_every)
    S.prev_dispatch[(job.group["id"], job.name, job.text)] = job
    if registry.claim("dispatch", S.client.base_url, job.group["id"], new) is not None:
        return
    S.dispatch_job = new
    register_run(new)
    attach_persist(new)
    new.start()


def resend_fresh(group: dict, name: str, text: str, devs: list[Device], wait_minutes: float,
                 online_min: float, activity_every_s: float, prev: DispatchJob | None = None) -> None:
    """Надсилає лише вказаним об'єктам: заново читає їхні команди (ID могла змінитись), за потреби створює
    команду. Без проходу по всій групі."""
    targets, failed = [], []
    bar = st.progress(0.0, text="Готую команди…")
    for n, dev in enumerate(devs, 1):
        try:
            cmds = [Command.from_api(c) for c in S.client.device_commands(dev.id) if c.get("id") is not None]
            same = [c for c in find_by_name(cmds, name) if c.text == text]
            if same:
                cid = same[0].id
            else:
                cid = (S.client.create_command(dev.id, name, text) or {}).get("id")
                if cid is None:
                    raise ApiError("сервер не повернув ID створеної команди")
            targets.append((dev, int(cid)))
        except ApiError as e:
            failed.append((dev, f"Не вдалося підготувати команду: {describe_error(e, PERM_CREATE)}"))
        bar.progress(n / len(devs), text=f"Готую команди: {n} з {len(devs)}")
        time.sleep(0.3)  # м'яко до сервера команд
    if not targets:
        st.warning("Жодному об'єкту не вдалося підготувати команду: " +
                   "; ".join(f"{d.name}: {why}" for d, why in failed[:5]))
        return
    new = DispatchJob(S.client, group, name, text, by_priority(targets), failed, dispatch_cfg(),
                      wait_minutes=wait_minutes, poll_every=POLL_EVERY_S, online_min=online_min,
                      activity_every=activity_every_s)
    if registry.claim("dispatch", S.client.base_url, group["id"], new) is not None:
        st.warning("На цій групі вже йде інша відправка — спробуйте після її завершення.")
        return
    if prev is not None:
        S.prev_dispatch[(group["id"], name, text)] = prev
    S.dispatch_job, S.dispatch_stage = new, "progress"
    register_run(new)
    attach_persist(new)
    new.start()
    st.rerun()


def retry_send_errors(job: DispatchJob) -> None:
    """Повтор лише для об'єктів зі статусом «Помилка» в цьому запуску."""
    devices = {d.id: d for d in S.devices.get(job.group["id"], [])}
    devs = [devices.get(did) or Device(id=did, name=r["name"], imei=r["imei"], last_message=r["last_message"])
            for did, r in job.rows.items() if r["status"] == ERROR]
    resend_fresh(job.group, job.name, job.text, devs, job.wait_minutes, job.online_min, job.activity_every, prev=job)


def stats_error_devices(gid: int, devices: list[Device], name: str, text: str) -> list[Device]:
    """Об'єкти групи, у яких за статистикою «Помилка відправки» і які ще не прийняли команду."""
    store = get_store()
    docs, _ = load_docs(store) if store else (local_docs(), "local")
    doc = docs.get(command_dir(name, text))
    if not doc:
        return []
    index = device_index(doc)
    return [d for d in devices if (rec := index.get(d.id)) and not rec["accepted"]
            and str(rec.get("status", "")).startswith("Помилка відправки")]


def dispatch_progress(job: DispatchJob) -> None:
    busy = job.running or job.save_state[0] in ("pending", "saving")
    run_every = (3 if len(job.rows) > BIG_GROUP else 1) if busy else None

    @st.fragment(run_every=run_every)
    def live() -> None:
        c = job.counts()
        sent = job.dispatched
        answered, waiting = c.get(ANSWERED, 0), c.get(SENT, 0)
        errors = c.get(ERROR, 0) + c.get(NOT_SENT, 0)
        h1, h2 = st.columns([3, 1], vertical_alignment="bottom")
        h1.markdown(f"### Запуск о {fmt_time(job.started_wall)[:5]} · «{job.name}» → {job.group['name']}")
        h2.caption(f"Відправлено {fmt_n(sent)} з {fmt_n(job.targets)}")
        st.code(job.text, language=None)

        with st.container(border=True):
            if job.phase in ("idle", "baseline"):
                st.markdown("**Готуюсь до відправки**")
                st.progress(0.0, text=job.message or "Читаю історію сповіщень")
            elif job.phase == "sending":
                if job.message:
                    st.warning(job.message)
                done = sent + c.get(ERROR, 0)
                st.markdown(f"**Відправка** · пачка {job.batch_no} з {job.batch_total}")
                st.progress(min(1.0, done / job.targets) if job.targets else 1.0,
                            text=f"Оброблено {fmt_n(done)} з {fmt_n(job.targets)} · відповіли {fmt_n(answered)}")
            else:
                if job.phase == "waiting":
                    waited = (job.wait_minutes * 60) - (job.wait_left or 0)
                    st.markdown(f"**Очікуємо відповіді** · {mmss(waited)} з {mmss(job.wait_minutes * 60)}")
                else:
                    st.markdown("**Очікування завершено**")
                st.progress(min(1.0, answered / sent) if sent else 0.0,
                            text=f"Відповіли {fmt_n(answered)} з {fmt_n(sent)} надісланих"
                                 + (f" · {answered / sent * 100:.0f}%" if sent else ""))
            stats = job.activity_stats()
            parts = [f"<span style='color:{ACT_TEXT[k]};font-weight:600'>● {ACTIVITY_LABELS[k]}</span> "
                     f"{fmt_n(v['answered'])} з {fmt_n(v['sent'])}" for k, v in stats.items() if v["sent"]]
            if parts:
                st.markdown("<div style='font-size:14px;color:#555A66'>Відповіли за активністю: "
                            + " &nbsp;·&nbsp; ".join(parts) + "</div>", unsafe_allow_html=True)
            if job.phase == "waiting":
                ws = job.waiting_summary()
                if ws["waiting"]:
                    checked = (fmt_time(datetime.fromtimestamp(job.activity_checked).astimezone(LOCAL_TZ))
                               if job.activity_checked and job.activity_checked > (job.wait_started or 0) else "ще ні")
                    w1, w2 = st.columns([3, 1], vertical_alignment="center")
                    w1.markdown(
                        f"<div style='font-size:14px;color:#555A66'>Із тих, хто чекає ({fmt_n(ws['waiting'])}): "
                        f"<b style='color:#1E7A43'>вийшли на зв'язок після відправки — {fmt_n(ws['woke'])}</b>, "
                        f"мовчать — {fmt_n(ws['silent'])} · «останнє повідомлення» оновлено: {checked}"
                        f" (кожні {job.activity_every / 60:g} хв)</div>", unsafe_allow_html=True)
                    if w2.button("Оновити зараз", key="d_act_now", width="stretch"):
                        job.refresh_activity()
                    waited_min = ((job.wait_minutes * 60) - (job.wait_left or 0)) / 60
                    if ws["woke"] == 0 and checked != "ще ні" and waited_min >= 2:
                        st.info("Ніхто з тих, хто чекає, після відправки на зв'язок не виходив — чекати далі, "
                                "найімовірніше, немає сенсу. Можна натиснути «Зупинити очікування» і пізніше "
                                "надіслати їм ще раз кнопкою «Ще раз — без відповіді» (вона стає активною після "
                                "завершення очікування; сама за онлайном не стежить).")
                if job.activity_error:
                    st.caption(job.activity_error)
        m = st.columns(4)
        m[0].metric("Надіслано", fmt_n(sent), border=True)
        m[1].metric("Відповіли", fmt_n(answered), border=True)
        m[2].metric("Чекаємо" if job.running else "Без відповіді",
                    fmt_n(waiting if job.running else c.get(NO_ANSWER, 0)), border=True)
        m[3].metric("Помилка відправки", fmt_n(errors), border=True)

        if job.poll_error:
            st.warning(job.poll_error)
        if job.status == "error":
            st.error(job.message)
        elif not job.running:
            if c.get(NO_ANSWER) or errors:
                st.warning(f"Відповіли {fmt_n(answered)} з {fmt_n(sent)}. Без відповіді — {fmt_n(c.get(NO_ANSWER, 0))}, "
                           f"не надіслано або помилка — {fmt_n(errors)}.")
            elif sent:
                st.success(f"Усі {n_obj(answered)} відповіли.")
        save_status(job)

        b = st.columns(4)
        if job.running and job.phase in ("idle", "baseline", "sending"):
            if b[0].button("Зупинити відправку", width="stretch", key="d_stop"):
                job.stop()
        elif job.running:
            if b[0].button("Зупинити очікування", width="stretch", key="d_stopw"):
                job.stop_waiting()
        else:
            if b[0].button("Перевірити відповіді ще раз", width="stretch", key="d_poll"):
                with st.spinner("Читаю історію сповіщень…"):
                    got = job.poll()
                st.toast(f"Нових відповідей: {got}")
                if got:
                    persist_async(job)  # дописати пізні відповіді в статистику
                    st.rerun()
        retry_n = len(job.not_answered())
        if b[1].button(f"Ще раз — без відповіді ({fmt_n(retry_n)})", width="stretch",
                       disabled=job.running or not retry_n, key="d_resend",
                       help="Новий запуск лише для тих, хто не відповів, не отримав команду або отримав помилку"):
            resend(job)
            st.rerun()
        n_err = c.get(ERROR, 0)
        if n_err and not job.running:
            e1, e2 = st.columns([1, 2], vertical_alignment="center")
            if e1.button(f"Повторити помилки відправки ({fmt_n(n_err)})", width="stretch", key="d_retry_err"):
                retry_send_errors(job)
            e2.caption("Лише об'єкти зі статусом «Помилка»: заново прочитає їхні команди, за потреби створить NTP "
                       "і надішле тільки їм. Решту групи не чіпає.")
        rows = job.snapshot()
        # у порядку відправки (онлайн першими, якщо увімкнено пріоритет), пропущені — в кінці
        rows.sort(key=lambda r: (r["order"] is None, r["order"] or 0, r["name"].lower()))
        def answer_text(r):
            return r["answer"] or r["detail"] or (
                ("Виходив на зв'язок, відповіді ще немає" if DispatchJob.woke(r)
                 else "Не виходив на зв'язок") if r["status"] == SENT else "")

        def status_cell(r):
            """Статус + час: «Відповів · 17:24:14», «Чекаємо · з 17:24:12»."""
            label = LABELS[r["status"]]
            if r["answered_at"]:
                return f"{label} · {fmt_time(r['answered_at'])}"
            if r["sent_at"] and r["status"] in (SENT, NO_ANSWER):
                return f"{label} · з {fmt_time(r['sent_at'])}"
            return label

        df_csv = pd.DataFrame([{
            "№": str(r["order"] or ""), "Об'єкт": r["name"], "IMEI": r["imei"],
            "Активність": act_label(r["activity"], r["last_message"]).lstrip("● "),
            "Останнє повідомлення": fmt_time(r["last_message"], True), "Надіслано": fmt_time(r["sent_at"]),
            "Статус": LABELS[r["status"]], "Відповідь трекера / деталі": answer_text(r),
            "Отримано": fmt_time(r["answered_at"]),
        } for r in rows])
        df = pd.DataFrame([{
            "№": str(r["order"] or ""), "Об'єкт": r["name"],
            "Останнє повідомлення": seen_cell(r["activity"], r["last_message"]),
            "Статус": status_cell(r), "Відповідь трекера / деталі": answer_text(r), "IMEI": r["imei"],
        } for r in rows])
        df, df_csv = drop_empty(df), drop_empty(df_csv)
        b[2].download_button("Завантажити звіт CSV", csv_bytes(df_csv), width="stretch", disabled=job.running,
                             file_name=f"dispatch_{job.name}_{datetime.now(LOCAL_TZ):%Y%m%d_%H%M}.csv",
                             mime="text/csv", key="d_csv")
        if b[3].button("← Нова відправка", disabled=job.running, width="stretch", key="d_back"):
            S.prev_dispatch[(job.group["id"], job.name, job.text)] = job
            S.dispatch_stage = "form"
            st.rerun()

        filters = {"all": ("Усі", None), ANSWERED: ("Відповіли", {ANSWERED}), "wait": ("Чекаємо", {SENT, QUEUED, RUNNING}),
                   NO_ANSWER: ("Без відповіді", {NO_ANSWER}), "err": ("Помилки", {ERROR, NOT_SENT}),
                   SKIPPED: ("Пропущено", {SKIPPED})}

        def count(k):
            keep = filters[k][1]
            return len(rows) if keep is None else sum(1 for r in rows if r["status"] in keep)

        flt = st.segmented_control("Фільтр", list(filters), default="all", key="d_filter", label_visibility="collapsed",
                                   format_func=lambda k: f"{filters[k][0]} · {fmt_n(count(k))}")
        keep = filters.get(flt or "all", filters["all"])[1]
        view = df[[keep is None or r["status"] in keep for r in rows]] if len(df) else df
        big = job.running and len(view) > BIG_GROUP
        st.dataframe(view if big else styled(view), hide_index=True, width="stretch", height=560,
                     column_config=col_cfg(view))
        if big:
            st.caption("Велика група: під час запуску таблиця оновлюється раз на 3 с і без кольорів, щоб не гальмувати.")
        if run_every and not job.running and job.save_state[0] not in ("pending", "saving"):
            S.prev_dispatch[(job.group["id"], job.name, job.text)] = job
            st.rerun()

    live()


# ---------------------------------------------------------------- запуск
if S.client is None or not S.client.token:
    login_page()
else:
    sidebar()
    st.title("Масові команди")
    t1, t2, t3 = st.tabs(TABS, key="nav", on_change="rerun")
    with t1:
        create_tab()
    with t2:
        dispatch_tab()
    with t3:
        stats_tab()
