# -*- coding: utf-8 -*-
"""
Общие помощники для работы с АСУ РСО (https://asurso.ru/) через Playwright.

Портировано и обобщено из проекта C:\\r (vd_upload.py, attendance_report.py):
* вход в систему (форма /authorize/login, select2 организации, .env);
* закрытие модальных окон (и журнал текстов «Предупреждение о безопасности»);
* выход (обязателен в конце — иначе копятся параллельные сеансы);
* перехват webapi-запросов и токена `at` (авторизация webapi);
* GET/POST JSON через fetch внутри страницы (куки + заголовок at);
* снятие DOM/скриншота/сети для разведки экранов.

Модуль намеренно не зависит от других проектов: только stdlib + playwright.
"""

import json
import os
import re
import sys
import tempfile
import uuid
import atexit
import shutil
from pathlib import Path
from datetime import datetime

try:  # консоль Windows бывает в cp866 — не роняем вывод
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).resolve().parent
ENV_FILE = BASE / ".env"


# --------------------------------------------------------------------------
# конфигурация окружения
# --------------------------------------------------------------------------
def load_env(env_file=None):
    """Читает .env (KEY=VALUE) и наполняет os.environ (не перетирая заданные)."""
    env_file = Path(env_file) if env_file else ENV_FILE
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip())


def site_url():
    return os.environ.get("ASURSO_URL", "https://asurso.ru/").rstrip("/")


def login_url():
    return site_url() + "/authorize/login"


# --------------------------------------------------------------------------
# браузер
# --------------------------------------------------------------------------
_CURRENT = {"page": None}
_MODALS_LOG = BASE / "modals.log"
_PROFILE_BASE = "ASUWIN_PROFILE_"


def launch(pw, headless=None):
    """Запускает Chromium с уникальным временным профилем на запуск.

    Уникальный профиль + чистый вход каждый раз (сессия не переиспользуется,
    чтобы не размножать параллельные входы и не ловить «Предупреждение о
    безопасности»). Профиль удаляется при завершении процесса.
    """
    if headless is None:
        headless = bool(os.environ.get("ASU_HEADLESS"))
    d = Path(tempfile.gettempdir()) / (_PROFILE_BASE + uuid.uuid4().hex[:8])
    d.mkdir(parents=True, exist_ok=True)
    atexit.register(shutil.rmtree, str(d), True)
    # Преднастройки профиля: менеджер паролей и автозаполнение выключены,
    # чтобы Chromium не показывал плашку «Сохранить пароль?».
    try:
        prefs_dir = d / "Default"
        prefs_dir.mkdir(parents=True, exist_ok=True)
        (prefs_dir / "Preferences").write_text(json.dumps({
            "credentials_enable_service": False,
            "credentials_enable_autosignin": False,
            "profile": {
                "password_manager_enabled": False,
                "password_manager_leak_detection": False,
                "password_manager_allow_show_passwords": False,
            },
        }), encoding="utf-8")
    except Exception:
        pass
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(d),
        headless=headless,
        args=[
            "--start-maximized",
            # Не предлагать сохранять пароль (плашка «Сохранить пароль?» /
            # «Никогда»): менеджер паролей и автозаполнение выключены.
            "--disable-save-password-bubble",
            "--disable-features=PasswordManagerOnboarding,PasswordLeakDetection,"
            "AutofillServerCommunication,AutofillEnableAccountWalletStorage",
            "--password-store=basic",
        ],
        no_viewport=True,
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    _CURRENT["page"] = page
    return ctx, page


def _visible(loc):
    try:
        return loc.is_visible()
    except Exception:
        return False


def looks_logged_in(page):
    try:
        url = (page.url or "").lower()
        if any(k in url for k in ("login", "authorize", "esia", "signin")):
            return False
        return page.locator("input[type=password]").count() == 0
    except Exception:
        return False


def _org_rendered(page):
    try:
        r = page.locator(".select2-container--default .select2-selection__rendered")
        if r.count():
            return (r.first.inner_text() or "").strip()
    except Exception:
        pass
    return ""


def select_org(page, keyword=None, tries=3):
    """Выбирает организацию в select2 (AJAX-поиск). Возвращает выбранный текст.

    Филиал «(УСОПДОД) (ДО)» отличим по суффиксу «(ДО)»/«УСОПДОД» — для журнала
    нужен основной филиал «Наянов».
    """
    keyword = (keyword or os.environ.get("ASURSO_ORG") or "Наянов").strip()
    if keyword.lower() in _org_rendered(page).lower():
        return _org_rendered(page)
    last = None
    for _ in range(tries):
        try:
            page.locator(".select2-selection").first.click()
            search = page.locator(".select2-search__field")
            search.first.wait_for(state="visible", timeout=10000)
            search.first.fill("")
            search.first.type(keyword, delay=150)
            opts = page.locator(".select2-results__option")
            best = None
            for _ in range(30):
                for i in range(opts.count()):
                    t = (opts.nth(i).inner_text() or "").strip()
                    if keyword.lower() not in t.lower():
                        continue
                    if best is None:
                        best = i
                    if "(ДО)" not in t and "УСОПДОД" not in t:
                        best = i
                        break
                if best is not None:
                    break
                page.wait_for_timeout(500)
            if best is None:
                last = "вариант не найден"
                page.keyboard.press("Escape")
                page.wait_for_timeout(600)
                continue
            chosen = (opts.nth(best).inner_text() or "").strip().replace("\n", " ")
            opts.nth(best).click()
            page.wait_for_timeout(1200)
            if keyword.lower() in _org_rendered(page).lower():
                return chosen
            last = "не применилось: %r" % _org_rendered(page)
        except Exception as exc:
            last = str(exc)
            page.wait_for_timeout(800)
    raise RuntimeError("не удалось выбрать организацию '%s': %s" % (keyword, last))


def try_autofill(page, org_key=None):
    """Подставляет организацию/логин/пароль из .env и отправляет форму."""
    login = os.environ.get("ASURSO_LOGIN")
    password = os.environ.get("ASURSO_PASSWORD")
    if not login or not password:
        return False
    try:
        select_org(page, org_key or os.environ.get("ASURSO_ORG"))
    except Exception as exc:
        print("Не удалось выбрать организацию: %s" % exc)
        return False
    try:
        page.fill("input[name=loginname]", login)
        page.fill("input[name=password]", password)
    except Exception:
        return False
    for sel in ("div.primary-button", ".primary-button", "button:has-text('Войти')"):
        loc = page.locator(sel)
        if loc.count():
            try:
                loc.first.click()
                page.wait_for_timeout(3000)
                return True
            except Exception:
                pass
    return False


def dismiss_modals(page, max_n=6, where=""):
    """Закрывает всплывающие модальные окна, логируя их текст в modals.log."""
    seen = []
    for _ in range(max_n):
        modals = page.locator(".modal.in, [role=dialog]")
        if modals.count() == 0:
            break
        try:
            txt = re.sub(r"\s+", " ", modals.first.inner_text())[:200]
        except Exception:
            txt = "?"
        seen.append(txt)
        closed = False
        for sel in (".modal.in button:has-text('Да')", "[role=dialog] button:has-text('Да')",
                    ".modal.in button.close", "[role=dialog] button.close",
                    ".modal.in button:has-text('Продолжить')",
                    ".modal.in button:has-text('Понятно')",
                    ".modal.in button:has-text('Ок')",
                    ".modal.in button:has-text('OK')",
                    ".modal.in button:has-text('Закрыть')",
                    ".modal.in button:has-text('Отмена')"):
            loc = page.locator(sel)
            if loc.count():
                try:
                    loc.first.click(timeout=3000)
                    page.wait_for_timeout(900)
                    closed = True
                    break
                except Exception:
                    pass
        if not closed:
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(800)
            except Exception:
                break
    if seen:
        try:
            with open(str(_MODALS_LOG), "a", encoding="utf-8") as f:
                f.write("%s\t%s\n" % (where or "?", " || ".join(seen)))
        except Exception:
            pass
    return page.locator(".modal.in, [role=dialog]").count() == 0


def login(ctx, page, org_key=None, wait_ms=5000):
    """Полный вход: goto login → autofill → ожидание рабочего кабинета."""
    page.goto(login_url(), wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(2500)
    if not looks_logged_in(page):
        if not try_autofill(page, org_key=org_key):
            raise RuntimeError("не удалось войти (проверьте .env: ASURSO_LOGIN/ASURSO_PASSWORD)")
        try:
            page.wait_for_load_state("networkidle", timeout=40000)
        except Exception:
            pass
        page.wait_for_timeout(wait_ms)
    dismiss_modals(page, where="login")
    if not looks_logged_in(page):
        raise RuntimeError("вход не подтверждён (осталась форма логина)")
    return page


def logout(page):
    """Выход: кнопка «Выход» + подтверждение «Да» (обязательно в конце)."""
    if page.locator("input[name=password]").count():
        return False
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception:
        pass

    clicked = False
    for sel in ("[ng-click*='logout' i]", "[ng-click*='signout' i]",
                "a:has-text('Выход')", "button:has-text('Выход')",
                "[title='Выход']", "[ng-click*='exit' i]"):
        loc = page.locator(sel)
        for i in range(loc.count()):
            try:
                if loc.nth(i).is_visible():
                    loc.nth(i).click(timeout=4000)
                    clicked = True
                    break
            except Exception:
                pass
        if clicked:
            break

    if not clicked:
        for trig in (".navbar .dropdown-toggle", ".user-menu", "[ng-click*='user' i]",
                     "[ng-click*='profile' i]", ".profile-menu", "[uib-dropdown-toggle]"):
            loc = page.locator(trig)
            if loc.count():
                try:
                    loc.first.click(timeout=3000)
                    page.wait_for_timeout(1000)
                except Exception:
                    pass
        loc = page.get_by_text("Выход", exact=True)
        for i in range(loc.count()):
            try:
                if loc.nth(i).is_visible():
                    loc.nth(i).click(timeout=4000)
                    clicked = True
                    break
            except Exception:
                pass

    if not clicked:
        return False

    page.wait_for_timeout(1500)
    for sel in (".modal.in button:has-text('Да')", "[role=dialog] button:has-text('Да')",
                ".modal.in a:has-text('Да')", ".modal.in button:has-text('OK')",
                "button:has-text('Да')", "button:has-text('OK')"):
        loc = page.locator(sel)
        if loc.count():
            try:
                loc.first.click(timeout=3000)
                page.wait_for_timeout(2500)
                break
            except Exception:
                pass
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(2000)
    url = (page.url or "").lower()
    return ("logout" in url or "authorize" in url
            or page.locator("input[name=password]").count() > 0)


# --------------------------------------------------------------------------
# webapi (куки + заголовок «at»)
# --------------------------------------------------------------------------
_AT = {"val": None}


def capture_at(page):
    """Начинает перехват заголовка `at` из реальных webapi-запросов страницы."""
    def _on(req):
        try:
            if "webapi" in req.url:
                a = (req.headers or {}).get("at")
                if a and len(a) >= 16:
                    _AT["val"] = a
        except Exception:
            pass
    page.on("request", _on)


def find_at(page):
    """Токен `at`: сначала из перехвата, затем из localStorage/sessionStorage."""
    if _AT["val"]:
        return _AT["val"]
    try:
        cands = page.evaluate("""() => {
          const out = [];
          const scan = (st) => { for (let i = 0; i < st.length; i++) {
            const v = st.getItem(st.key(i));
            if (v && /^\\d{16,}$/.test(v)) out.push(v); } };
          try { scan(localStorage); } catch (e) {}
          try { scan(sessionStorage); } catch (e) {}
          return out;
        }""")
        if cands:
            _AT["val"] = cands[0]
    except Exception:
        pass
    return _AT["val"]


def api_get(page, path):
    """GET JSON через fetch на странице (куки + at). path — начиная с /webapi/..."""
    return api_call(page, "GET", path)


def api_post(page, path, payload):
    """POST JSON через fetch на странице (куки + at)."""
    return api_call(page, "POST", path, payload)


def api_call(page, method, path, payload=None):
    url = path if str(path).startswith("http") else (site_url() + path)
    return page.evaluate(
        """async ([method, u, at, body]) => {
          try {
            const headers = {'accept': 'application/json, text/plain, */*'};
            if (at) headers['at'] = at;
            const opt = {method: method, credentials: 'include', headers: headers};
            if (body !== null && body !== undefined) {
              headers['content-type'] = 'application/json;charset=UTF-8';
              opt.body = JSON.stringify(body);
            }
            const r = await fetch(u, opt);
            const ct = r.headers.get('content-type') || '';
            let data = null;
            if (ct.indexOf('json') >= 0) { try { data = await r.json(); } catch (e) { data = null; } }
            else { data = (await r.text()).slice(0, 2000); }
            return {status: r.status, body: data};
          } catch (e) { return {status: -1, body: null, err: String(e)}; }
        }""",
        [method, url, _AT.get("val"), payload],
    )


# --------------------------------------------------------------------------
# перехват сети и снятие снимков (разведка)
# --------------------------------------------------------------------------
class NetLog:
    """Копит xhr/fetch/document-запросы и (опционально) JSON-ответы GET."""

    def __init__(self, log_path, capture_bodies=False, max_bodies=400):
        self.records = []
        self.bodies = {}
        self.log_path = Path(log_path)
        self.capture_bodies = capture_bodies
        self.max_bodies = max_bodies

    def attach(self, page):
        def on_request(req):
            if req.resource_type in ("xhr", "fetch", "document"):
                rec = {"method": req.method, "type": req.resource_type, "url": req.url}
                try:
                    pd = req.post_data
                    if pd:
                        rec["post"] = pd[:4000]
                except Exception:
                    pass
                self.records.append(rec)
                try:
                    self.log_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(str(self.log_path), "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                except Exception:
                    pass

        def on_response(resp):
            if not self.capture_bodies:
                return
            try:
                if resp.request.method == "GET" and "json" in (resp.headers.get("content-type") or "") \
                        and len(self.bodies) < self.max_bodies:
                    self.bodies[resp.url] = resp.json()
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)
        return self

    def save_bodies(self, path):
        try:
            Path(path).write_text(json.dumps(self.bodies, ensure_ascii=False)[:4000000],
                                  encoding="utf-8")
            return len(self.bodies)
        except Exception:
            return 0


_DOM_JS = r"""
() => {
  const labelOf = (el) => {
    if (el.labels && el.labels.length)
      return [...el.labels].map(l => (l.innerText || '').trim()).join(' | ');
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    const p = el.closest('label');
    if (p) return (p.innerText || '').trim();
    let td = el.closest('td');
    if (td) { let prev = td.previousElementSibling; if (prev) return (prev.innerText || '').trim(); }
    return '';
  };
  const txt = (s, n) => (s || '').replace(/\s+/g, ' ').trim().slice(0, n || 80);
  const sels = [...document.querySelectorAll('select')].map(el => ({
    id: el.id, name: el.name, ngmodel: el.getAttribute('ng-model'), label: labelOf(el),
    value: el.value,
    options: [...el.options].slice(0, 30).map(o => ({v: o.value, t: txt(o.text, 60)})),
    nopts: el.options.length
  }));
  const inps = [...document.querySelectorAll('input, textarea')]
    .filter(el => el.type !== 'hidden' && el.type !== 'password')
    .map(el => ({ tag: el.tagName.toLowerCase(), id: el.id, name: el.name,
                  type: el.type || '', ngmodel: el.getAttribute('ng-model'),
                  label: labelOf(el), value: txt(el.value, 40), readonly: el.readOnly }));
  const btns = [...document.querySelectorAll(
      'button, input[type=submit], input[type=button], a[onclick], a[href*="javascript"]')]
    .map(el => ({ tag: el.tagName, id: el.id, name: el.name,
                  ngclick: el.getAttribute('ng-click'),
                  text: txt(el.innerText || el.value || el.title, 70) }))
    .filter(b => b.text || b.ngclick);
  const tables = [...document.querySelectorAll('table')].slice(0, 20).map(t => ({
    id: t.id, cls: txt(t.className, 60),
    headers: [...t.querySelectorAll('th')].slice(0, 30).map(th => txt(th.innerText, 50)),
    rows: t.rows.length
  }));
  const menus = [...document.querySelectorAll('.dropdown-menu a, .dropdown-menu li a, .nav a, a')]
    .map(a => txt(a.innerText, 60)).filter(Boolean);
  const heads = [...document.querySelectorAll('h1,h2,h3,.page-title,.title')]
    .map(h => txt(h.innerText, 80)).filter(Boolean).slice(0, 12);
  return { url: location.href, title: document.title,
           selects: sels, inputs: inps, buttons: btns, tables: tables,
           menus: [...new Set(menus)].slice(0, 120), heads: heads };
}
"""


def dump_screen(page, disc_dir, tag, net=None):
    """Сохраняет DOM (JSON), HTML, скриншот; при net — дамп тел GET-ответов."""
    disc_dir = Path(disc_dir)
    disc_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    summary = {}
    try:
        summary = page.evaluate(_DOM_JS)
        (disc_dir / ("dom_%s_%s.json" % (tag, stamp))).write_text(
            json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        (disc_dir / ("page_%s_%s.html" % (tag, stamp))).write_text(
            page.content(), encoding="utf-8")
        page.screenshot(path=str(disc_dir / ("shot_%s_%s.png" % (tag, stamp))), full_page=True)
    except Exception as exc:
        print("dump failed: %r" % exc)
        return summary
    if net is not None:
        n = net.save_bodies(disc_dir / ("api_%s_%s.json" % (tag, stamp)))
        if n:
            print("API-ответов сохранено: %d -> api_%s_%s.json" % (n, tag, stamp))
    print("DOM: %d select, %d input, %d button, %d table; %s" % (
        len(summary.get("selects", [])), len(summary.get("inputs", [])),
        len(summary.get("buttons", [])), len(summary.get("tables", [])),
        summary.get("url", "")))
    for h in summary.get("heads", []):
        print("  заголовок: %s" % h)
    for s in summary.get("selects", []):
        print("  select name=%s ng=%s: n=%d first=%s" % (
            s.get("name") or s.get("id"), s.get("ngmodel"),
            s.get("nopts"), [o["t"] for o in s.get("options", [])[:5]]))
    return summary
