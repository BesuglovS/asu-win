# -*- coding: utf-8 -*-
"""
Загрузка оценок преподавателя (Безуглов С.В.) в АСУ РСО (https://asurso.ru/)
из базы данных проекта j-web.

Источник данных: SQLite j-web (db/app.db) — таблицы lessons/marks/subjects/
students/classes. Приёмник: электронный классный журнал АСУ РСО
(«Журнал» → классный журнал; экран выставления оценок).

Почему браузер, а не прямые HTTP-запросы: АСУ РСО — Angular-приложение с
серверными postback'ами и webapi, требующим токен приложения (`at`). Мы
управляем реальным интерфейсом (Playwright), как в проекте C:\\r.

Режимы:
  python grades_upload.py list      — показать оценки из источника j-web (без браузера)
  python grades_upload.py fetch     — скачать оценки с боевого j-web (HTTP-экспорт)
  python grades_upload.py login     — проверка авто-входа (вход + выход)
  python grades_upload.py discover  — разведка экрана журнала АСУ РСО
  python grades_upload.py probe     — типы заданий и сетка класса (чтение)
  python grades_upload.py scan      — предметы класса и содержимое сеток (чтение)
  python grades_upload.py selftest  — безопасный тест записи (переустановка оценки)
  python grades_upload.py plan      — сопоставить данные j-web с АСУ РСО → plan.json
  python grades_upload.py run       — залить оценки (по options.json)
  python grades_upload.py verify    — сверить оценки на сайте с БД j-web
  python grades_upload.py assign    — темы уроков (из КТП) и ДЗ (из j-web)
  python grades_upload.py clear     — убрать оценки в РОДНОМ предмете для предметов,
                                      перенаправленных классовым правилом (см. ниже)
  python grades_upload.py dedupe    — убрать устаревшие дубли оценок резервного типа
                                      (type 10 поверх штатной type 3)

Правило по классам: options.class_grade_subject (напр. 7/8/9 → «Информатика») —
все предметы этих классов из j-web пишутся в указанный предмет АСУ РСО. Для
перенесённых предметов старые оценки в родном предмете убирает режим `clear`.

Опции: options.json (dry_run, org, only_classes, only_subjects, months, limit,
force, pause, db_path, class_grade_subject, cleanup_duplicates).
Прогресс: progress.json (ключи «класс||предмет||урок»).
Журнал: run.log (UTF-8). Модалки: modals.log.
"""

import json
import os
import re
import signal
import sys
import time
from datetime import date as _date
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import asurso_common as a          # noqa: E402
import jweb                        # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OPTIONS_FILE = BASE / "options.json"
PROGRESS_FILE = BASE / "progress.json"
PLAN_FILE = BASE / "plan.json"
SNAPSHOT_FILE = BASE / "jweb_snapshot.json"
DISC_DIR = BASE / "grades_discovery"
LOG_FILE = BASE / "run.log"

# Адрес боевого j-web (HTTP-экспорт оценок: GET /api/export/grades)
DEFAULT_JWEB_URL = "https://j.nayanovaacademy.ru"

ORG_KEY = "Наянов"          # основной филиал (не УСОПДОД)
MENU_TOP = "Журнал"
# возможные подписи пункта классного журнала (в разных версиях АСУ РСО)
MENU_JOURNAL_CANDIDATES = ["Классный журнал", "Журнал", "Оценки", "Успеваемость"]

_CURRENT = {"page": None}


def note(s):
    line = str(s)
    try:
        print(line, flush=True)
    except Exception:
        print(line.encode("utf-8", "backslashreplace").decode("utf-8", "replace"), flush=True)
    try:
        with open(str(LOG_FILE), "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _sigint(sig, frame):
    note("Прервано пользователем — выходим...")
    p = _CURRENT["page"]
    if p is not None:
        try:
            a.logout(p)
        except Exception:
            pass
    sys.exit(130)


def load_options():
    cfg = {}
    if OPTIONS_FILE.exists():
        try:
            cfg = json.loads(OPTIONS_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            note("options.json не прочитан: %r" % exc)
    return cfg


def load_progress():
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"done": [], "errors": {}}


def save_progress(p):
    try:
        PROGRESS_FILE.write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:
        note("progress не сохранён: %r" % exc)


# ==========================================================================
# источник оценок: БД j-web / снимок / HTTP-экспорт
# ==========================================================================
def _jweb_url(cfg):
    return (os.environ.get("JWEB_URL") or cfg.get("jweb_url") or DEFAULT_JWEB_URL).rstrip("/")


def fetch_model(cfg, save=True):
    """Тянет оценки с боевого j-web (GET /api/export/grades) и нормализует."""
    import urllib.request
    import urllib.error
    token = os.environ.get("JWEB_EXPORT_TOKEN")
    if not token:
        raise RuntimeError("JWEB_EXPORT_TOKEN не задан в .env")
    url = _jweb_url(cfg) + "/api/export/grades"
    req = urllib.request.Request(url, headers={
        "X-Export-Token": token,
        "Accept": "application/json",
        "User-Agent": "asu-win",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise RuntimeError("HTTP %s: %s" % (exc.code, detail))
    except Exception as exc:
        raise RuntimeError("не удалось получить экспорт: %r" % exc)
    data = json.loads(raw)
    if "classes" not in data:
        raise RuntimeError("неожиданный ответ j-web: %s" % list(data)[:5])
    p = BASE / (cfg.get("snapshot_path") or "jweb_snapshot.json")
    if save:
        try:
            p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as exc:
            note("снимок не сохранён: %r" % exc)
    return jweb.load_snapshot(data=data)


def load_model_any(cfg):
    """Выбор источника: options.jweb_source = db | snapshot | http | auto.

    auto (по умолчанию): HTTP, если задан JWEB_EXPORT_TOKEN; иначе снимок, если
    он есть; иначе локальная БД j-web.
    """
    src = (cfg.get("jweb_source") or "auto").lower()
    snap = BASE / (cfg.get("snapshot_path") or "jweb_snapshot.json")
    if src == "db":
        return jweb.load_model(cfg.get("db_path")), "db"
    if src == "snapshot":
        return jweb.load_snapshot(snap), "snapshot"
    if src == "http":
        return fetch_model(cfg), "http"
    if os.environ.get("JWEB_EXPORT_TOKEN"):
        try:
            return fetch_model(cfg), "http"
        except Exception as exc:
            note("HTTP-экспорт j-web недоступен (%s) — fallback" % exc)
    if snap.exists():
        return jweb.load_snapshot(snap), "snapshot"
    return jweb.load_model(cfg.get("db_path")), "db"


def cmd_fetch():
    cfg = load_options()
    note("Источник j-web: %s/api/export/grades" % _jweb_url(cfg))
    try:
        model = fetch_model(cfg, save=True)
    except Exception as exc:
        note("Ошибка HTTP-экспорта: %s" % exc)
        note("Проверьте JWEB_URL/JWEB_EXPORT_TOKEN в .env и что j-web задеплоен "
             "(токен на сервере: db/export_token.txt).")
        return 3
    summ = jweb.summarize(model)
    note("Получено: %(classes)d классов, %(subjects)d предметов, %(lessons)d уроков, "
         "%(marks)d оценок" % summ)
    note("Снимок сохранён: %s" % (cfg.get("snapshot_path") or "jweb_snapshot.json"))
    return 0


# ==========================================================================
# режим list — только БД j-web
# ==========================================================================
def cmd_list():
    model, kind = load_model_any(load_options())
    summ = jweb.summarize(model)
    note("Источник (%s): %s" % (kind, model["source_db"]))
    note("Классов: %d, предметов: %d, уроков: %d, оценок: %d" % (
        summ["classes"], summ["subjects"], summ["lessons"], summ["marks"]))
    note("-" * 74)
    for c in model["classes"]:
        for s in c["subjects"]:
            note("%-6s  %-28s  уроков: %2d  оценок: %3d" % (
                c["name"], s["name"], len(s["lessons"]),
                sum(len(l["marks"]) for l in s["lessons"])))
            for l in s["lessons"]:
                marks = ", ".join("%s=%d" % (m["fio"].split()[0], m["value"])
                                  for m in l["marks"] if m["value"] is not None)
                note("        %s  %s  [%s]" % (
                    l["date"], (l["topic"] or "")[:30], marks[:120]))
    return 0


# ==========================================================================
# навигация по меню АСУ РСО
# ==========================================================================
def open_top_menu(page, top):
    """Открывает выпадающее меню верхнего уровня `top`."""
    loc = page.get_by_text(top, exact=True)
    for i in range(loc.count()):
        if a._visible(loc.nth(i)):
            try:
                loc.nth(i).click(timeout=8000)
                page.wait_for_timeout(1500)
                a.dismiss_modals(page, where="top %s" % top)
                return True
            except Exception:
                pass
    return False


def click_text_visible(page, text, timeout=8000):
    loc = page.get_by_text(text, exact=True)
    for i in range(loc.count()):
        if a._visible(loc.nth(i)):
            try:
                loc.nth(i).click(timeout=timeout)
                return True
            except Exception:
                pass
    # нестрогое совпадение
    loc = page.get_by_text(text)
    for i in range(loc.count()):
        if a._visible(loc.nth(i)):
            try:
                loc.nth(i).click(timeout=timeout)
                return True
            except Exception:
                pass
    return False


def goto_journal(page):
    """Открывает классный журнал через меню: Журнал → <пункт журнала>."""
    a.dismiss_modals(page, where="pre journal")
    if not open_top_menu(page, MENU_TOP):
        note("Не найдено верхнее меню «%s»." % MENU_TOP)
        return False
    page.wait_for_timeout(1200)
    # что вообще есть в выпадающем меню
    items = page.eval_on_selector_all(
        ".dropdown-menu a, .dropdown-menu li a, .nav a",
        "els => els.map(e => (e.innerText||'').replace(/\\s+/g,' ').trim()).filter(Boolean)")
    note("Пункты меню после «%s»: %s" % (MENU_TOP, sorted(set(items))[:40]))
    for cand in MENU_JOURNAL_CANDIDATES:
        if click_text_visible(page, cand):
            page.wait_for_timeout(5000)
            a.dismiss_modals(page, where="journal opened")
            note("Открыт журнал (пункт «%s»): %s" % (cand, page.url))
            return True
    # запасной вариант — прямой URL Angular (может сбросить сессию)
    note("Пункт журнала не найден — пробую прямой адрес /angular/school/journal/assignments/")
    try:
        page.goto(a.site_url() + "/angular/school/journal/assignments/",
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        a.dismiss_modals(page, where="journal direct")
        return a.looks_logged_in(page)
    except Exception as exc:
        note("прямой переход не удался: %r" % exc)
        return False


def _opts(page, name):
    return page.eval_on_selector_all(
        "select[name=%s] option" % name,
        "els=>els.map(e=>({v:e.value,t:(e.innerText||'').replace(/\\s+/g,' ').trim()}))")


def select_by_label(page, name, value=None, label=None, index=None):
    sel = page.locator("select[name=%s]" % name)
    if not sel.count():
        return False
    try:
        if value is not None:
            sel.first.select_option(value=value)
        elif label is not None:
            sel.first.select_option(label=label)
        elif index is not None:
            sel.first.select_option(index=index)
        page.wait_for_timeout(1500)
        return True
    except Exception:
        return False


# ==========================================================================
# режим discover
# ==========================================================================
def cmd_discover():
    from playwright.sync_api import sync_playwright

    cfg = load_options()
    org = cfg.get("org") or ORG_KEY
    net = a.NetLog(DISC_DIR / "network.log", capture_bodies=True)

    with sync_playwright() as pw:
        ctx, page = a.launch(pw)
        _CURRENT["page"] = page
        net.attach(page)
        a.capture_at(page)
        a.login(ctx, page, org_key=org)
        note("Вход выполнен: %s" % (a._org_rendered(page) or a.site_url()))

        if not goto_journal(page):
            note("Журнал не открылся. Снимаю текущий экран.")
            a.dump_screen(page, DISC_DIR, "after_login", net)
            a.logout(page)
            ctx.close()
            return 2

        page.wait_for_timeout(3000)
        summ = a.dump_screen(page, DISC_DIR, "journal", net)

        # пытаемся загрузить сетку: класс/предмет/период + кнопка «Загрузить»
        selects = {s.get("name"): s for s in summ.get("selects", []) if s.get("name")}
        note("Select'ы экрана журнала: %s" % list(selects.keys()))
        class_name = _pick_select_name(selects, ["PCLID_IUP", "CLASSID", "class", "PCLID"])
        subj_name = _pick_select_name(selects, ["SGID", "SUBJID", "subject", "Subject"])
        period_name = _pick_select_name(selects, ["TERMID", "PERIOD", "period", "Term"])
        if class_name:
            _select_first_real(page, class_name)
        if subj_name:
            _select_first_real(page, subj_name)
        if period_name:
            _select_first_real(page, period_name)
        for btn in ("Загрузить", "Показать", "Применить"):
            if click_text_visible(page, btn):
                note("Нажата кнопка «%s»." % btn)
                break
        page.wait_for_timeout(5000)
        a.dismiss_modals(page, where="after load")
        a.dump_screen(page, DISC_DIR, "journal_loaded", net)

        note("Полный HTML/скриншоты/сеть — в %s" % DISC_DIR)
        a.logout(page)
        ctx.close()
    return 0


def cmd_probe():
    """Разведка данных журнала: типы заданий, сетка, безопасный POST-зонд."""
    from playwright.sync_api import sync_playwright
    from urllib.parse import urlparse, parse_qs
    import urllib.parse

    cfg = load_options()
    org = cfg.get("org") or ORG_KEY
    net = a.NetLog(DISC_DIR / "network_probe.log", capture_bodies=True)

    with sync_playwright() as pw:
        ctx, page = a.launch(pw)
        _CURRENT["page"] = page
        net.attach(page)
        a.capture_at(page)
        a.login(ctx, page, org_key=org)
        if not goto_journal(page):
            a.logout(page); ctx.close(); return 2
        page.wait_for_timeout(2500)
        a.find_at(page)

        types = a.api_get(page, "/webapi/grade/assignment/types?all=False")
        note("assignment/types -> %s" % types.get("status"))
        note(json.dumps(types.get("body"), ensure_ascii=False)[:1500])

        cls = cfg.get("probe_class")
        subj = cfg.get("probe_subject")
        _select_class_and_subject(page, cls, subj)
        page.wait_for_timeout(4000)
        for btn in ("Загрузить", "Показать"):
            if click_text_visible(page, btn):
                note("Нажата «%s»" % btn)
                break
        page.wait_for_timeout(4000)

        # ищем реальный URL сетки и перезапрашиваем его
        jump = None
        for rec in reversed(net.records):
            if "/webapi/grade/journal?" in rec.get("url", ""):
                jump = rec["url"]
                break
        if jump:
            q = parse_qs(urlparse(jump).query)
            note("grid params: %s" % {k: v[0] for k, v in q.items()})
            path = "/webapi/grade/journal?" + urllib.parse.urlencode({k: v[0] for k, v in q.items()})
            r = a.api_get(page, path)
            note("grade/journal -> %s" % r.get("status"))
            save = DISC_DIR / "journal_data.json"
            save.write_text(json.dumps(r.get("body"), ensure_ascii=False, indent=1), encoding="utf-8")
            note("сетка сохранена: %s" % save)
            b = r.get("body") or {}
            for jr in (b.get("journals") or []):
                note("  класс %s / %s / %s: учеников %d, дат %d, заданий %d" % (
                    jr.get("className"), jr.get("subjectGroupName"), jr.get("teacherName"),
                    len(jr.get("students") or []), len(jr.get("classMeeting") or []),
                    len(jr.get("assignments") or [])))
                for cm in (jr.get("classMeeting") or [])[:8]:
                    note("     дата %s cmId=%s lessonId=%s relay=%s слот=%s" % (
                        str(cm.get("date"))[:10], cm.get("id"), cm.get("lessonId"),
                        cm.get("relay"), cm.get("scheduleTimeNum")))
                for asg in (jr.get("assignments") or [])[:8]:
                    note("     задание id=%s typeId=%s '%s' cmId=%s" % (
                        asg.get("id"), asg.get("typeId"), asg.get("assignmentName"),
                        asg.get("classMeetingId")))
        else:
            note("URL сетки не найден в перехвате.")

        # безопасный зонд quickedit: пустые массивы (ничего не меняет)
        sgid = _first_real_option(page, "SGID")
        cls_opt = _opts(page, "PCLID_IUP")
        cls_val = next((o for o in cls_opt if o["v"] not in ("", "0")), None)
        if sgid and cls_val:
            cls_id = (cls_val["v"].split("_")[0])
            r = a.api_post(page, "/webapi/grade/journal/quickedit", {
                "sgId": int(sgid["v"]), "classId": int(cls_id),
                "grade": int((cls_val["v"].split("_")[1]) if "_" in cls_val["v"] else 0),
                "results": [], "attendance": [],
            })
            note("quickedit(empty) -> %s %s" % (r.get("status"), json.dumps(r.get("body"), ensure_ascii=False)[:300]))

        a.logout(page)
        ctx.close()
    return 0


def _select_class_and_subject(page, class_want=None, subj_want=None):
    opts = _opts(page, "PCLID_IUP")
    val = None
    if class_want:
        want = jweb.norm_class(class_want)
        for o in opts:
            if jweb.norm_class(o["t"]) == want:
                val = o["v"]; break
    if val is None:
        val = next((o["v"] for o in opts if o["v"] not in ("", "0")), None)
    if val:
        select_by_label(page, "PCLID_IUP", value=val)
        page.wait_for_timeout(2500)
    subs = _opts(page, "SGID")
    sv = None
    if subj_want:
        want = jweb.norm_name(subj_want)
        for o in subs:
            if jweb.norm_name(o["t"]) == want or want in jweb.norm_name(o["t"]):
                sv = o["v"]; break
    if sv is None:
        sv = next((o["v"] for o in subs if o["v"] not in ("", "0")), None)
    if sv:
        select_by_label(page, "SGID", value=sv)
        page.wait_for_timeout(2500)
    return val, sv


def _first_real_option(page, name):
    for o in _opts(page, name):
        if o["v"] not in ("", "0", "-1", "null"):
            return o
    return None


def _pick_select_name(selects, candidates):
    for c in candidates:
        if c in selects:
            return c
    for name in selects:
        for c in candidates:
            if c.lower() in (name or "").lower():
                return name
    return None


def _select_first_real(page, name):
    opts = _opts(page, name)
    for o in opts:
        t = (o.get("t") or "").strip().lower()
        if o.get("v") in ("", "0", "-1", "null") or t in ("", "все", "выберите"):
            continue
        if select_by_label(page, name, value=o["v"]):
            note("  select %s = %r" % (name, o["t"]))
            return o
    return None


# ==========================================================================
# сопоставление и загрузка
# ==========================================================================
# Алиасы предметов j-web → АСУ РСО (если названия расходятся). Ключ и значение
# нормализуются norm_name. Дополняются из options.subject_aliases.
SUBJECT_ALIASES = {}


def _opts_one(page, name, n=0):
    """Опции n-го select[name=...] (для дублирующихся имён, напр. SGID)."""
    return page.eval_on_selector_all(
        "select[name=%s]" % name,
        """(els, n) => {
             if (!els.length) return [];
             const s = els[Math.min(n, els.length - 1)];
             return [...s.options].map(o => ({v: o.value, t: (o.innerText||'').replace(/\\s+/g,' ').trim()}));
           }""",
        n)


def _class_base_digit(class_name):
    """«8 А1» → ('8А','1'); «8 А» → ('8А', None). Цифра в конце = подгруппа."""
    n = jweb.norm_class(class_name)
    m = re.match(r"^(.*\D)([1-9])$", n)
    if m:
        return m.group(1), m.group(2)
    return n, None


def _grade_of(class_norm):
    """Номер параллели из норм. имени класса: «9А1» → '9'."""
    m = re.match(r"^(\d+)", class_norm or "")
    return m.group(1) if m else ""


def _native_subject(subject_name):
    """Родной предмет АСУ РСО для имени j-web (только alias, без правила по классу)."""
    return SUBJECT_ALIASES.get(subject_name, subject_name)


def _target_subject(cfg, class_norm, subject_name):
    """Целевой предмет АСУ РСО с учётом правила по параллели.

    options.class_grade_subject: {"7": "Информатика", ...} — ВСЕ предметы этой
    параллели направляются в указанный предмет. Иначе — алиас (SUBJECT_ALIASES)
    или сам предмет.
    """
    mp = cfg.get("class_grade_subject") or {}
    g = _grade_of(class_norm)
    if g and g in mp:
        return mp[g]
    return _native_subject(subject_name)


def _is_moved(cfg, class_norm, subject_name):
    """Предмет перенаправлен классовым правилом (цель ≠ родной предмет АСУ РСО)."""
    return (jweb.norm_name(_target_subject(cfg, class_norm, subject_name))
            != jweb.norm_name(_native_subject(subject_name)))


def _is_preserved(preserve, class_norm):
    """Класс защищён от перезаписи/удаления (options.preserve_classes).

    Учитывается и базовый класс: подгруппа «7В1»/«7В2» наследует защиту «7В».
    """
    if not preserve or not class_norm:
        return False
    if class_norm in preserve:
        return True
    base, _digit = _class_base_digit(class_norm)
    return base in preserve


def _match_class_option(opts, class_name):
    base, _digit = _class_base_digit(class_name)
    for o in opts:
        if jweb.norm_class(o["t"]) == base:
            return o
    return None


def _match_subject_option(opts, subject_name):
    res = _match_subject_options(opts, subject_name)
    return res[0] if res else None


def _match_subject_options(opts, subject_name, group_digit=None):
    """Все подходящие subject group. При group_digit предпочитаются подгруппы «…гр.N»."""
    want = jweb.norm_name(SUBJECT_ALIASES.get(subject_name, subject_name))
    if not want:
        return []
    exact, partial = [], []
    for o in opts:
        base = jweb.norm_name((o["t"] or "").split("/")[0])
        full = jweb.norm_name(o["t"])
        if base == want or full == want:
            exact.append(o)
        elif want in base or base in want:
            partial.append(o)
    res = exact or partial
    if group_digit and len(res) > 1:
        pref = [o for o in res
                if re.search(r"[Гг]р\.?\s*%s" % re.escape(str(group_digit)), o["t"] or "")]
        if pref:
            return pref
    return res


def _type_map(types, cfg, work_types=None):
    """work_type (j-web, свободный текст) → id типа задания АСУ РСО.

    Разрешение по каждому фактическому значению work_type: точное имя типа →
    частичное → эвристика по ключевым словам (ДЗ/сам/контроль/тест/урок…).
    Явные пары из options.work_type_map (по названию) применяются первыми.
    """
    by_norm = {}
    for t in types or []:
        if isinstance(t, dict) and t.get("isActive", True):
            by_norm[jweb.norm_name(t.get("name"))] = t.get("id")
    out = {}
    for wt, name in (cfg.get("work_type_map") or {}).items():
        out[wt] = _resolve_type(name, by_norm)
    for wt in (work_types or []):
        if wt not in out:
            out[wt] = _resolve_type(wt, by_norm)
    return out, by_norm


_TYPE_RULES = [
    (("ДЗ", "ДОМАШ"), "ДОМАШНЕЕЗАДАНИЕ"),
    (("САМ", "СР"), "САМОСТОЯТЕЛЬНАЯРАБОТА"),
    (("КОНТРОЛ", "КР", "ДИКТАНТ"), "КОНТРОЛЬНАЯРАБОТА"),
    (("ПРОЕКТ",), "ПРОЕКТ"),
    (("РЕФЕРАТ",), "РЕФЕРАТ"),
    (("ТЕСТ", "КВИЗ", "TEST"), "ТЕСТИРОВАНИЕ"),
    (("ПРАКТИК",), "ПРАКТИЧЕСКАЯРАБОТА"),
    (("УРОК", "ОТВЕТ"), "ОТВЕТНАУРОКЕ"),
]


def _resolve_type(label, by_norm):
    n = jweb.norm_name(label)
    if not n:
        return None
    if n in by_norm:
        return by_norm[n]
    for k, v in by_norm.items():
        if k and (k in n or n in k):
            return v
    for keys, target in _TYPE_RULES:
        if any(kk in n for kk in keys):
            return by_norm.get(target)
    return None


def _is_homework_conflict(resp):
    """409 «На выбранный Вами урок уже назначено Домашнее задание».

    Возникает, когда в уроке уже есть домашнее задание (напр. после `assign`),
    и оценку типа «Домашнее задание» туда добавить нельзя.
    """
    if resp.get("status") != 409:
        return False
    b = resp.get("body")
    msg = str(b.get("message") or "") if isinstance(b, dict) else str(b or "")
    return "Домашнее задание" in msg


def _student_match(students, fio):
    for s in students or []:
        if jweb.match_fio(fio, s.get("fullName") or ""):
            return s.get("id")
    return None


def _last_journal_url(net):
    for rec in reversed(net.records):
        if "/webapi/grade/journal?" in rec.get("url", ""):
            return rec["url"]
    return None


def _load_grid(page, net):
    """Жмёт «Загрузить» и возвращает journals[0] свежей сетки (или None)."""
    for btn in ("Загрузить", "Показать"):
        if click_text_visible(page, btn):
            break
    page.wait_for_timeout(3500)
    a.dismiss_modals(page, where="grid")
    url = None
    for _ in range(12):
        url = _last_journal_url(net)
        if url:
            break
        page.wait_for_timeout(700)
    if not url:
        return None, None
    r = a.api_get(page, url)
    if r.get("status") != 200:
        return None, url
    jrs = (r.get("body") or {}).get("journals") or []
    return (jrs[0] if jrs else None), url


def _build_results(jr, subject, wt2id, misses, tol_days=0, clear_marks=False,
                   protect_types=None):
    """Оценки j-web → results quickedit. Возвращает (list, matched_lesson_ids).

    tol_days>0 — если точной даты нет, берётся ближайшее занятие в пределах
    допуска (записи о переносе складываются в misses['remap']).
    clear_marks=True — mark=None (очистка существующей оценки).
    В результат добавляется `assignId`, если задание нужного типа уже есть
    у занятия (иначе сервер создаёт новое и «ДЗ» конфликтует).
    protect_types — набор typeId; если в клетке уже стоит оценка этого типа
    (или целевого `aid`), значение НЕ перезаписывается (skip) и попадает в
    misses['kept']. Для классов, где оценки ставит не только наш пользователь.
    """
    cms = {}
    for cm in jr.get("classMeeting") or []:
        d = str(cm.get("date") or "")[:10]
        if d and d not in cms:
            cms[d] = cm
    cm_dates = []
    for d, cm in cms.items():
        try:
            cm_dates.append((_date.fromisoformat(d), cm))
        except Exception:
            pass
    amap = {}
    for a in jr.get("assignments") or []:
        try:
            amap[(int(a.get("classMeetingId") or 0), int(a.get("typeId") or 0))] = int(a.get("id"))
        except Exception:
            pass
    site = set()
    if protect_types:
        a2 = {a.get("id"): a for a in jr.get("assignments") or []}
        for m in jr.get("marks") or []:
            a = a2.get(m.get("assignmentId"))
            if a and m.get("mark"):
                try:
                    site.add((int(a["classMeetingId"]), int(m["studentId"]),
                              int(a["typeId"])))
                except Exception:
                    pass
    students = jr.get("students") or []
    out = []
    matched = set()
    for l in subject["lessons"]:
        d = str(l.get("date") or "")[:10]
        cm = cms.get(d)
        if cm is not None:
            matched.add(l["lesson_id"])
        elif tol_days > 0:
            try:
                ld = _date.fromisoformat(d)
            except Exception:
                ld = None
            if ld is not None:
                best = bestdiff = None
                for cd, c in cm_dates:
                    diff = abs((cd - ld).days)
                    if diff <= tol_days and (bestdiff is None or diff < bestdiff):
                        best, bestdiff = c, diff
                if best is not None:
                    cm = best
                    matched.add(l["lesson_id"])
                    misses.setdefault("remap", []).append(
                        (d, str(best.get("date"))[:10], bestdiff))
        if cm is None:
            continue
        for m in l["marks"]:
            if m.get("value") is None:
                continue
            sid = _student_match(students, m["fio"])
            aid = wt2id.get(m["work_type"])
            if not sid:
                misses.setdefault("no_student", []).append((d, m["fio"]))
                continue
            if not aid:
                misses.setdefault("no_type", []).append(m["work_type"])
                continue
            if protect_types:
                cid, sid_i = int(cm["id"]), int(sid)
                protect = {int(aid)} | {int(t) for t in protect_types}
                if any((cid, sid_i, t) in site for t in protect):
                    misses.setdefault("kept", []).append((d, m["fio"], int(aid)))
                    continue
            entry = {"cmId": int(cm["id"]), "assignTypeId": int(aid),
                     "studentId": int(sid),
                     "mark": None if clear_marks else int(m["value"]), "dutyMark": 0}
            ex = amap.get((int(cm["id"]), int(aid)))
            if ex:
                entry["assignId"] = ex
            out.append(entry)
    return out, matched


def _dedupe(results):
    seen = {}
    for r in results:
        seen[(r["cmId"], r["studentId"], r["assignTypeId"])] = r
    return list(seen.values())


def cmd_scan():
    """Только чтение: перебирает предметы одного класса и показывает содержимое сетки."""
    from playwright.sync_api import sync_playwright
    cfg = load_options()
    org = cfg.get("org") or ORG_KEY
    want_class = os.environ.get("PROBE_CLASS") or cfg.get("probe_class") or "1а"
    with sync_playwright() as pw:
        ctx, page = a.launch(pw)
        _CURRENT["page"] = page
        net = a.NetLog(DISC_DIR / "network_scan.log", capture_bodies=False)
        net.attach(page)
        a.capture_at(page)
        a.login(ctx, page, org_key=org)
        if not goto_journal(page):
            a.logout(page); ctx.close(); return 2
        page.wait_for_timeout(2500)
        a.find_at(page)
        cop = _match_class_option(_opts_one(page, "PCLID_IUP"), want_class)
        if not cop:
            note("класс %s не найден" % want_class)
            a.logout(page); ctx.close(); return 2
        select_by_label(page, "PCLID_IUP", value=cop["v"])
        page.wait_for_timeout(2500)
        subs = []
        for _ in range(20):
            subs = _opts_one(page, "SGID")
            if len(subs) > 1:
                break
            page.wait_for_timeout(500)
        note("Класс %s, предметов: %d" % (cop["t"], len(subs)))
        for o in subs:
            if o["v"] in ("", "0"):
                continue
            select_by_label(page, "SGID", value=o["v"])
            page.wait_for_timeout(2000)
            jr, url = _load_grid(page, net)
            if not jr:
                note("  %-40s — сетка пуста" % o["t"][:40])
                continue
            marks = jr.get("marks") or []
            note("  %-40s дат=%d заданий=%d оценок=%d" % (
                o["t"][:40], len(jr.get("classMeeting") or []),
                len(jr.get("assignments") or []), len(marks)))
            if marks:
                note("     marks[0]=%s" % json.dumps(marks[0], ensure_ascii=False))
                note("     assignments[0]=%s" % json.dumps((jr.get("assignments") or [{}])[0], ensure_ascii=False))
                save = DISC_DIR / ("scan_%s.json" % re.sub(r"\W+", "_", o["t"])[:40])
                save.write_text(json.dumps(jr, ensure_ascii=False, indent=1), encoding="utf-8")
        a.logout(page)
        ctx.close()
    return 0


def cmd_selftest():
    """Безопасный тест записи: переустанавливает СУЩЕСТВУЮЩУЮ оценку тем же значением."""
    from playwright.sync_api import sync_playwright
    cfg = load_options()
    org = cfg.get("org") or ORG_KEY
    want_class = os.environ.get("PROBE_CLASS") or cfg.get("probe_class") or "8а"
    want_subj = os.environ.get("PROBE_SUBJECT") or cfg.get("probe_subject") or "Биология"
    with sync_playwright() as pw:
        ctx, page = a.launch(pw)
        _CURRENT["page"] = page
        net = a.NetLog(DISC_DIR / "network_selftest.log", capture_bodies=False)
        net.attach(page)
        a.capture_at(page)
        a.login(ctx, page, org_key=org)
        if not goto_journal(page):
            a.logout(page); ctx.close(); return 2
        page.wait_for_timeout(2500)
        a.find_at(page)
        cop = _match_class_option(_opts_one(page, "PCLID_IUP"), want_class)
        select_by_label(page, "PCLID_IUP", value=cop["v"])
        page.wait_for_timeout(2500)
        subs = []
        for _ in range(20):
            subs = _opts_one(page, "SGID")
            if len(subs) > 1:
                break
            page.wait_for_timeout(500)
        sops = _match_subject_options(subs, want_subj)
        if not sops:
            note("предмет %s не найден" % want_subj); a.logout(page); ctx.close(); return 2
        select_by_label(page, "SGID", value=sops[0]["v"])
        page.wait_for_timeout(2000)
        jr = None
        for term in _opts_one(page, "TERMID"):
            select_by_label(page, "TERMID", value=term["v"])
            page.wait_for_timeout(800)
            jr, _u = _load_grid(page, net)
            if jr and (jr.get("marks") or []):
                break
        if not jr or not (jr.get("marks") or []):
            note("нет оценок для теста"); a.logout(page); ctx.close(); return 2
        asg_by_id = {x["id"]: x for x in (jr.get("assignments") or [])}
        target = None
        for m in jr["marks"]:
            if m.get("mark") and m.get("assignmentId") in asg_by_id and not m.get("duty"):
                target = m; break
        if not target:
            note("нет подходящей оценки (с числовым значением)"); a.logout(page); ctx.close(); return 2
        asg = asg_by_id[target["assignmentId"]]
        value = int(target["mark"])
        payload = {
            "sgId": int(jr["subjectGroupId"]), "classId": int(jr["classId"]),
            "grade": int(jr["grade"]),
            "results": [{"cmId": int(asg["classMeetingId"]), "assignTypeId": int(asg["typeId"]),
                         "studentId": int(target["studentId"]), "mark": value, "dutyMark": 0}],
            "attendance": [],
        }
        note("Тест: класс %s / %s, ученик %s, тип %s, значение %d (переустановка тем же)" % (
            jr["className"], jr["subjectGroupName"], target["studentId"], asg["typeId"], value))
        r = a.api_post(page, "/webapi/grade/journal/quickedit", payload)
        note("quickedit -> %s %s" % (r.get("status"), json.dumps(r.get("body"), ensure_ascii=False)[:200]))
        # перечитать и убедиться, что значение не изменилось
        jr2, _u = _load_grid(page, net)
        same = False
        for m in (jr2 or {}).get("marks") or []:
            if m.get("assignmentId") == target["assignmentId"] and m.get("studentId") == target["studentId"]:
                same = (str(m.get("mark")) == str(value))
        note("Проверка: значение на сайте %s" % ("без изменений" if same else "изменилось/не найдено"))
        a.logout(page)
        ctx.close()
    return 0


def cmd_verify():
    """Сверка: сопоставляет оценки j-web с оценками на сайте АСУ РСО (только чтение)."""
    from playwright.sync_api import sync_playwright
    cfg = load_options()
    SUBJECT_ALIASES.update(cfg.get("subject_aliases") or {})
    org = cfg.get("org") or ORG_KEY
    only_classes = [jweb.norm_class(x) for x in (cfg.get("only_classes") or [])]
    only_subjects = [jweb.norm_name(x) for x in (cfg.get("only_subjects") or [])]
    preserve = {jweb.norm_class(x) for x in (cfg.get("preserve_classes") or [])}
    tol_days = int(cfg.get("date_tolerance_days") or 0)
    model, _src = load_model_any(cfg)
    lines = []
    tot = {"desired": 0, "present": 0, "missing": 0, "mismatch": 0,
           "nodate": 0, "preserved": 0}

    with sync_playwright() as pw:
        ctx, page = a.launch(pw)
        _CURRENT["page"] = page
        net = a.NetLog(DISC_DIR / "network_verify.log", capture_bodies=False)
        net.attach(page)
        a.capture_at(page)
        a.login(ctx, page, org_key=org)
        if not goto_journal(page):
            a.logout(page); ctx.close(); return 2
        page.wait_for_timeout(2500)
        a.find_at(page)
        types = a.api_get(page, "/webapi/grade/assignment/types?all=False").get("body") or []
        wts = sorted({m["work_type"] for c in model["classes"] for s in c["subjects"]
                      for l in s["lessons"] for m in l["marks"]})
        wt2id, by_norm = _type_map(types, cfg, wts)
        homework_id = _resolve_type("Домашнее задание", by_norm)
        fallback_id = _resolve_type(cfg.get("homework_fallback_type") or "Ответ на уроке",
                                    by_norm)
        class_opts = _opts_one(page, "PCLID_IUP")
        term_opts = _opts_one(page, "TERMID")
        try:
            for c in model["classes"]:
                if only_classes and c["norm"] not in only_classes:
                    continue
                cop = _match_class_option(class_opts, c["name"])
                if not cop:
                    lines.append("класс %s не найден" % c["name"])
                    continue
                select_by_label(page, "PCLID_IUP", value=cop["v"])
                page.wait_for_timeout(2500)
                _cbase, cdigit = _class_base_digit(c["norm"])
                subs = []
                for _ in range(20):
                    subs = _opts_one(page, "SGID")
                    if len(subs) > 1:
                        break
                    page.wait_for_timeout(500)
                for s in c["subjects"]:
                    if only_subjects and not any(x in s["norm_name"] for x in only_subjects):
                        continue
                    matched = set()
                    desired = {}
                    site = {}
                    for sop in _match_subject_options(
                            subs, _target_subject(cfg, c["norm"], s["name"]), cdigit):
                        select_by_label(page, "SGID", value=sop["v"])
                        page.wait_for_timeout(2000)
                        for term in term_opts:
                            select_by_label(page, "TERMID", value=term["v"])
                            page.wait_for_timeout(800)
                            jr, _u = _load_grid(page, net)
                            if not jr:
                                continue
                            asg = {x["id"]: x for x in (jr.get("assignments") or [])}
                            for m in jr.get("marks") or []:
                                if not m.get("mark"):
                                    continue
                                x = asg.get(m.get("assignmentId"))
                                if x:
                                    site[(int(x["classMeetingId"]), int(m["studentId"]),
                                          int(x["typeId"]))] = int(m["mark"])
                            misses = {}
                            part, mtd = _build_results(jr, {"lessons": s["lessons"]}, wt2id, misses, tol_days)
                            matched |= mtd
                            for r in part:
                                desired[(r["cmId"], r["studentId"], r["assignTypeId"])] = r["mark"]
                            if len(matched) >= len(s["lessons"]):
                                break
                    nodate = len([l for l in s["lessons"] if l["lesson_id"] not in matched])
                    is_preserve = _is_preserved(preserve, c["norm"])
                    d, p, miss, mism, prot = 0, 0, 0, 0, 0
                    for key, want in desired.items():
                        d += 1
                        k = key
                        if (k not in site and homework_id and fallback_id
                                and key[2] == homework_id):
                            alt = (key[0], key[1], fallback_id)
                            if alt in site:
                                k = alt
                        if k not in site:
                            miss += 1
                            tot["missing"] += 1
                        elif site[k] != want:
                            if is_preserve:
                                prot += 1
                                tot["preserved"] += 1
                            else:
                                mism += 1
                                tot["mismatch"] += 1
                        else:
                            p += 1
                            tot["present"] += 1
                    tot["desired"] += d
                    tot["nodate"] += nodate
                    lines.append("[%s / %s] в j-web: %d, совпало: %d, нет на сайте: %d, иное: %d%s, нет даты: %d" % (
                        c["name"], s["name"], d, p, miss, mism,
                        (" (+%d не перезаписано)" % prot) if prot else "",
                        nodate))
        finally:
            note("-" * 74)
            for r in lines:
                note(r)
            note("Итого: оценок в j-web %d, совпало %d, отсутствуют %d, расходятся %d, не перезаписано чужих %d, нет даты %d" % (
                tot["desired"], tot["present"], tot["missing"], tot["mismatch"],
                tot["preserved"], tot["nodate"]))
            try:
                (BASE / "verify.txt").write_text("\n".join(lines), encoding="utf-8")
                note("Отчёт: verify.txt")
            except Exception:
                pass
            try:
                a.logout(page)
            except Exception:
                pass
            try:
                ctx.close()
            except Exception:
                pass
    return 0


def _site_marks(jr):
    """{(cmId, studentId, typeId): mark} из сетки журнала (как в verify)."""
    asg = {x["id"]: x for x in (jr.get("assignments") or [])}
    out = {}
    for m in jr.get("marks") or []:
        if not m.get("mark"):
            continue
        x = asg.get(m.get("assignmentId"))
        if x:
            out[(int(x["classMeetingId"]), int(m["studentId"]),
                 int(x["typeId"]))] = int(m["mark"])
    return out


def cmd_clear():
    """Убирает оценки в РОДНОМ предмете АСУ РСО для предметов, перенаправленных
    классовым правилом (options.class_grade_subject) в другой предмет.

    Сейчас это «Труд (технология)» 9А–9Г → «Информатика»: после переноса копии
    остаются и в «Труде» — их нужно очистить. Используется тот же quickedit, но
    с `assignId` существующего задания и `mark: null` (как в UI). По умолчанию
    dry_run.
    """
    from playwright.sync_api import sync_playwright

    cfg = load_options()
    SUBJECT_ALIASES.update(cfg.get("subject_aliases") or {})
    write = not bool(cfg.get("dry_run", True))
    org = cfg.get("org") or ORG_KEY
    only_classes = [jweb.norm_class(x) for x in (cfg.get("only_classes") or [])]
    only_subjects = [jweb.norm_name(x) for x in (cfg.get("only_subjects") or [])]
    preserve = {jweb.norm_class(x) for x in (cfg.get("preserve_classes") or [])}
    tol_days = int(cfg.get("date_tolerance_days") or 0)
    pause = float(cfg.get("pause", 0.4))

    model, _src = load_model_any(cfg)
    note("=" * 74)
    note("Режим очистки родного предмета перенаправленных: %s"
         % ("ЗАПИСЬ" if write else "ПРОСМОТР (без изменений)"))
    stats = {"cells": 0, "cleared": 0, "errors": 0}

    with sync_playwright() as pw:
        ctx, page = a.launch(pw)
        _CURRENT["page"] = page
        net = a.NetLog(DISC_DIR / "network_clear.log", capture_bodies=False)
        net.attach(page)
        a.capture_at(page)
        a.login(ctx, page, org_key=org)
        if not goto_journal(page):
            a.logout(page)
            ctx.close()
            return 2
        page.wait_for_timeout(2500)
        a.find_at(page)
        types = a.api_get(page, "/webapi/grade/assignment/types?all=False").get("body") or []
        wts = sorted({m["work_type"] for c in model["classes"] for s in c["subjects"]
                      for l in s["lessons"] for m in l["marks"]})
        wt2id, _ = _type_map(types, cfg, wts)
        class_opts = _opts_one(page, "PCLID_IUP")
        term_opts = _opts_one(page, "TERMID")
        try:
            for c in model["classes"]:
                if only_classes and c["norm"] not in only_classes:
                    continue
                if _is_preserved(preserve, c["norm"]):
                    note("[%s] защищённый класс — очистка пропущена" % c["name"])
                    continue
                moved_subjects = [s for s in c["subjects"]
                                  if _is_moved(cfg, c["norm"], s["name"])]
                if not moved_subjects:
                    continue
                cop = _match_class_option(class_opts, c["name"])
                if not cop:
                    note("[%s] класс не найден в АСУ РСО" % c["name"])
                    stats["errors"] += 1
                    continue
                select_by_label(page, "PCLID_IUP", value=cop["v"])
                page.wait_for_timeout(2500)
                a.dismiss_modals(page, where="clear class")
                _cbase, cdigit = _class_base_digit(c["norm"])
                sg_opts = []
                for _ in range(20):
                    sg_opts = _opts_one(page, "SGID")
                    if len(sg_opts) > 1:
                        break
                    page.wait_for_timeout(500)
                for s in moved_subjects:
                    if only_subjects and not any(x in s["norm_name"] for x in only_subjects):
                        continue
                    # родной предмет (без классового правила)
                    sops = _match_subject_options(sg_opts, s["name"], cdigit)
                    if not sops:
                        note("[%s / %s] родной предмет не найден" % (c["name"], s["name"]))
                        continue
                    for sop in sops:
                        select_by_label(page, "SGID", value=sop["v"])
                        page.wait_for_timeout(2000)
                        a.dismiss_modals(page, where="clear subject")
                        for term in term_opts:
                            jr, _u = _load_grid(page, net)
                            if not jr:
                                continue
                            site = _site_marks(jr)
                            wants, _mtd = _build_results(
                                jr, {"lessons": s["lessons"]}, wt2id, {}, tol_days,
                                clear_marks=True)
                            results = []
                            seen = set()
                            for x in wants:
                                k = (x["cmId"], x["studentId"], x["assignTypeId"])
                                if k in site and k not in seen and x.get("assignId"):
                                    seen.add(k)
                                    results.append(x)
                            if not results:
                                continue
                            stats["cells"] += len(results)
                            note("[%s / %s / %s / %s] к очистке: %d" % (
                                c["name"], s["name"][:20], sop["t"][:20], term["t"], len(results)))
                            if not write:
                                continue
                            payload = {
                                "sgId": int(jr["subjectGroupId"]),
                                "classId": int(jr["classId"]),
                                "grade": int(jr["grade"]),
                                "results": results,
                                "attendance": [],
                            }
                            res = a.api_post(page, "/webapi/grade/journal/quickedit", payload)
                            if res.get("status") in (200, 204):
                                stats["cleared"] += len(results)
                                note("   → очищено %d" % len(results))
                            else:
                                stats["errors"] += 1
                                note("   ! ошибка очистки: %s %s" % (
                                    res.get("status"),
                                    json.dumps(res.get("body"), ensure_ascii=False)[:200]))
                            if pause:
                                page.wait_for_timeout(int(pause * 1000))
        except StopIteration:
            note("Очистка прервана.")
        finally:
            note("-" * 74)
            note("К очистке клеток: %d, очищено: %d, ошибок: %d" % (
                stats["cells"], stats["cleared"], stats["errors"]))
            if not write:
                note("Это был ПРОСМОТР — данные НЕ изменены.")
            try:
                a.logout(page)
            except Exception:
                pass
            try:
                ctx.close()
            except Exception:
                pass
    return 0


def cmd_dedupe():
    """Убирает устаревшие дубли оценок резервного типа.

    Дубль — запись типа `homework_fallback_type` (по умолчанию «Ответ на
    уроке», typeId 10), которая не нужна ни одному предмету j-web, но
    повторяет штатную «Домашнее задание» (typeId 3) в той же клетке
    (cmId, studentId) тем же значением. Такие записи остаются от прошлых
    прогонов, когда «ДЗ» не удалось записать в своё задание и сработал
    резервный тип. Позднее штатная type 3 записалась, а старая type 10
    осталась отдельной колонкой.

    Безопасные условия очистки:
      - type 10 не входит в ожидаемые оценки ни одного предмета j-web;
      - в той же клетке есть ожидаемая type 3 с тем же значением;
      - у задания type 10 нет `issueClassMeetingId` (след резервной записи);
      - класс не входит в `preserve_classes`.

    По умолчанию `dry_run: true` — только отчёт. Запись — при `dry_run: false`.
    """
    from playwright.sync_api import sync_playwright

    cfg = load_options()
    SUBJECT_ALIASES.update(cfg.get("subject_aliases") or {})
    write = not bool(cfg.get("dry_run", True))
    tol_days = int(cfg.get("date_tolerance_days") or 0)
    preserve = {jweb.norm_class(x) for x in (cfg.get("preserve_classes") or [])}
    org = cfg.get("org") or ORG_KEY

    model, _src = load_model_any(cfg)
    note("=" * 74)
    note("Очистка дублей оценок: %s" % ("ЗАПИСЬ" if write else "ПРОСМОТР (без изменений)"))
    note("Анализируются все классы и предметы j-web; фильтры only_* не применяются.")

    stats = {"site": 0, "candidates": 0, "removed": 0, "skipped": 0, "errors": 0}
    report = {"mode": "write" if write else "plan", "candidates": [],
              "removed": [], "skipped": [], "errors": []}
    site, desired, grids, loaded = [], {}, {}, set()

    with sync_playwright() as pw:
        ctx, page = a.launch(pw)
        _CURRENT["page"] = page
        net = a.NetLog(DISC_DIR / "network_dedupe.log", capture_bodies=False)
        net.attach(page)
        a.capture_at(page)
        try:
            a.login(ctx, page, org_key=org)
            if not goto_journal(page):
                note("Не удалось открыть классный журнал.")
                return 2
            page.wait_for_timeout(2500)
            a.find_at(page)

            types = a.api_get(page, "/webapi/grade/assignment/types?all=False").get("body") or []
            wts = sorted({m["work_type"] for c in model["classes"] for s in c["subjects"]
                          for l in s["lessons"] for m in l["marks"]})
            wt2id, by_norm = _type_map(types, cfg, wts)
            homework_id = _resolve_type("Домашнее задание", by_norm)
            fallback_name = cfg.get("homework_fallback_type") or "Ответ на уроке"
            fallback_id = _resolve_type(fallback_name, by_norm)
            type_names = {int(t["id"]): (t.get("name") or str(t["id"]))
                          for t in types if t.get("id") is not None}
            if not homework_id or not fallback_id or fallback_id == homework_id:
                note("Типы «Домашнее задание»/«%s» не различаются — очистка невозможна."
                     % fallback_name)
                return 2

            class_opts = []
            for _ in range(20):
                class_opts = _opts_one(page, "PCLID_IUP")
                if class_opts:
                    break
                page.wait_for_timeout(700)
            term_opts = _opts_one(page, "TERMID")
            if not class_opts or not term_opts:
                note("Не удалось получить фильтры классов/периодов.")
                return 2

            for c in model["classes"]:
                cop = _match_class_option(class_opts, c["name"])
                if not cop:
                    continue
                select_by_label(page, "PCLID_IUP", value=cop["v"])
                page.wait_for_timeout(2500)
                a.dismiss_modals(page, where="dedupe class %s" % c["name"])
                sg_opts = []
                for _ in range(20):
                    sg_opts = _opts_one(page, "SGID")
                    if len(sg_opts) > 1:
                        break
                    page.wait_for_timeout(500)
                _cbase, cdigit = _class_base_digit(c["norm"])

                pairs = []
                for s in c["subjects"]:
                    target_name = _target_subject(cfg, c["norm"], s["name"])
                    for sop in _match_subject_options(sg_opts, target_name, cdigit):
                        pairs.append((s, sop))

                # грузим каждую группу/период один раз
                unique_sops = {p[1]["v"]: p[1] for p in pairs}
                for sop in unique_sops.values():
                    select_by_label(page, "SGID", value=sop["v"])
                    page.wait_for_timeout(2000)
                    a.dismiss_modals(page, where="dedupe subject %s" % sop["t"])
                    for term in term_opts:
                        gkey = (cop["v"], sop["v"], term["v"])
                        if gkey in loaded:
                            continue
                        select_by_label(page, "TERMID", value=term["v"])
                        page.wait_for_timeout(700)
                        jr, _url = _load_grid(page, net)
                        if not jr:
                            continue
                        loaded.add(gkey)
                        grids[gkey] = jr
                        sgid = int(jr["subjectGroupId"])
                        asg = {x.get("id"): x for x in (jr.get("assignments") or [])}
                        students = {int(x["id"]): x.get("fullName")
                                    for x in (jr.get("students") or [])}
                        cms = {int(x["id"]): x for x in (jr.get("classMeeting") or [])}
                        for m in jr.get("marks") or []:
                            if not m.get("mark"):
                                continue
                            x = asg.get(m.get("assignmentId"))
                            if not x:
                                continue
                            try:
                                cmid = int(x["classMeetingId"])
                                sid = int(m["studentId"])
                                tid = int(x["typeId"])
                            except Exception:
                                continue
                            cm = cms.get(cmid) or {}
                            site.append({
                                "sgId": sgid,
                                "classId": int(jr["classId"]),
                                "grade": int(jr["grade"]),
                                "class": jr.get("className"),
                                "group": jr.get("subjectGroupName"),
                                "cmId": cmid,
                                "date": str(cm.get("date") or "")[:10],
                                "studentId": sid,
                                "student": students.get(sid),
                                "typeId": tid,
                                "type": type_names.get(tid, str(tid)),
                                "assignmentId": int(x["id"]),
                                "issueCmId": x.get("issueClassMeetingId"),
                                "mark": str(m.get("mark")),
                                "class_v": cop["v"],
                                "sg_v": sop["v"],
                                "term_v": term["v"],
                            })

                # ожидаемые оценки: все предметы, попавшие в эту группу
                for s, sop in pairs:
                    for term in term_opts:
                        jr = grids.get((cop["v"], sop["v"], term["v"]))
                        if not jr:
                            continue
                        sgid = int(jr["subjectGroupId"])
                        part, _mtd = _build_results(
                            jr, {"lessons": s["lessons"]}, wt2id, {}, tol_days)
                        for r in part:
                            key = (sgid, int(r["cmId"]), int(r["studentId"]),
                                   int(r["assignTypeId"]))
                            desired.setdefault(key, []).append({
                                "mark": str(r["mark"]),
                                "class": c["name"],
                                "subject": s["name"],
                            })

            # ---------- поиск безопасных дублей ----------
            site_by_key = {}
            for rec in site:
                site_by_key[(rec["sgId"], rec["cmId"], rec["studentId"],
                             rec["typeId"], rec["assignmentId"])] = rec
            by_type = {}
            for rec in site_by_key.values():
                by_type.setdefault((rec["sgId"], rec["cmId"], rec["studentId"],
                                    rec["typeId"]), []).append(rec)

            candidates = []
            for key, recs in by_type.items():
                if key in desired:
                    continue
                for rec in recs:
                    if int(rec["typeId"]) != int(fallback_id):
                        continue
                    if rec.get("issueCmId") not in (None, 0, "0", ""):
                        continue
                    if _is_preserved(preserve, jweb.norm_class(rec["class"] or "")):
                        continue
                    keep_keys = [k for k in desired
                                 if k[0] == key[0] and k[1] == key[1]
                                 and k[2] == key[2] and k[3] == homework_id]
                    keep_site = [r for r in by_type.get(
                        (key[0], key[1], key[2], homework_id), [])
                        if r["mark"] == rec["mark"]]
                    if not keep_keys or not keep_site:
                        continue
                    item = dict(rec)
                    item["keep_assignmentId"] = keep_site[0]["assignmentId"]
                    item["keep_mark"] = keep_site[0]["mark"]
                    candidates.append(item)

            stats["site"] = len(site_by_key)
            stats["candidates"] = len(candidates)
            report["candidates"] = candidates
            note("Оценок на сайте: %d; дублей-кандидатов: %d" % (
                stats["site"], stats["candidates"]))
            for x in candidates:
                note("  %s / %s | %s | %s | type %s (%s), asg %s = %s; "
                     "штатная type 3 asg %s" % (
                         x.get("class"), x.get("group"), x.get("date"),
                         x.get("student"), x.get("typeId"), x.get("type"),
                         x.get("assignmentId"), x.get("mark"),
                         x.get("keep_assignmentId")))

            if write and candidates:
                groups = {}
                for x in candidates:
                    groups.setdefault((x["class_v"], x["sg_v"], x["term_v"]), []).append(x)

                for (class_v, sg_v, term_v), items in groups.items():
                    select_by_label(page, "PCLID_IUP", value=class_v)
                    page.wait_for_timeout(2000)
                    select_by_label(page, "SGID", value=sg_v)
                    page.wait_for_timeout(1500)
                    select_by_label(page, "TERMID", value=term_v)
                    page.wait_for_timeout(700)
                    jr, _url = _load_grid(page, net)
                    if not jr:
                        stats["errors"] += len(items)
                        report["errors"].append({"items": items, "why": "сетка не загрузилась"})
                        continue
                    asg = {x.get("id"): x for x in (jr.get("assignments") or [])}
                    cur = {}
                    for m in jr.get("marks") or []:
                        if not m.get("mark"):
                            continue
                        x = asg.get(m.get("assignmentId"))
                        if x:
                            cur[(int(x["classMeetingId"]), int(m["studentId"]),
                                 int(x["typeId"]), int(x["id"]))] = str(m["mark"])
                    keep = {}
                    for (cmid, sid, tid, _aid), mark in cur.items():
                        if tid == homework_id:
                            keep.setdefault((cmid, sid), []).append(mark)

                    valid = []
                    for x in items:
                        aid = int(x["assignmentId"])
                        cmid, sid = int(x["cmId"]), int(x["studentId"])
                        mark = str(x["mark"])
                        if cur.get((cmid, sid, fallback_id, aid)) != mark:
                            stats["skipped"] += 1
                            report["skipped"].append({"item": x, "why": "запись изменилась"})
                            continue
                        if mark not in keep.get((cmid, sid), []):
                            stats["skipped"] += 1
                            report["skipped"].append({"item": x, "why": "нет штатной type 3"})
                            continue
                        valid.append(x)
                    if not valid:
                        continue

                    payload = {
                        "sgId": int(jr["subjectGroupId"]),
                        "classId": int(jr["classId"]),
                        "grade": int(jr["grade"]),
                        "results": [{
                            "cmId": int(x["cmId"]),
                            "assignTypeId": fallback_id,
                            "studentId": int(x["studentId"]),
                            "mark": None,
                            "dutyMark": 0,
                            "assignId": int(x["assignmentId"]),
                        } for x in valid],
                        "attendance": [],
                    }
                    r = a.api_post(page, "/webapi/grade/journal/quickedit", payload)
                    if r.get("status") in (200, 204):
                        stats["removed"] += len(valid)
                        report["removed"].extend(valid)
                        note("  → %s / %s: удалено %d" % (
                            items[0].get("class"), items[0].get("group"), len(valid)))
                    else:
                        stats["errors"] += len(valid)
                        report["errors"].append({
                            "items": valid, "status": r.get("status"),
                            "body": r.get("body"),
                        })
                        note("  ! %s / %s: ошибка %s %s" % (
                            items[0].get("class"), items[0].get("group"),
                            r.get("status"),
                            json.dumps(r.get("body"), ensure_ascii=False)[:200]))
                    page.wait_for_timeout(1200)
                    a.dismiss_modals(page, where="after dedupe")

                # контрольная перепроверка затронутых групп
                note("Проверка после очистки:")
                for (class_v, sg_v, term_v), items in groups.items():
                    select_by_label(page, "PCLID_IUP", value=class_v)
                    page.wait_for_timeout(1500)
                    select_by_label(page, "SGID", value=sg_v)
                    page.wait_for_timeout(1200)
                    select_by_label(page, "TERMID", value=term_v)
                    page.wait_for_timeout(600)
                    jr, _url = _load_grid(page, net)
                    if not jr:
                        note("  ! не удалось перечитать %s / %s" % (
                            items[0].get("class"), items[0].get("group")))
                        continue
                    asg = {x.get("id"): x for x in (jr.get("assignments") or [])}
                    cur = {}
                    for m in jr.get("marks") or []:
                        if not m.get("mark"):
                            continue
                        x = asg.get(m.get("assignmentId"))
                        if x:
                            cur[(int(x["classMeetingId"]), int(m["studentId"]),
                                 int(x["typeId"]), int(x["id"]))] = str(m["mark"])
                    left = 0
                    kept = 0
                    for x in items:
                        if (int(x["cmId"]), int(x["studentId"]), fallback_id,
                                int(x["assignmentId"])) in cur:
                            left += 1
                        if any(k[0] == int(x["cmId"]) and k[1] == int(x["studentId"])
                               and k[2] == homework_id and v == str(x["mark"])
                               for k, v in cur.items()):
                            kept += 1
                    note("  %s / %s: осталось дублей %d, штатных на месте %d/%d" % (
                        items[0].get("class"), items[0].get("group"),
                        left, kept, len(items)))
        finally:
            note("-" * 74)
            note("Дублей найдено: %d, удалено: %d, пропущено: %d, ошибок: %d" % (
                stats["candidates"], stats["removed"], stats["skipped"], stats["errors"]))
            if not write:
                note("Это был ПРОСМОТР — данные НЕ изменены.")
            try:
                report["stats"] = stats
                (DISC_DIR / "duplicates.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
                note("Отчёт: duplicates.json")
            except Exception as exc:
                note("duplicates.json не сохранён: %r" % exc)
            try:
                a.logout(page)
            except Exception:
                pass
            try:
                ctx.close()
            except Exception:
                pass
    return 0


def _md(value):
    """Дата → (месяц, день): 'YYYY-MM-DD' или 'DD.MM' или None."""
    s = str(value or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return (int(m.group(2)), int(m.group(3)))
    m = re.match(r"^(\d{1,2})\.(\d{1,2})$", s)
    if m:
        return (int(m.group(2)), int(m.group(1)))
    return None


def _in_range(d, fr, to):
    if d is None:
        return True
    if fr and d < fr:
        return False
    if to and d > to:
        return False
    return True


def _row_md(row):
    try:
        a_ = row.locator("td").first.locator("a")
        if a_.count():
            return _md((a_.first.inner_text() or "").strip())
    except Exception:
        pass
    return None


def _click_load_assign(page):
    loc = page.locator("button[ng-click=\"$ctrl.getAssignments()\"]")
    if loc.count():
        loc.first.click()
    else:
        click_text_visible(page, "Загрузить")
    page.wait_for_timeout(3500)
    a.dismiss_modals(page, where="assign load")


def _eligible_theme_links(page, fr, to):
    """Список (locator, дата) для строк с «Тема не задана» в диапазоне."""
    out = []
    links = page.locator('a[title="Выбрать тему урока"]')
    for i in range(links.count()):
        el = links.nth(i)
        try:
            if "Тема не задана" not in (el.inner_text() or ""):
                continue
        except Exception:
            continue
        row = el.locator("xpath=ancestor::tr[1]")
        d = _row_md(row)
        if not _in_range(d, fr, to):
            continue
        out.append((el, d))
    return out


def _empty_homework_rows(page, fr, to):
    """Строки в диапазоне с пустой ячейкой ДЗ («Добавить домашнее задание»)."""
    out = []
    rows = page.locator("tr[ng-repeat-start]")
    for i in range(rows.count()):
        row = rows.nth(i)
        if not _in_range(_row_md(row), fr, to):
            continue
        add = row.get_by_text("Добавить домашнее задание", exact=False)
        try:
            if add.count() and add.first.is_visible():
                out.append(row)
        except Exception:
            pass
    return out


def _assign_themes_api(page, class_id, sg_id, term_id, year_id, fr, to):
    """Темы через API: следующая неиспользованная тема КТП на каждое пустое занятие.

    Возвращает (сколько поставлено, сообщение)."""
    plan = a.api_get(page, "/webapi/subjectplans/getForSubjectGroup?sgId=%s" % sg_id)
    lessons = (plan.get("body") or {}).get("lessons") or []
    if not lessons:
        return 0, "нет КТП"
    r = a.api_get(page, "/webapi/grade/journal?classId=%s&grade=-1&sgId=%s&termId=%s&v=1&yearId=%s"
                  % (class_id, sg_id, term_id, year_id))
    jrs = (r.get("body") or {}).get("journals") or []
    if not jrs:
        return 0, "нет сетки"
    cms = jrs[0].get("classMeeting") or []
    used = {cm.get("lessonId") for cm in cms if cm.get("lessonId")}
    order = [l["id"] for l in lessons]
    empties = [cm for cm in cms if not cm.get("lessonId")]
    empties.sort(key=lambda c: (str(c.get("date"))[:10], c.get("scheduleTimeNum") or 0))
    done = 0
    for cm in empties:
        if not _in_range(_md(str(cm.get("date"))[:10]), fr, to):
            continue
        nxt = next((lid for lid in order if lid not in used), None)
        if nxt is None:
            return done, "темы КТП закончились"
        a.api_post(page, "/webapi/grade/journal/classmeetings/%s/lesson" % cm["id"], int(nxt))
        used.add(nxt)
        done += 1
    return done, ""


def _assign_themes(page, fr, to, pause=0.4, limit=400):
    """Кликает «Тема не задана» (ставит следующую тему из КТП) по диапазону дат."""
    done = 0
    for _ in range(limit):
        cands = _eligible_theme_links(page, fr, to)
        if not cands:
            break
        before = len(cands)
        try:
            cands[0][0].click(timeout=6000)
            page.wait_for_timeout(int(max(pause, 0.8) * 1000))
        except Exception as exc:
            note("      ! тема: клик не удался: %s" % exc)
            break
        a.dismiss_modals(page, where="theme set")
        # ждём, пока таблица перезагрузится и число «Тема не задана» уменьшится
        after = before
        deadline = time.time() + 8
        while time.time() < deadline:
            after = len(_eligible_theme_links(page, fr, to))
            if after < before:
                break
            page.wait_for_timeout(500)
        if after < before:
            done += 1
            continue
        if after >= before:
            # возможно, открылся список — выбираем первый видимый пункт
            opt = page.locator(".dropdown-menu:visible li a, .dropdown-menu:visible li,"
                               " ul[uib-dropdown-menu]:visible li a, .ui-select-choices-row:visible")
            clicked = False
            for i in range(opt.count()):
                try:
                    if opt.nth(i).is_visible():
                        opt.nth(i).click(timeout=4000)
                        clicked = True
                        break
                except Exception:
                    pass
            if clicked:
                page.wait_for_timeout(int(max(pause, 0.8) * 1000))
                a.dismiss_modals(page, where="theme pick")
                after2 = len(_eligible_theme_links(page, fr, to))
                if after2 < before:
                    done += 1
                    continue
            note("      ! тема не изменилась — прерываю простановку тем")
            break
        done += 1
    return done


def _assign_homework(page, home_by_md, default, fr, to, overwrite=False, pause=0.4):
    """Проставляет ДЗ: из j-web по дате, иначе default; пустые ячейки в диапазоне."""
    total = page.locator("tr[ng-repeat-start]").count()
    done = 0
    for i in range(total):
        row = page.locator("tr[ng-repeat-start]").nth(i)
        if not _in_range(_row_md(row), fr, to):
            continue
        d = _row_md(row)
        text = (home_by_md.get(d) or default).strip()
        if not text:
            continue
        add = row.get_by_text("Добавить домашнее задание", exact=False)
        has_add = False
        try:
            has_add = add.count() > 0 and add.first.is_visible()
        except Exception:
            has_add = False
        if not has_add and not overwrite:
            continue
        try:
            if has_add:
                add.first.click()
            else:
                pen = row.locator('a[title*="дактир"], i.glyphicon-pencil, .glyphicon-pencil')
                if pen.count():
                    pen.first.click()
                else:
                    continue
            page.wait_for_timeout(int(max(pause, 0.5) * 1000))
            inp = page.locator("input[name=dynText]")
            if not inp.count():
                continue
            inp.first.fill(text)
            inp.first.press("Enter")
            page.wait_for_timeout(int(max(pause, 0.8) * 1000))
            a.dismiss_modals(page, where="homework set")
            done += 1
        except Exception as exc:
            note("      ! ДЗ: %s" % exc)
    return done


def cmd_assign():
    """Проставляет темы уроков (из КТП) и домашние задания (из j-web) в АСУ РСО."""
    from playwright.sync_api import sync_playwright

    cfg = load_options()
    SUBJECT_ALIASES.update(cfg.get("subject_aliases") or {})
    write = not bool(cfg.get("dry_run", True))
    org = cfg.get("org") or ORG_KEY
    only_classes = [jweb.norm_class(x) for x in (cfg.get("only_classes") or [])]
    only_subjects = [jweb.norm_name(x) for x in (cfg.get("only_subjects") or [])]
    do_themes = bool(cfg.get("assign_themes", True))
    do_home = bool(cfg.get("assign_homework", True))
    overwrite_home = bool(cfg.get("overwrite_homework", False))
    home_default = cfg.get("homework_default") or "Повторение изученного материала"
    pause = float(cfg.get("pause", 0.4))
    fr = _md(cfg.get("assign_from"))
    to = _md(cfg.get("assign_to"))

    model, _src = load_model_any(cfg)
    note("=" * 74)
    note("Режим: %s   темы: %s   ДЗ: %s%s" % (
        "ЗАПИСЬ" if write else "ПРОСМОТР",
        "да" if do_themes else "нет",
        "да" if do_home else "нет",
        ("   диапазон: %s–%s" % (fr, to)) if (fr or to) else ""))
    stats = {"subjects": 0, "themes": 0, "homework": 0}

    with sync_playwright() as pw:
        ctx, page = a.launch(pw)
        _CURRENT["page"] = page
        a.capture_at(page)
        a.login(ctx, page, org_key=org)
        if not goto_journal(page):
            a.logout(page); ctx.close(); return 2
        page.wait_for_timeout(2500)
        page.locator("button.assign-themes-btn").first.click()
        page.wait_for_timeout(4500)
        a.dismiss_modals(page, where="assignments screen")
        if "/assignments" not in page.url:
            note("Не открылся экран «Темы уроков и задания» (%s)." % page.url)
            a.logout(page); ctx.close(); return 2
        note("Экран: %s" % page.url)

        class_opts = _opts_one(page, "PCLID_IUP")
        term_opts = _opts_one(page, "TERMID")
        year_id = (a.api_get(page, "/webapi/years/current").get("body") or {}).get("id")
        note("Учебный год id: %s" % year_id)
        try:
            for c in model["classes"]:
                if only_classes and c["norm"] not in only_classes:
                    continue
                cop = _match_class_option(class_opts, c["name"])
                if not cop:
                    continue
                select_by_label(page, "PCLID_IUP", value=cop["v"])
                page.wait_for_timeout(2500)
                a.dismiss_modals(page, where="assign class")
                sg_opts = []
                for _ in range(20):
                    sg_opts = _opts_one(page, "SGID")
                    if len(sg_opts) > 1:
                        break
                    page.wait_for_timeout(500)
                _cbase, cdigit = _class_base_digit(c["norm"])
                for s in c["subjects"]:
                    if only_subjects and not any(x in s["norm_name"] for x in only_subjects):
                        continue
                    home_by_md = {}
                    for l in s["lessons"]:
                        hw = (l.get("homework") or "").strip()
                        if hw:
                            home_by_md[_md(l["date"])] = hw
                    sops = _match_subject_options(sg_opts, s["name"], cdigit)
                    if not sops:
                        note("[%s / %s] предмет не найден" % (c["name"], s["name"]))
                        continue
                    for sop in sops:
                        select_by_label(page, "SGID", value=sop["v"])
                        page.wait_for_timeout(2500)
                        a.dismiss_modals(page, where="assign subject")
                        for term in term_opts:
                            # есть ли уроки предмета в периоде (грубо — по наличию вообще)
                            _click_load_assign(page)
                            if not write:
                                et = len(_eligible_theme_links(page, fr, to))
                                eh = len(_empty_homework_rows(page, fr, to))
                                note("[%s / %s / %s] пустых тем: %d, пустых ДЗ: %d" % (
                                    c["name"], s["name"][:20], term["t"], et, eh))
                                break
                            t = h = 0
                            if do_themes:
                                t, msg = _assign_themes_api(page, str(cop["v"]).split("_")[0],
                                                            sop["v"], term["v"], year_id, fr, to)
                                if msg:
                                    note("      КТП: %s" % msg)
                            if do_home:
                                h = _assign_homework(page, home_by_md, home_default, fr, to,
                                                     overwrite_home, pause)
                            stats["themes"] += t
                            stats["homework"] += h
                            note("[%s / %s / %s] темы: +%d, ДЗ: +%d" % (
                                c["name"], s["name"][:20], term["t"], t, h))
                            break  # данные (почти всегда) помещаются в один период
                    stats["subjects"] += 1
        finally:
            note("-" * 74)
            note("Предметов: %d, тем поставлено: %d, ДЗ: %d" % (
                stats["subjects"], stats["themes"], stats["homework"]))
            if not write:
                note("Это был ПРОСМОТР — данные НЕ изменены.")
            try:
                a.logout(page)
            except Exception:
                pass
            try:
                ctx.close()
            except Exception:
                pass
    return 0


def cmd_plan_or_run(mode):
    from playwright.sync_api import sync_playwright

    cfg = load_options()
    SUBJECT_ALIASES.update(cfg.get("subject_aliases") or {})
    write = (mode == "run") and not bool(cfg.get("dry_run", True))
    force = bool(cfg.get("force"))
    limit = int(cfg.get("limit") or 0)
    pause = float(cfg.get("pause", 0.6))
    tol_days = int(cfg.get("date_tolerance_days") or 0)
    only_classes = [jweb.norm_class(x) for x in (cfg.get("only_classes") or [])]
    only_subjects = [jweb.norm_name(x) for x in (cfg.get("only_subjects") or [])]
    months = set(cfg.get("months") or [])
    preserve = {jweb.norm_class(x) for x in (cfg.get("preserve_classes") or [])}
    org = cfg.get("org") or ORG_KEY

    model, _src = load_model_any(cfg)
    summ = jweb.summarize(model)
    note("=" * 74)
    note("j-web: %(classes)d классов, %(subjects)d предметов, %(lessons)d уроков, %(marks)d оценок" % summ)
    note("Режим: %s" % ("ЗАПИСЬ" if write else "ПРОСМОТР (план, без изменений)"))

    progress = load_progress() if write else {"done": [], "errors": {}}
    done = set(progress.get("done") or [])

    stats = {"subjects": 0, "results": 0, "posted": 0, "errors": 0}
    plan = {"source_db": model["source_db"], "mode": mode, "classes": []}

    with sync_playwright() as pw:
        ctx, page = a.launch(pw)
        _CURRENT["page"] = page
        net = a.NetLog(DISC_DIR / "network_run.log", capture_bodies=False)
        net.attach(page)
        a.capture_at(page)
        a.login(ctx, page, org_key=org)
        if not goto_journal(page):
            note("Не удалось открыть классный журнал.")
            a.logout(page)
            ctx.close()
            return 2
        page.wait_for_timeout(2500)
        a.find_at(page)

        types = a.api_get(page, "/webapi/grade/assignment/types?all=False").get("body") or []
        wts = sorted({m["work_type"] for c in model["classes"] for s in c["subjects"]
                      for l in s["lessons"] for m in l["marks"]})
        wt2id, by_norm = _type_map(types, cfg, wts)
        note("Типы работ j-web → тип задания: %s" % {w: wt2id.get(w) for w in wts})
        homework_id = _resolve_type("Домашнее задание", by_norm)
        fallback_name = cfg.get("homework_fallback_type") or "Ответ на уроке"
        fallback_id = _resolve_type(fallback_name, by_norm)
        if fallback_id:
            note("Резервный тип при конфликте ДЗ: «%s» (id %s)" % (fallback_name, fallback_id))
        if tol_days:
            note("Допуск по дате: ±%d дн." % tol_days)

        class_opts = _opts_one(page, "PCLID_IUP")
        term_opts = _opts_one(page, "TERMID")
        note("Классов в фильтре: %d; периодов: %s" % (
            len(class_opts), [o["t"] for o in term_opts]))

        processed = 0
        try:
            for c in model["classes"]:
                if only_classes and c["norm"] not in only_classes:
                    continue
                cop = _match_class_option(class_opts, c["name"])
                if not cop:
                    note("[%s] класс не найден в АСУ РСО" % c["name"])
                    stats["errors"] += 1
                    continue
                select_by_label(page, "PCLID_IUP", value=cop["v"])
                page.wait_for_timeout(2500)
                a.dismiss_modals(page, where="class %s" % c["name"])
                _cbase, cdigit = _class_base_digit(c["norm"])
                sg_opts = []
                for _ in range(20):  # ждём наполнения списка предметов
                    sg_opts = _opts_one(page, "SGID")
                    if len(sg_opts) > 1:
                        break
                    page.wait_for_timeout(500)

                for s in c["subjects"]:
                    if only_subjects and not any(x in s["norm_name"] for x in only_subjects):
                        continue
                    skey = "%s||%s" % (c["norm"], s["norm_name"])
                    if skey in done and not force:
                        note("[%s / %s] уже загружено — пропуск" % (c["name"], s["name"]))
                        continue
                    target_name = _target_subject(cfg, c["norm"], s["name"])
                    sops = _match_subject_options(sg_opts, target_name, cdigit)
                    if not sops:
                        note("[%s / %s] предмет не найден в АСУ РСО" % (c["name"], s["name"]))
                        stats["errors"] += 1
                        continue
                    lessons_all = [l for l in s["lessons"]
                                   if not months or (l.get("date", "")[:7].replace("-", "_") in months)]
                    misses = {"no_student": [], "no_type": []}
                    matched = set()
                    groups_out = []
                    protect = ({t for t in (homework_id, fallback_id) if t}
                               if _is_preserved(preserve, c["norm"]) else None)
                    if protect:
                        note("[%s] режим сохранения: существующие оценки не перезаписываются"
                             % c["name"])
                    for sop in sops:  # их может быть несколько: подгруппы «Предмет/ГрN»
                        select_by_label(page, "SGID", value=sop["v"])
                        page.wait_for_timeout(2500)
                        a.dismiss_modals(page, where="subject %s" % s["name"])
                        results = []
                        jr_id = None
                        for term in term_opts:
                            if not lessons_all:
                                break
                            select_by_label(page, "TERMID", value=term["v"])
                            page.wait_for_timeout(1000)
                            jr, _url = _load_grid(page, net)
                            if not jr:
                                continue
                            if jr_id is None:
                                jr_id = {"sgId": jr.get("subjectGroupId"), "classId": jr.get("classId"),
                                         "grade": jr.get("grade")}
                            part, mtd = _build_results(jr, {"lessons": lessons_all}, wt2id,
                                                       misses, tol_days, protect_types=protect)
                            results.extend(part)
                            matched |= mtd
                            note("  [%s / %s / %s / %s] дат: %d, заданий: %d, оценок: %d" % (
                                c["name"], s["name"][:20], sop["t"][:20], term["t"],
                                len(jr.get("classMeeting") or []), len(jr.get("assignments") or []), len(part)))
                            if pause:
                                page.wait_for_timeout(int(pause * 1000))
                            if lessons_all and len(matched) >= len(lessons_all):
                                break  # все уроки предмета уже нашли свои даты
                        groups_out.append((sop, jr_id, _dedupe(results)))
                    total = sum(len(r) for _, _, r in groups_out)
                    no_date = len([l for l in lessons_all if l["lesson_id"] not in matched])
                    stats["subjects"] += 1
                    stats["results"] += total
                    moved = _is_moved(cfg, c["norm"], s["name"])
                    note("[%s / %s%s] групп: %d, к загрузке оценок: %d%s" % (
                        c["name"], s["name"],
                        (" → %s" % target_name) if moved else "",
                        len(groups_out), total,
                        ("  (нет даты в АСУ РСО: %d)" % no_date) if no_date else ""))
                    if misses.get("no_student"):
                        note("   ! не сопоставлены ученики: %s" % misses["no_student"][:6])
                    if misses.get("no_type"):
                        note("   ! нет типа задания: %s" % sorted(set(misses["no_type"])))
                    if misses.get("remap"):
                        note("   ~ по дате сопоставлено (j-web→сайт±дн): %s" % sorted(set(misses["remap"]))[:12])
                    if misses.get("kept"):
                        note("   ~ сохранены существующие оценки (не перезаписаны): %d" % len(misses["kept"]))

                    cplan = {"class": c["name"], "subject": s["name"],
                             "target_subject": target_name, "moved": moved,
                             "results": total, "groups": []}
                    plan["classes"].append(cplan)
                    post_err = False
                    for sop, jr_id, results in groups_out:
                        pentry = {"group": sop["t"], "results": len(results),
                                  "target": jr_id, "posted": False}
                        if write and results and jr_id and jr_id.get("sgId"):
                            # Перед записью переключаемся на ЭТУ подгруппу и перезагружаем
                            # сетку: quickedit сервер валидирует по «последней открытой»
                            # группе (last-access) и иначе отвечает 409 «нет доступа»,
                            # даже если sgId в теле верный.
                            select_by_label(page, "SGID", value=sop["v"])
                            _load_grid(page, net)
                            a.dismiss_modals(page, where="before post %s" % sop["t"])
                            payload = {
                                "sgId": int(jr_id["sgId"]),
                                "classId": int(jr_id["classId"]),
                                "grade": int(jr_id["grade"]),
                                "results": results,
                                "attendance": [],
                            }
                            r = a.api_post(page, "/webapi/grade/journal/quickedit", payload)
                            if r.get("status") in (200, 204):
                                stats["posted"] += len(results)
                                pentry["posted"] = True
                                note("   → [%s] сохранено %d оценок" % (sop["t"][:24], len(results)))
                            elif fallback_id and homework_id and _is_homework_conflict(r):
                                # Урок уже имеет ДЗ: существующие задания — как есть (assignId),
                                # а тем урокам, где задания нет, — резервным типом.
                                keep = [x for x in results if x.get("assignId")]
                                miss = [{k: v for k, v in dict(x, assignTypeId=fallback_id).items()
                                         if k != "assignId"}
                                        for x in results if not x.get("assignId")]
                                saved = 0
                                post_err = False
                                for chunk in (keep, miss):
                                    if not chunk:
                                        continue
                                    r2 = a.api_post(page, "/webapi/grade/journal/quickedit",
                                                    dict(payload, results=chunk))
                                    if r2.get("status") in (200, 204):
                                        saved += len(chunk)
                                    else:
                                        post_err = True
                                        note("   ! [%s] ошибка сохранения: %s %s" % (
                                            sop["t"][:24], r2.get("status"),
                                            json.dumps(r2.get("body"), ensure_ascii=False)[:200]))
                                        progress.setdefault("errors", {})[skey] = \
                                            "http %s" % r2.get("status")
                                if saved:
                                    stats["posted"] += saved
                                    pentry["posted"] = True
                                    pentry["fallback_type"] = fallback_name
                                    note("   → [%s] сохранено %d оценок "
                                         "(есть ДЗ: %d в своих заданиях, %d типом «%s»)"
                                         % (sop["t"][:24], saved, len(keep), len(miss),
                                            fallback_name))
                            else:
                                stats["errors"] += 1
                                post_err = True
                                note("   ! [%s] ошибка сохранения: %s %s" % (
                                    sop["t"][:24], r.get("status"),
                                    json.dumps(r.get("body"), ensure_ascii=False)[:200]))
                                progress.setdefault("errors", {})[skey] = "http %s" % r.get("status")
                        cplan["groups"].append(pentry)

                    if write and total and not post_err and not misses.get("no_student"):
                        done.add(skey)
                        progress["done"] = sorted(done)
                        progress.get("errors", {}).pop(skey, None)
                        save_progress(progress)

                    processed += 1
                    if limit and processed >= limit:
                        raise StopIteration
        except StopIteration:
            note("Достигнут лимит (%d)." % limit)
        finally:
            note("-" * 74)
            note("Предметов обработано: %d, оценок: %d" %
                 (stats["subjects"], stats["results"]))
            if write:
                note("Сохранено оценок: %d, ошибок: %d" % (stats["posted"], stats["errors"]))
            else:
                note("Это был ПРОСМОТР — данные НЕ сохранены.")
            try:
                (BASE / "plan.json").write_text(
                    json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
                note("План: plan.json")
            except Exception as exc:
                note("plan.json не сохранён: %r" % exc)
            try:
                a.logout(page)
            except Exception:
                pass
            try:
                ctx.close()
            except Exception:
                pass

    # После реальной записи — очистка устаревших дублей (если не отключена).
    if mode == "run" and write:
        if bool(cfg.get("cleanup_duplicates", True)):
            note("=" * 74)
            note("Запуск очистки устаревших дублей оценок...")
            rc = cmd_dedupe()
            if rc not in (0, None):
                return rc
        else:
            note("Очистка дублей отключена (cleanup_duplicates: false).")
    return 0


# ==========================================================================
def main():
    a.load_env()
    signal.signal(signal.SIGINT, _sigint)
    mode = sys.argv[1] if len(sys.argv) > 1 else "list"
    if mode == "list":
        return cmd_list()
    if mode == "fetch":
        return cmd_fetch()
    if mode == "login":
        return cmd_login()
    if mode == "discover":
        return cmd_discover()
    if mode == "probe":
        return cmd_probe()
    if mode == "scan":
        return cmd_scan()
    if mode == "selftest":
        return cmd_selftest()
    if mode in ("plan", "run"):
        return cmd_plan_or_run(mode)
    if mode == "verify":
        return cmd_verify()
    if mode == "clear":
        return cmd_clear()
    if mode == "dedupe":
        return cmd_dedupe()
    if mode == "assign":
        return cmd_assign()
    print(__doc__)
    return 2


def cmd_login():
    from playwright.sync_api import sync_playwright
    cfg = load_options()
    org = cfg.get("org") or ORG_KEY
    with sync_playwright() as pw:
        ctx, page = a.launch(pw)
        _CURRENT["page"] = page
        try:
            a.login(ctx, page, org_key=org)
            note("Вход выполнен: %s" % page.url)
            ok = a.logout(page)
            note("Выход: %s" % ("успешно" if ok else "не подтверждён"))
        finally:
            ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
