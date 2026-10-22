# -*- coding: utf-8 -*-
"""
Чтение журнала преподавателя из БД проекта j-web (SQLite).

Источник: C:\\websites\\na\\j-web\\db\\app.db (или путь из env JWEB_DB / опций).
Схема j-web: classes, subjects (привязан к классу), students, student_classes,
lessons (subject_id+class_id+date), marks (student_id+lesson_id+value+work_type).

Модуль возвращает «рабочую модель» — что и куда надо загрузить в АСУ РСО:
    {
      "classes": [
        {"class_id": 1, "name": "9 А", "norm": "9А",
         "subjects": [
            {"subject_id": 3, "name": "Информатика", "norm_name": "ИНФОРМАТИКА",
             "lessons": [
                {"lesson_id": 10, "date": "2026-09-01", "topic": "...",
                 "marks": [
                    {"student_id": 5, "fio": "Иванов Иван Иванович",
                     "norm_fio": "ИВАНОВИИ", "value": 5,
                     "work_type": "lesson", "comment": ""}
                 ]}
             ]}
         ]}
      ]
    }
"""

import os
import re
import json
import sqlite3
from pathlib import Path

# Значения работы в j-web → человекочитаемый тип (см. j-web/src/helpers.php)
WORK_TYPE_LABELS = {
    "lesson": "Урок",
    "control": "Контроль",
    "homework": "Домашнее",
    "answer": "Ответ",
    "ДЗ": "ДЗ",
    "СР": "СР",
}

DEFAULT_DB = Path(r"C:\websites\na\j-web\db\app.db")


def db_path(override=None):
    if override:
        return Path(override)
    env = os.environ.get("JWEB_DB")
    if env:
        return Path(env)
    return DEFAULT_DB


# --------------------------------------------------------------------------
# нормализация
# --------------------------------------------------------------------------
def norm_class(raw):
    """«9 А» / «9а» / «9А.И1» → «9А» (верхний регистр, без пробелов/точек/дефисов)."""
    s = str(raw or "").upper()
    s = re.sub(r"[\s.\-]", "", s)
    s = re.sub(r"И(\d)$", r"\1", s)  # индекс подгруппы «…И1» → «…1»
    return s


def norm_name(raw):
    """ФИО → ключ сопоставления: без регистра, Ё→Е, лишних символов."""
    s = str(raw or "").upper().replace("Ё", "Е").replace("Э", "Е")
    s = re.sub(r"[\s.\-]", "", s)
    return s


def fio(last, first, middle):
    parts = [str(x).strip() for x in (last, first, middle) if x and str(x).strip()]
    return " ".join(parts)


def teacher_key(name):
    """Фамилия + инициалы: 'Молчанова М. В.' → ('МОЛЧАНОВА', 'МВ')."""
    toks = [t for t in re.split(r"[\s.\-]+", str(name or "").strip()) if t]
    if not toks:
        return "", ""
    return norm_name(toks[0]), "".join(norm_name(t)[:1] for t in toks[1:3])


def match_fio(student_name, asurso_name):
    """Сопоставление ФИО ученика: полное совпадение нормы или фамилия+инициалы."""
    a = norm_name(student_name)
    b = norm_name(asurso_name)
    if a == b:
        return True
    if not a or not b:
        return False
    sur_a, ini_a = teacher_key(student_name)
    sur_b, ini_b = teacher_key(asurso_name)
    if sur_a and sur_a == sur_b:
        if ini_a and ini_b and ini_a[0] == ini_b[0]:
            return True
        # без отчества у одной из сторон — считаем совпадением по фамилии
        if not ini_a or not ini_b:
            return True
    return False


# --------------------------------------------------------------------------
# загрузка
# --------------------------------------------------------------------------
def _connect(path):
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    return con


def _table_exists(con, name):
    r = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return r is not None


def load_model(path=None, include_retakes=False):
    """Собирает модель (классы→предметы→уроки→оценки) из БД j-web.

    include_retakes=False — берём только итоговые попытки (is_current=1),
    как в средних j-web. Переписывания (is_retake=1) по умолчанию пропускаем:
    в АСУ РСО выгружаем итоговую оценку клетки.
    """
    path = db_path(path)
    if not Path(path).exists():
        raise FileNotFoundError("БД j-web не найдена: %s" % path)
    con = _connect(path)
    try:
        # 1) уроки (класс → предмет → урок): тема и ДЗ
        classes = {}
        lessons = {}
        for r in con.execute("""
            SELECT l.id AS lesson_id, l.date AS lesson_date, l.topic,
                   sub.id AS subject_id, sub.name AS subject_name,
                   c.id AS class_id, c.name AS class_name
            FROM lessons l
            JOIN subjects sub ON sub.id = l.subject_id
            JOIN classes  c  ON c.id = l.class_id
            ORDER BY c.name, sub.name, l.date, l.id
        """):
            c = classes.setdefault(r["class_id"], {
                "class_id": r["class_id"], "name": r["class_name"],
                "norm": norm_class(r["class_name"]), "subjects": {},
            })
            s = c["subjects"].setdefault(r["subject_id"], {
                "subject_id": r["subject_id"], "name": r["subject_name"],
                "norm_name": norm_name(r["subject_name"]), "lessons": {},
            })
            entry = {"lesson_id": r["lesson_id"], "date": r["lesson_date"],
                     "topic": r["topic"] or "", "homework": "", "marks": []}
            s["lessons"][r["lesson_id"]] = entry
            lessons[r["lesson_id"]] = entry

        # 2) домашние задания (склеиваем заголовок и описание)
        for r in con.execute("SELECT lesson_id, title, description FROM homeworks ORDER BY lesson_id, id"):
            entry = lessons.get(r["lesson_id"])
            if entry is None:
                continue
            parts = [x for x in [(r["title"] or "").strip(), (r["description"] or "").strip()] if x]
            text = " — ".join(parts).strip()
            if text:
                entry["homework"] = text if not entry["homework"] else (entry["homework"] + "; " + text)

        # 3) оценки (только итоговые попытки, если не запрошена история)
        where_cur = "" if include_retakes else " AND m.is_current=1"
        for r in con.execute("""
            SELECT m.id AS mark_id, m.value, m.work_type, m.comment, m.is_retake, m.is_current,
                   m.lesson_id, st.id AS student_id, st.last_name, st.first_name,
                   st.middle_name, st.external_id
            FROM marks m
            JOIN students st ON st.id = m.student_id
            WHERE 1=1 %s
            ORDER BY m.lesson_id, st.last_name, st.first_name
        """ % where_cur):
            entry = lessons.get(r["lesson_id"])
            if entry is None:
                continue
            nm = fio(r["last_name"], r["first_name"], r["middle_name"])
            entry["marks"].append({
                "mark_id": r["mark_id"],
                "student_id": r["student_id"],
                "fio": nm,
                "norm_fio": norm_name(nm),
                "external_id": r["external_id"],
                "value": r["value"],
                "work_type": (r["work_type"] or "lesson"),
                "work_type_label": WORK_TYPE_LABELS.get(r["work_type"] or "lesson",
                                                        r["work_type"] or "Урок"),
                "comment": r["comment"] or "",
                "is_retake": r["is_retake"],
            })

        # dict → list, чистим контейнеры
        out = {"classes": [], "source_db": str(path)}
        for c in classes.values():
            subs = []
            for s in c["subjects"].values():
                s["lessons"] = sorted(s["lessons"].values(), key=lambda x: (x["date"], x["lesson_id"]))
                subs.append(s)
            c["subjects"] = sorted(subs, key=lambda x: x["name"])
            out["classes"].append(c)
        out["classes"].sort(key=lambda x: x["name"])
        return out
    finally:
        con.close()


def load_students(path=None):
    """Справочник учеников j-web: {class_norm: [ {fio, norm_fio, external_id} ]}."""
    path = db_path(path)
    con = _connect(path)
    try:
        rows = con.execute("""
            SELECT st.id, st.last_name, st.first_name, st.middle_name, st.external_id,
                   c.name AS class_name
            FROM students st
            LEFT JOIN student_classes sc ON sc.student_id = st.id
            LEFT JOIN classes c ON c.id = sc.class_id
            ORDER BY c.name, st.last_name, st.first_name
        """).fetchall()
        out = {}
        for r in rows:
            nm = fio(r["last_name"], r["first_name"], r["middle_name"])
            out.setdefault(norm_class(r["class_name"]), []).append({
                "id": r["id"],
                "fio": nm,
                "norm_fio": norm_name(nm),
                "external_id": r["external_id"],
            })
        return out
    finally:
        con.close()


def normalize_snapshot(data):
    """Нормализует JSON HTTP-экспорта j-web в ту же модель, что load_model()."""
    out = {"classes": [], "source": data.get("source") or "snapshot",
           "generated": data.get("generated")}
    for c in data.get("classes", []):
        cname = c.get("name") or ""
        cc = {"class_id": c.get("class_id"), "name": cname,
              "norm": norm_class(cname), "subjects": []}
        for s in c.get("subjects", []):
            sname = s.get("name") or ""
            ss = {"subject_id": s.get("subject_id"), "name": sname,
                  "norm_name": norm_name(sname), "lessons": []}
            for l in s.get("lessons", []):
                ll = {"lesson_id": l.get("lesson_id"), "date": l.get("date"),
                      "topic": l.get("topic") or "", "homework": l.get("homework") or "",
                      "marks": []}
                for m in l.get("marks", []):
                    nm = m.get("fio") or fio(m.get("last_name"), m.get("first_name"),
                                             m.get("middle_name"))
                    wt = m.get("work_type") or "lesson"
                    ll["marks"].append({
                        "mark_id": m.get("mark_id"),
                        "student_id": m.get("student_id"),
                        "fio": nm,
                        "norm_fio": norm_name(nm),
                        "external_id": m.get("external_id"),
                        "value": m.get("value"),
                        "work_type": wt,
                        "work_type_label": WORK_TYPE_LABELS.get(wt, wt),
                        "comment": m.get("comment") or "",
                        "is_retake": m.get("is_retake", 0),
                    })
                ss["lessons"].append(ll)
            ss["lessons"].sort(key=lambda x: (str(x.get("date") or ""), x.get("lesson_id") or 0))
            cc["subjects"].append(ss)
        cc["subjects"].sort(key=lambda x: x["name"])
        out["classes"].append(cc)
    out["classes"].sort(key=lambda x: x["name"])
    return out


def load_snapshot(path=None, data=None):
    """Читает JSON-экспорт j-web (файл или уже разобранный dict) → модель."""
    if data is None:
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        src = str(path)
    else:
        src = "http"
    out = normalize_snapshot(data)
    out["source_db"] = src
    return out


def summarize(model):
    """Краткая сводка модели для консоли."""
    cells = 0
    lessons = 0
    for c in model["classes"]:
        for s in c["subjects"]:
            lessons += len(s["lessons"])
            for l in s["lessons"]:
                cells += len(l["marks"])
    return {
        "classes": len(model["classes"]),
        "subjects": sum(len(c["subjects"]) for c in model["classes"]),
        "lessons": lessons,
        "marks": cells,
    }
