"""Streamlit-застосунок «Масові команди M2M»: створення команди на групі об'єктів і масова відправка з результатами."""

from __future__ import annotations

import html
from datetime import datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from m2m_commands.client import NAME_MAX, TEXT_MAX, M2MClient
from m2m_commands.errors import ApiError
from m2m_commands.jobs import (
    ACTIVITY_LABELS, ANSWERED, CREATED, ERROR, LABELS, NO_ANSWER, NOT_SENT, ONLINE, PERM_CREATE, PERM_EXECUTE,
    QUEUED, RUNNING, SENT, SKIPPED, STALE, TODAY, CreateJob, Device, DispatchJob, LoadCfg, ScanJob, activity,
    by_priority, find_by_name, now_utc,
)

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
    [data-testid="stSidebar"] [data-testid="stNumberInput"] input {background: #FFFFFF;}
    </style>
    """,
    unsafe_allow_html=True,
)

S = st.session_state
for k, v in {"client": None, "groups": [], "devices": {}, "scans": {}, "create_job": None, "create_stage": "form",
             "dispatch_job": None, "dispatch_stage": "form", "prev_dispatch": {}, "last_name": "",
             "d_wait": 15, "d_online": 10, "d_priority": True}.items():
    S.setdefault(k, v)
# Streamlit забуває значення полів, які в цьому проході не намальовані (напр., форма під час прогресу).
# Перезапис зберігає їх між екранами.
for k in ("g_create", "c_name", "c_text", "g_dispatch", "d_wait", "d_online", "d_mode", "d_priority"):
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


def mmss(sec: float | None) -> str:
    if sec is None:
        return "—"
    sec = int(sec)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def host_label(url: str) -> str:
    return urlparse(url if "//" in url else f"//{url}").hostname or url


def load_cfg() -> LoadCfg:
    return LoadCfg(batch_size=int(S.get("cfg_batch", 50)), pause=float(S.get("cfg_pause", 2.0)),
                   workers=int(S.get("cfg_workers", 4)))


def any_running() -> bool:
    jobs = [S.create_job, S.dispatch_job, *S.scans.values()]
    return any(j is not None and j.running for j in jobs)


def styled(df: pd.DataFrame, col: str = "Статус"):
    if not len(df) or col not in df.columns:
        return df

    def paint(v):
        bg, fg = STATUS_COLORS.get(v, ("", ""))
        return f"background-color: {bg}; color: {fg}; font-weight: 600" if bg else ""

    by_label = {f"● {lbl}": ACT_TEXT[k] for k, lbl in ACTIVITY_LABELS.items()}

    def paint_act(v):
        return f"color: {by_label[v]}; font-weight: 600" if v in by_label else ""

    sty = df.style.map(paint, subset=[col])
    if "Активність" in df.columns:
        sty = sty.map(paint_act, subset=["Активність"])
    return sty


def act_label(k: str) -> str:
    return f"● {ACTIVITY_LABELS[k]}"


def activity_bar(counts: dict[str, int], online_min: int) -> None:
    """Смуга розподілу об'єктів за давністю останнього повідомлення + легенда."""
    total = sum(counts.values()) or 1
    names = {ONLINE: f"Онлайн (≤ {online_min} хв)", TODAY: "За добу", STALE: "Давніше / ніколи"}
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
        st.markdown("**Навантаження**")
        busy = any_running()
        st.number_input("Розмір пачки, об'єктів", 1, 500, 50, key="cfg_batch", disabled=busy)
        st.number_input("Пауза між пачками, с", 0.0, 60.0, 2.0, step=0.5, key="cfg_pause", disabled=busy)
        st.number_input("Паралельних запитів у пачці", 1, 10, 4, key="cfg_workers", disabled=busy)
        st.number_input("Повторів при помилці 5xx", 0, 5, 2, key="cfg_retries", disabled=busy)
        st.number_input("Опитувати відповіді кожні, с", 5, 120, 15, key="cfg_poll", disabled=busy)
        client.retries = int(S.get("cfg_retries", 2))
        gid = S.get("g_create") or S.get("g_dispatch")
        if busy:
            st.caption("Налаштування заблоковані, поки йде запуск")
        elif gid:
            n = len(group_by_id(gid)["deviceIds"])
            cfg = load_cfg()
            st.caption(f"{n_obj(n)} → {cfg.batches(n)} {plural(cfg.batches(n), ('пачка', 'пачки', 'пачок'))}")
        if st.button("Вийти", disabled=busy, width="stretch"):
            for k in list(S.keys()):
                del S[k]
            st.rerun()
        if st.button("Оновити список груп", disabled=busy, width="stretch"):
            try:
                S.groups, S.devices, S.scans = client.groups(), {}, {}
            except ApiError as e:
                st.error(str(e))


# ---------------------------------------------------------------- вкладка 1: створення
def create_tab() -> None:
    job: CreateJob | None = S.create_job
    if job is not None and S.create_stage == "progress":
        create_progress(job)
        return

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
                rows.append({"Об'єкт": d.name, "IMEI": d.imei, "Останнє повідомлення": ago(d.last_message),
                             f"Право «{PERM_CREATE}»": {True: "Є", False: "Немає"}.get(d.can_create, "—"),
                             f"Команда «{name}»" if name else "Команда": state})
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=360)

    ready = bool(name and text.strip()) and bool(allowed) and not any_running()
    label = f"Створити на {n_obj(to_create, OBJ_LOC)}" if scan and name else f"Створити на {n_obj(len(allowed), OBJ_LOC)}"
    b1, b2 = st.columns([2, 3], vertical_alignment="center")
    if b1.button(label, type="primary", disabled=not ready, width="stretch"):
        job = CreateJob(S.client, group_by_id(gid), devices, name, text, load_cfg(), scan=scan)
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


def create_progress(job: CreateJob) -> None:
    run_every = 1 if job.running else None

    @st.fragment(run_every=run_every)
    def live() -> None:
        c = job.counts()
        done = c.get(CREATED, 0) + c.get(SKIPPED, 0) + c.get(ERROR, 0)
        with st.container(border=True):
            h1, h2 = st.columns([3, 2], vertical_alignment="bottom")
            h1.markdown(f"#### Створення «{job.name}» у групі «{job.group['name']}»")
            if job.running:
                h2.caption(f"Пачка {job.batch_no} з {job.batch_total} · пауза {job.cfg.pause:g} с між пачками")
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
                st.success(f"Готово: створено {fmt_n(c.get(CREATED, 0))}, пропущено {fmt_n(c.get(SKIPPED, 0))}. "
                           "Можна переходити до вкладки «2 · Відправка і результати».")
        elif job.status == "stopped":
            st.warning(f"Зупинено. Не оброблено {n_obj(c.get(QUEUED, 0))} — їх можна дообробити кнопкою «Повторити».")
        elif job.status == "error":
            st.error(job.message)

        rows = job.snapshot()
        df = pd.DataFrame([{"Об'єкт": r["name"], "IMEI": r["imei"], "Статус": LABELS[r["status"]],
                            "Деталі": r["detail"]} for r in rows])
        b = st.columns(4)
        if b[0].button("Зупинити", disabled=not job.running, width="stretch", key="c_stop"):
            job.stop()
        left = c.get(ERROR, 0) + c.get(QUEUED, 0)
        if b[1].button(f"Повторити помилки ({fmt_n(left)})", disabled=job.running or not left,
                       width="stretch", key="c_retry"):
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
        st.dataframe(styled(view), hide_index=True, width="stretch", height=420)
        if run_every and not job.running:
            st.rerun()

    live()


# ---------------------------------------------------------------- вкладка 2: відправка
def build_targets(devices: list[Device], scan: ScanJob, name: str, text: str, mode: str, online_min: int,
                  prev: DispatchJob | None):
    targets, skipped = [], []
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

    if S.get("g_dispatch") is None and S.get("g_create") is not None:
        S.g_dispatch = S.g_create
    gid = group_select("g_dispatch")
    if gid is None:
        return
    devices = get_devices(gid)
    if devices is None:
        return
    scan = scan_block(gid, devices, "d", "Щоб вибрати команду, прочитаю команди кожного об'єкта групи "
                                         "(пачками, як налаштовано ліворуч).")
    if scan is None:
        return
    names = scan.names()
    if not names:
        st.info("На об'єктах цієї групи команд немає. Спершу створіть команду на вкладці «1 · Створення команди».")
        return
    name_counts = dict(names)
    default = S.get("last_name") or S.get("c_name", "").strip()
    idx = next((i for i, (n, _) in enumerate(names) if n == default), 0)
    name = st.selectbox("Команда", [n for n, _ in names], index=idx, key=f"d_cmd_{gid}",
                        format_func=lambda n: f"{n} — є на {fmt_n(name_counts[n])} з {n_obj(len(devices))}")
    variants = scan.variants(name)
    if len(variants) == 1:
        text = variants[0][0]
        st.markdown("<span style='font-size:14px'>Текст команди на об'єктах</span>", unsafe_allow_html=True)
        st.code(text or "(порожній)", language=None)
    else:
        vc = dict(variants)
        text = st.selectbox("Текст команди (на об'єктах він різний)", [t for t, _ in variants], key=f"d_txt_{gid}_{name}",
                            format_func=lambda t: f"{t or '(порожній)'} — {n_obj(vc[t], OBJ_LOC)}")
        others = sum(c for t, c in variants if t != text)
        st.warning(f"На {n_obj(others, OBJ_LOC)} команда «{name}» має інший текст — їх не надсилаємо.")

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
    with c2:
        wait = st.number_input("Чекати відповідь, хв", min_value=1, max_value=180, key="d_wait")
        st.caption("Після цього об'єкти без відповіді отримають статус «Без відповіді». Пізню відповідь можна "
                   "дочитати кнопкою «Перевірити відповіді ще раз».")

    targets, skipped = build_targets(devices, scan, name, text, mode, online_min, prev)
    if priority:
        targets = by_priority(targets)
    stale = sum(1 for d, _ in targets if activity(d.last_message, online_min) == STALE)
    if stale:
        st.caption(f"{n_obj(stale)} давно не виходили на зв'язок — за {int(wait)} хв вони, найімовірніше, не "
                   "відповідять. Потім їм можна надіслати ще раз окремим запуском.")
    reasons: dict[str, int] = {}
    for _, r in skipped:
        key = r.split(":")[0] if r.startswith(("Офлайн", "Не вдалося")) else r
        reasons[key] = reasons.get(key, 0) + 1
    b1, b2 = st.columns([2, 3], vertical_alignment="center")
    if b1.button(f"Надіслати на {n_obj(len(targets))}", type="primary", width="stretch",
                 disabled=not targets or any_running()):
        job = DispatchJob(S.client, group_by_id(gid), name, text, targets, skipped, load_cfg(),
                          wait_minutes=float(wait), poll_every=float(S.get("cfg_poll", 15)), online_min=online_min)
        S.dispatch_job, S.dispatch_stage = job, "progress"
        job.start()
        st.rerun()
    if reasons:
        b2.caption("Пропускаємо: " + "; ".join(f"{r[:1].lower() + r[1:]} — {fmt_n(n)}" for r, n in
                                                sorted(reasons.items(), key=lambda x: -x[1])))


def resend(job: DispatchJob) -> None:
    ids = set(job.not_answered())
    devices = {d.id: d for d in S.devices.get(job.group["id"], [])}
    targets = []
    for did in ids:
        r = job.rows[did]
        dev = devices.get(did) or Device(id=did, name=r["name"], imei=r["imei"], last_message=r["last_message"])
        targets.append((dev, int(r["command_id"])))
    new = DispatchJob(S.client, job.group, job.name, job.text, by_priority(targets), [], load_cfg(),
                      wait_minutes=job.wait_minutes, poll_every=float(S.get("cfg_poll", 15)),
                      online_min=job.online_min)
    S.prev_dispatch[(job.group["id"], job.name, job.text)] = job
    S.dispatch_job = new
    new.start()


def dispatch_progress(job: DispatchJob) -> None:
    run_every = 1 if job.running else None

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
        retry_n = len(job.not_answered())
        if b[1].button(f"Ще раз — без відповіді ({fmt_n(retry_n)})", width="stretch",
                       disabled=job.running or not retry_n, key="d_resend",
                       help="Новий запуск лише для тих, хто не відповів, не отримав команду або отримав помилку"):
            resend(job)
            st.rerun()
        rows = job.snapshot()
        # у порядку відправки (онлайн першими, якщо увімкнено пріоритет), пропущені — в кінці
        rows.sort(key=lambda r: (r["order"] is None, r["order"] or 0, r["name"].lower()))
        df = pd.DataFrame([{
            "№": str(r["order"] or ""), "Об'єкт": r["name"], "Активність": act_label(r["activity"]),
            "Останнє повідомлення": ago(r["last_message"]), "Надіслано": fmt_time(r["sent_at"]),
            "Статус": LABELS[r["status"]], "Відповідь трекера / деталі": r["answer"] or r["detail"],
            "Отримано": fmt_time(r["answered_at"]), "IMEI": r["imei"],
        } for r in rows])
        b[2].download_button("Завантажити звіт CSV", csv_bytes(df), width="stretch", disabled=job.running,
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
        st.dataframe(styled(view), hide_index=True, width="stretch", height=460)
        if run_every and not job.running:
            S.prev_dispatch[(job.group["id"], job.name, job.text)] = job
            st.rerun()

    live()


# ---------------------------------------------------------------- запуск
if S.client is None or not S.client.token:
    login_page()
else:
    sidebar()
    st.title("Масові команди")
    t1, t2 = st.tabs(["1 · Створення команди", "2 · Відправка і результати"])
    with t1:
        create_tab()
    with t2:
        dispatch_tab()
