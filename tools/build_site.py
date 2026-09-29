#!/usr/bin/env python3
"""Сборка статического сайта «ЖК глазами жильцов» из data/site.db в out/.

    build_site.py               # собрать в out/ (через out.new и подмену папки)
    build_site.py --freeze      # то же и закрепить адреса страниц навсегда (перед выкладкой)

Страница ЖК публикуется только с адресом до улицы: адрес обязателен (решение Давида).
И только если за 30 дней в чатах набралось хотя бы MIN_TOPIC_MSGS сообщений о жизни дома
без рекламы: чат приёмщика, барахолка или чат под спамом вакансий дал бы странице
«0 жалоб», и покупатель принял бы это за идеальный ЖК.
Застройщик — из ЕРЗ, если город совпал, иначе из нашей базы, иначе пометка.
УК — по упоминаниям жильцов, иначе «УК не указана в реестрах».

Диаграммы — обычный HTML, а не SVG: на телефоне строки складываются в два
этажа (подпись сверху, полоса снизу), а не уезжают вправо мелким шрифтом.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = "/opt/zhk-site"
DB = f"{ROOT}/data/site.db"
OUT = f"{ROOT}/out"
CONFIG = f"{ROOT}/data/config.json"
REDIRECTS = f"{ROOT}/data/redirects.caddy"      # import в блоке tilibom.automatiko.ru
sys.path.insert(0, f"{ROOT}/tools")
from zhk_data import RANK_MIN_MSGS, TOPICS, clean_address, nice_name, slugify, street_level  # noqa: E402

LABEL = {k: v for k, v, _ in TOPICS}
MIN_TOPIC_MSGS = 10
NBSP = " "
DEFAULTS = {"brand": "ЖК глазами жильцов", "brand_html": "ЖК <span>глазами жильцов</span>",
            "base_url": "https://zhk.automatiko.ru", "bot_username": "", "metrika_id": ""}
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]
MSG = ("сообщение", "сообщения", "сообщений")
NEG = ("жалоба", "жалобы", "жалоб")
POS = ("похвала", "похвалы", "похвал")
GOLD_SAMPLE = f"{ROOT}/data/gold/sample.jsonl"
GOLD_LABELS = f"{ROOT}/data/gold/labels.json"
TONE_EVAL = f"{ROOT}/data/tone/eval.json"


def plural(n: int, forms: tuple[str, str, str]) -> str:
    n = abs(int(n)) % 100
    if 11 <= n <= 19:
        return forms[2]
    return forms[0] if n % 10 == 1 else forms[1] if 2 <= n % 10 <= 4 else forms[2]


def fmt(n: int) -> str:
    return f"{int(n):,}".replace(",", NBSP)


def share(per_1000: float) -> str:
    """Доля сообщений с темой: 358.6 на тысячу → «36%», 4.5 → «0,5%»."""
    p = (per_1000 or 0) / 10
    return f"{round(p)}%" if p >= 10 else f"{p:.1f}%".replace(".", ",")


def config() -> dict:
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG):
        cfg.update(json.load(open(CONFIG, encoding="utf-8")))
    return cfg


def esc(s) -> str:
    return html.escape(str(s), quote=True)


# ── ДИАГРАММЫ ────────────────────────────────────────────────────────────
def tone_line(t: dict) -> str:
    return (f'{fmt(t["n_neg"])} {plural(t["n_neg"], NEG)}, {fmt(t["n_pos"])} {plural(t["n_pos"], POS)}, '
            f'{fmt(t["n_neu"])} нейтральных')


def bars_html(topics: list[dict]) -> str:
    """Полоса на тему: длина — сколько раз тему упоминали (без рекламы), внутри —
    жалобы, нейтральные и похвала. Число справа — жалобы: для покупателя это главное.
    Цвета — расходящаяся пара с серой серединой, проверена на цветовую слепоту."""
    mx = max((t["n_30d"] for t in topics), default=0) or 1
    rows = []
    for t in topics:
        w = 100 * t["n_30d"] / mx
        segs = "".join(f'<span class="seg {cls}" style="flex-grow:{v}"></span>'
                       for cls, v in (("neg", t["n_neg"]), ("neu", t["n_neu"]), ("pos", t["n_pos"])) if v)
        tip = f'{t["label"]}: {tone_line(t)} — за 30 дней, реклама не считается'
        rows.append(f'<div class="brow" title="{esc(tip)}"><span class="blabel">{esc(t["label"])}</span>'
                    f'<span class="btrack"><span class="bstack" style="width:{w:.1f}%">{segs}</span></span>'
                    f'<span class="bval">{fmt(t["n_neg"])}</span></div>')
    legend = ('<div class="legend"><span><i class="sw neg"></i>жалобы</span>'
              '<span><i class="sw neu"></i>нейтрально</span><span><i class="sw pos"></i>похвала</span></div>')
    return (f'{legend}<div class="bars" role="img" aria-label="Жалобы, нейтральные сообщения и похвала '
            f'по темам за 30 дней">{"".join(rows)}</div>')


def dots_html(ranked: list[dict]) -> str:
    rows = []
    for t in ranked:
        tip = (f'{t["label"]}: {t["city_rank"]}-е место из {t["city_total"]} — жалуются чаще, '
               f'чем в {t["pct"]}% ЖК города')
        rows.append(f'<div class="drow" title="{esc(tip)}"><span class="dlabel">{esc(t["label"])}</span>'
                    f'<span class="dtrack"><span class="ddot" style="left:{t["pct"]}%"></span></span>'
                    f'<span class="dval">чаще {t["pct"]}%</span></div>')
    rows.append('<div class="drow daxis"><span class="dlabel"></span><span class="dends">'
                '<span>← жалуются реже всех</span><span>чаще всех →</span></span><span class="dval"></span></div>')
    return (f'<div class="dots" role="img" aria-label="Место ЖК среди комплексов города по жалобам">'
            f'{"".join(rows)}</div>')


def tone_quality(con: sqlite3.Connection) -> dict:
    """Честная цифра для «Как мы считаем»: насколько метки, по которым собран сайт,
    совпадают с ручной разметкой 5 000 сообщений. Считается заново при каждой сборке."""
    from tone_model import CUT       # только отложенные дни: на них локальная модель не училась
    try:
        gold = {}
        for line in open(GOLD_SAMPLE, encoding="utf-8"):
            x = json.loads(line)
            if x["posted_at"] >= CUT:
                gold[x["id"]] = x["n"]
        lab = {int(k): v for k, v in json.load(open(GOLD_LABELS, encoding="utf-8")).items()}
    except OSError:
        return {}
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='tone'").fetchone():
        return {}
    ids = ",".join(map(str, gold))
    rows = con.execute(f"SELECT message_id, label FROM tone WHERE message_id IN ({ids})").fetchall()
    if not rows:
        return {}
    ok = sum(1 for mid, l in rows if lab.get(gold[mid]) == l)
    src = {r[0]: r[1] for r in con.execute("SELECT src, COUNT(*) FROM tone GROUP BY src")}
    n = len(rows)
    q = {"agree": round(100 * ok / n), "n": n, "n_fmt": fmt(n), "src": src,
         "n_word": "сообщении" if n % 10 == 1 and n % 100 != 11 else "сообщениях",
         "model": max(src, key=src.get) if src else ""}
    if os.path.exists(TONE_EVAL):
        ev = json.load(open(TONE_EVAL, encoding="utf-8"))
        q["model_acc"], q["model_n"] = round(100 * ev["accuracy"]), ev["n"]
    return q


# ── ДАННЫЕ ───────────────────────────────────────────────────────────────
def load(con: sqlite3.Connection) -> list[dict]:
    zs = [dict(r) for r in con.execute(
        "SELECT z.*, e.name erz_name, e.how erz_how, e.brand, e.brand_place, e.developers,"
        " e.date_end, e.slug erz_slug, g.address geo_address, g.road geo_road,"
        " u.name uk, p.city p_city, p.city_slug p_city_slug, p.lat p_lat, p.lon p_lon,"
        " p.how p_how, p.hide p_hide, p.address p_address FROM zhk z"
        " LEFT JOIN erz e ON e.zhk_id = z.id LEFT JOIN geo g ON g.zhk_id = z.id"
        " LEFT JOIN uk u ON u.zhk_id = z.id LEFT JOIN place p ON p.zhk_id = z.id WHERE z.active = 1")]
    tp = defaultdict(dict)
    for r in con.execute("SELECT * FROM topic"):
        tp[r["zhk_id"]][r["topic"]] = dict(r)
    for z in zs:
        z["tp"] = tp.get(z["id"], {})
        # Город и координаты — после сверки с регионом (zhk_data.py place). Без сверки
        # или со снятием (вне России, ложные координаты) страница не публикуется.
        z["place_ok"] = bool(z["p_city"]) and not z["p_hide"]
        if z["place_ok"]:
            if z["p_city"] != z["city"]:
                z["district"] = z["metro"] = None      # район и метро были от чужого города
            z["city"], z["city_slug"] = z["p_city"], z["p_city_slug"]
            if z["p_how"] == "поиск":                    # координаты заменены найденными по имени
                z["lat"], z["lon"] = z["p_lat"], z["p_lon"]
                z["address_base"] = z["geo_address"] = z["geo_road"] = None
        erz_ok = bool(z["erz_slug"]) and not (z["erz_how"] or "").startswith("другой город")
        if not erz_ok:
            z["brand"] = z["brand_place"] = z["developers"] = z["date_end"] = None
        nm = (z["erz_name"] or "").strip() if erz_ok else ""
        nm = nice_name(nm[3:] if nm.lower().startswith("жк ") else nm) if nm else ""
        z["name"] = nm or nice_name(z["name_raw"] or z["name"], z["city"])
        if z["p_address"]:
            addr = clean_address(z["p_address"])
        elif street_level(z["address_base"]):
            addr = clean_address(z["address_base"])
        elif z["geo_road"]:
            addr = clean_address(z["geo_address"])
        else:
            addr = None
        # Улица должна быть в самом показанном адресе: геокодер иногда кладёт в «дорогу»
        # код трассы («46К-9530») или название ЖК — такой адрес покупателю ничего не даёт.
        z["address"] = addr if addr and street_level(addr) else None
    # Склейка иногда берёт основным ЖК без адреса («Галактика» в Краснодаре), а адрес есть
    # у дубля — это тот же комплекс, берём его адрес, чтобы страница не пропала.
    if not all(z["address"] for z in zs):
        dup_addr = {}
        for r in con.execute("SELECT z.canon_id, z.address_base, g.address, g.road FROM zhk z"
                             " LEFT JOIN geo g ON g.zhk_id = z.id WHERE z.canon_id IS NOT NULL"):
            a = clean_address(r[1]) if street_level(r[1]) else (clean_address(r[2]) if r[3] else None)
            if a and street_level(a):
                dup_addr.setdefault(r[0], a)
        for z in zs:
            if not z["address"] and z["p_how"] != "поиск" and z["id"] in dup_addr:
                z["address"] = dup_addr[z["id"]]
    return zs


def enrich(zs: list[dict], urls: dict) -> None:
    by_brand, by_city = defaultdict(list), defaultdict(list)
    for z in zs:
        z["url"] = urls[z["id"]]
        if z["brand"]:
            by_brand[z["brand"]].append(z)
        by_city[z["city"]].append(z)
    zero = {"n_30d": 0, "n_7d": 0, "n_prev7d": 0, "per_1000": 0, "n_neg": 0, "n_pos": 0, "n_neu": 0,
            "n_unk": 0, "n_ad": 0, "neg_7d": 0, "neg_prev7d": 0, "neg_per_1000": 0}
    for z in zs:
        topics = []
        for k, _, _ in TOPICS:
            t = dict(zero)
            t.update({f: v for f, v in z["tp"].get(k, {}).items() if v is not None})
            t.update(key=k, label=LABEL[k])
            # Без оценки (сообщение ещё не размечено) показываем вместе с нейтральными.
            t["n_neu"] = t["n_neu"] + t["n_unk"]
            t["share"] = share(t["per_1000"])
            t["neg_share"] = f'{round(100 * t["n_neg"] / t["n_30d"])}%' if t["n_30d"] else "—"
            topics.append(t)
        topics.sort(key=lambda t: (-t["n_neg"], -t["n_30d"]))
        z["topics"] = topics
        z["bars"] = bars_html(topics)
        ranked = []
        for t in topics:
            if t.get("city_rank") and (t.get("city_total") or 0) > 1:
                t["pct"] = round(100 * (t["city_total"] - t["city_rank"]) / (t["city_total"] - 1))
                ranked.append(t)
        z["ranked"] = ranked
        z["city_total"] = ranked[0]["city_total"] if ranked else 0
        z["dots"] = dots_html(ranked) if ranked else ""
        # Покупателю важны три вещи: на что здесь жалуются больше всего, за что хвалят
        # и чем ЖК выделяется среди соседей по жалобам. Главная по объёму тема часто
        # ничем не выделяется — поэтому место в городе идёт отдельной фразой.
        negs = [t for t in topics if t["n_neg"] > 0]
        poss = sorted([t for t in topics if t["n_pos"] > 0], key=lambda t: -t["n_pos"])
        z["neg_list"] = [{"label": t["label"], "n": fmt(t["n_neg"]), "word": plural(t["n_neg"], NEG)}
                         for t in negs[:3]]
        z["pos_list"] = [{"label": t["label"], "n": fmt(t["n_pos"]), "word": plural(t["n_pos"], POS)}
                         for t in poss[:3]]
        parts = []
        if negs:
            t0 = negs[0]
            parts.append(f'Больше всего жалоб в теме «{esc(t0["label"])}»: {fmt(t0["n_neg"])} за 30 дней, '
                         f'это {t0["neg_share"]} всех сообщений о ней.')
        # Похвалу выносим в первый абзац, только когда она заметна: «хвалят УК — 2 сообщения»
        # рядом с 95 жалобами на ту же УК звучит как издёвка. Полный список — в блоке ниже.
        praised = [t for t in poss if t["n_pos"] >= 3 and t["n_pos"] >= 0.25 * t["n_neg"]]
        if praised:
            parts.append(f'Чаще всего хвалят «{esc(praised[0]["label"])}»: {fmt(praised[0]["n_pos"])} '
                         f'{plural(praised[0]["n_pos"], MSG)}.')
        loud = max(ranked, key=lambda t: t["pct"]) if ranked else None
        if loud and loud["pct"] >= 75 and loud["n_neg"] > 0:
            parts.append(f'На фоне города выделяется «{esc(loud["label"])}»: жалуются чаще, '
                         f'чем в {loud["pct"]}% ЖК.')
        z["insight"] = " ".join(parts)
        trend = []
        for t in topics:
            if t["neg_7d"] >= 5 and t["neg_7d"] >= 1.5 * max(t["neg_prev7d"], 1):
                trend.append(f'Чаще обычного жаловались на тему «{esc(t["label"])}»: {t["neg_7d"]} '
                             f'{plural(t["neg_7d"], NEG)} за неделю, на прошлой неделе — {t["neg_prev7d"]}.')
        z["trend"] = trend[:3]
        z["msgs_line"] = f'{fmt(z["msgs_30d"])} {plural(z["msgs_30d"], MSG)}'
        z["chats_word"] = plural(z["chats"], ("чат", "чата", "чатов"))
        z["same_dev"] = [{"name": o["name"], "url": o["url"]} for o in
                         sorted(by_brand.get(z["brand"], []), key=lambda o: -o["msgs_30d"])
                         if o["id"] != z["id"]][:8] if z["brand"] else []
        z["nearby"] = [{"name": o["name"], "url": o["url"]} for o in
                       sorted(by_city[z["city"]], key=lambda o: -o["msgs_30d"]) if o["id"] != z["id"]][:8]


def page_meta(z: dict) -> tuple[str, str, str]:
    negs = [t for t in z["topics"] if t["n_neg"] > 0][:3]
    title = f'ЖК {z["name"]}, {z["city"]}: жалобы и отзывы жильцов'
    if negs:
        bits = ", ".join(f'«{t["label"]}» — {t["n_neg"]}' for t in negs)
        desc = f'На что жалуются жильцы ЖК {z["name"]} за 30 дней: {bits}.'
    else:
        desc = f'Что пишут жильцы ЖК {z["name"]} в чатах за 30 дней: жалобы и похвала по темам.'
    if z["brand"]:
        desc += f' Застройщик {z["brand"]}.'
    if z["uk"]:
        desc += f' УК {z["uk"]}.'
    ld = {"@context": "https://schema.org", "@type": "Place", "name": f'ЖК {z["name"]}',
          "address": {"@type": "PostalAddress", "streetAddress": z["address"],
                      "addressLocality": z["city"], "addressCountry": "RU"}}
    if z["lat"] and z["lon"]:
        ld["geo"] = {"@type": "GeoCoordinates", "latitude": z["lat"], "longitude": z["lon"]}
    return title, desc[:300], json.dumps(ld, ensure_ascii=False)


def assign_urls(con: sqlite3.Connection, zs: list[dict], freeze: bool) -> dict:
    """Адрес страницы: /город/имя/. Закреплённый однажды адрес не меняется,
    даже если имя ЖК потом уточнится по ЕРЗ, — иначе поиск терял бы страницы.
    Исключение — сменился город (исправили привязку): страница получает адрес
    в новом городе, закрепляется сразу, а старый адрес уходит в таблицу moved —
    с него Caddy отвечает 301 (redirects.caddy)."""
    con.execute("CREATE TABLE IF NOT EXISTS url (zhk_id INTEGER PRIMARY KEY, path TEXT UNIQUE,"
                " frozen_at TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS moved (old_path TEXT PRIMARY KEY, new_path TEXT,"
                " zhk_id INTEGER, moved_at TEXT)")
    fixed = {r[0]: r[1] for r in con.execute("SELECT zhk_id, path FROM url")}
    # Склейка сделала закреплённый ЖК дублем, а у основного своего адреса нет —
    # основной наследует адрес дубля: страница остаётся там же, где её знает поиск.
    canon = dict(con.execute("SELECT id, canon_id FROM zhk WHERE canon_id IS NOT NULL"))
    by_id = {z["id"]: z for z in zs}
    for zid, old in sorted(fixed.items()):
        main = by_id.get(canon.get(zid))
        if zid not in by_id and main and main["id"] not in fixed and old.split("/")[1] == main["city_slug"]:
            fixed[main["id"]] = fixed.pop(zid)
            con.execute("UPDATE url SET zhk_id = ? WHERE zhk_id = ?", (main["id"], zid))
    for z in zs:
        old = fixed.get(z["id"])
        if old and old.split("/")[1] != z["city_slug"]:
            del fixed[z["id"]]
            con.execute("DELETE FROM url WHERE zhk_id = ?", (z["id"],))
            z["moved_from"] = old
    taken = set(fixed.values())
    urls = {}
    for z in sorted(zs, key=lambda z: -z["msgs_30d"]):
        if z["id"] in fixed:
            urls[z["id"]] = fixed[z["id"]]
            continue
        base = f'/{z["city_slug"]}/{slugify(z["name"])}/'
        path, k = base, 2
        while path in taken:
            path, k = f'{base[:-1]}-{k}/', k + 1
        taken.add(path)
        urls[z["id"]] = path
        if freeze or z.get("moved_from"):
            con.execute("INSERT INTO url VALUES (?,?,datetime('now'))", (z["id"], path))
        if z.get("moved_from"):
            # Цепочки не копим: всё, что вело на старый адрес, теперь ведёт на новый.
            con.execute("UPDATE moved SET new_path = ? WHERE new_path = ?", (path, z["moved_from"]))
            con.execute("INSERT OR REPLACE INTO moved VALUES (?,?,?,datetime('now'))",
                        (z["moved_from"], path, z["id"]))
    # Закреплённая страница ЖК, который склейка сделала дублем: адрес ведёт на основной.
    for zid, old in list(fixed.items()):
        if zid not in urls and canon.get(zid) in urls:
            con.execute("DELETE FROM url WHERE zhk_id = ?", (zid,))
            con.execute("INSERT OR IGNORE INTO url VALUES (?,?,datetime('now'))",   # цель 301 — закреплена
                        (canon[zid], urls[canon[zid]]))
            con.execute("UPDATE moved SET new_path = ? WHERE new_path = ?", (urls[canon[zid]], old))
            con.execute("INSERT OR REPLACE INTO moved VALUES (?,?,?,datetime('now'))",
                        (old, urls[canon[zid]], zid))
    # Адрес, который снова стал адресом страницы, переадресовывать нельзя.
    con.execute("DELETE FROM moved WHERE old_path IN (SELECT path FROM url)")
    con.commit()
    return urls


def write_redirects(con: sqlite3.Connection, city_slugs: set[str]) -> bool:
    """redirects.caddy для блока tilibom в Caddyfile: 301 со старых адресов страниц ЖК
    и со страниц городов, которых больше нет, если все их ЖК уехали в один город.
    Возвращает True, если файл изменился (тогда Caddy нужно перечитать настройки)."""
    rows = con.execute("SELECT old_path, new_path FROM moved ORDER BY old_path").fetchall()
    lines = [f"redir {o} {n} 301" for o, n in rows]
    by_city = defaultdict(set)
    for o, n in rows:
        by_city[o.split("/")[1]].add(n.split("/")[1])
    for old_city, new in sorted(by_city.items()):
        if old_city not in city_slugs and len(new) == 1:
            lines.append(f"redir /{old_city}/ /{next(iter(new))}/ 301")
    text = "# Создаётся build_site.py — не править руками. Старые адреса → новые (исправлен город).\n" + \
        "".join(l + "\n" for l in lines)
    old = open(REDIRECTS, encoding="utf-8").read() if os.path.exists(REDIRECTS) else None
    if old == text:
        return False
    with open(REDIRECTS + ".new", "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(REDIRECTS + ".new", REDIRECTS)
    return True


def city_tops(items: list[dict]) -> list[dict]:
    big = [o for o in items if o["msgs_30d"] >= RANK_MIN_MSGS]
    if len(big) < 5:
        return []
    tops = []
    for key in ("voda", "uk", "shum", "lift", "parking", "zastr"):
        best = sorted(big, key=lambda o: -(o["tp"].get(key, {}).get("neg_per_1000") or 0))[:5]
        tops.append({"key": key, "label": LABEL[key], "entries": [
            {"name": o["name"], "url": o["url"],
             "share": share(o["tp"].get(key, {}).get("neg_per_1000") or 0)} for o in best]})
    return tops


# Сортировка ЖК по жалобам: «все темы» и 12 тем. Доли — жалобы на тысячу сообщений чата.
SORT_KEYS = ["all"] + [k for k, _, _ in TOPICS]
SORT_LABEL = {"all": "Жалобы по всем темам", **LABEL}
RATING_TOP = 10


def neg_vals(o: dict) -> list[float]:
    """Доли жалоб в порядке SORT_KEYS. «Все темы» — сумма по темам: сообщение
    сразу о двух темах считается в обеих (так же, как на странице ЖК)."""
    per = [o["tp"].get(k, {}).get("neg_per_1000") or 0 for k, _, _ in TOPICS]
    return [round(sum(per), 1)] + [round(v, 1) for v in per]


def build(freeze: bool) -> dict:
    cfg = config()
    con = sqlite3.connect(DB, timeout=60)
    con.row_factory = sqlite3.Row
    zs_all = load(con)
    thin = [z for z in zs_all if sum((t.get("n_30d") or 0) for t in z["tp"].values()) < MIN_TOPIC_MSGS]
    thin_ids = {z["id"] for z in thin}
    zs = [z for z in zs_all if z["address"] and z["id"] not in thin_ids and z["place_ok"]]
    # Один город — одно имя: «Королёв» и «Королев» дают один адрес /korolev/, и страница
    # одного перезаписывала бы другую. Показываем самое частое написание (с «ё», если поровну).
    names = defaultdict(lambda: defaultdict(int))
    for z in zs:
        names[z["city_slug"]][z["city"]] += 1
    for z in zs:
        z["city"] = max(names[z["city_slug"]].items(), key=lambda kv: (kv[1], "ё" in kv[0]))[0]
    # Тёзки в одном городе, которых склейка не объединила (далеко друг от друга), —
    # разные комплексы: к имени добавляем улицу, иначе в поиске три одинаковых «Зеленый квартал».
    same = defaultdict(list)
    for z in zs:
        same[(z["city"], z["name"].lower())].append(z)
    for group in same.values():
        if len(group) < 2:
            continue
        for z in group:
            street = next((part.strip() for part in (z["address"] or "").split(",")
                           if street_level(part)), None)
            if street:
                z["name"] = f'{z["name"]} ({street})'
    urls = assign_urls(con, zs, freeze)
    enrich(zs, urls)
    now = datetime.now()
    updated = f"{now.day} {MONTHS[now.month - 1]} {now.year}"
    build_id = str(int(time.time()))
    env = Environment(loader=FileSystemLoader(f"{ROOT}/templates"),
                      autoescape=select_autoescape(["html"]))
    common = {"cfg": cfg, "updated": updated, "build_id": build_id}
    new = f"{ROOT}/out.new"
    shutil.rmtree(new, ignore_errors=True)
    os.makedirs(new)
    shutil.copytree(f"{ROOT}/static", f"{new}/static")

    def write(path: str, tpl: str, **kw):
        dst = os.path.join(new, path.strip("/"), "index.html") if path.endswith("/") \
            else os.path.join(new, path.strip("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8") as fh:
            fh.write(env.get_template(tpl).render(path=path, **common, **kw))

    for z in zs:
        title, desc, ld = page_meta(z)
        write(z["url"], "zhk.html", z=z, title=title, description=desc, jsonld=ld)

    cities = defaultdict(list)
    for z in zs:
        cities[(z["city"], z["city_slug"])].append(z)
    city_list, rating = [], []
    for (city, cslug), items in sorted(cities.items(), key=lambda kv: -len(kv[1])):
        items.sort(key=lambda o: -o["msgs_30d"])
        rows = [{"name": o["name"], "url": o["url"], "district": o["district"],
                 "top_topic": o["topics"][0]["label"] if o["topics"][0]["n_neg"] else "—",
                 "msgs_fmt": fmt(o["msgs_30d"]), "msgs": o["msgs_30d"],
                 "vals": ",".join(f"{v:g}" for v in neg_vals(o)),
                 "small": o["msgs_30d"] < RANK_MIN_MSGS} for o in items]
        tops = city_tops(items)
        n_big = sum(1 for r in rows if not r["small"])
        c = {"city": city, "slug": cslug, "n": len(items), "entries": rows, "tops": tops, "n_big": n_big,
             "sort_opts": [(k, SORT_LABEL[k]) for k in SORT_KEYS], "sort_keys": ",".join(SORT_KEYS),
             "min_msgs": RANK_MIN_MSGS}
        write(f"/{cslug}/", "city.html", c=c, title=f"ЖК: {city} — что пишут жильцы в чатах",
              description=f"{len(items)} ЖК: {city}. О чём жильцы говорят в чатах: УК, протечки, лифты, шум, парковка.",
              jsonld="")
        city_list.append({"city": city, "slug": cslug, "n": len(items), "tops": tops})
        # Виджет на главной: те же правила, что у мест в городе, — только ЖК со 100+
        # сообщениями и только города, где таких хотя бы пять.
        big = [o for o in items if o["msgs_30d"] >= RANK_MIN_MSGS]
        if len(big) >= 5:
            rating.append({"c": city, "s": cslug, "z": [[o["name"], o["url"], neg_vals(o), o["msgs_30d"]]
                                                       for o in big]})

    # Главная по умолчанию: Москва, жалобы по всем темам, сначала больше. Первый экран
    # собран на сервере — без скрипта и для поисковиков список тоже виден.
    rating.sort(key=lambda r: (r["c"] != "Москва", r["c"] != "Санкт-Петербург", -len(r["z"])))
    r0 = rating[0] if rating else None
    rating_first = [{"name": z[0], "url": z[1], "share": share(z[2][0])}
                    for z in sorted(r0["z"], key=lambda z: (-z[2][0], -z[3]))[:RATING_TOP]] if r0 else []
    with open(f"{new}/rating.json", "w", encoding="utf-8") as fh:
        json.dump({"keys": SORT_KEYS, "top": RATING_TOP, "cities": rating}, fh, ensure_ascii=False)
    chats_total = con.execute("SELECT COUNT(DISTINCT source_id) FROM chat").fetchone()[0]
    write("/", "index.html", title=f"{cfg['brand']} — что пишут жильцы о своём ЖК",
          description="На что жалуются и за что хвалят свои ЖК жильцы в чатах: УК, протечки, лифты, шум, парковка. Цифры по тысяче с лишним комплексов, без рекламы застройщика.",
          jsonld="", rating=rating, r0=r0, rating_first=rating_first,
          sort_opts=[(k, SORT_LABEL[k]) for k in SORT_KEYS], cities=city_list, total_fmt=fmt(len(zs)),
          n_cities=len(city_list), cities_word=plural(len(city_list), ("городе", "городах", "городах")))
    write("/metodika/", "metodika.html", title="Как мы считаем", jsonld="",
          description="Откуда данные и что значат цифры на страницах ЖК.", chats_fmt=fmt(chats_total),
          tq=tone_quality(con), min_topic=MIN_TOPIC_MSGS)
    shutil.copyfile(os.path.join(new, "metodika", "index.html"), os.path.join(new, "404.html"))

    with open(f"{new}/search.json", "w", encoding="utf-8") as fh:
        json.dump([{"n": z["name"], "c": z["city"], "u": z["url"], "i": z["id"]} for z in zs], fh, ensure_ascii=False)
    today = now.strftime("%Y-%m-%d")
    locs = ["/", "/metodika/"] + [f'/{c["slug"]}/' for c in city_list] + [z["url"] for z in zs]
    with open(f"{new}/sitemap.xml", "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n')
        for loc in locs:
            fh.write(f"<url><loc>{esc(cfg['base_url'] + loc)}</loc><lastmod>{today}</lastmod></url>\n")
        fh.write("</urlset>\n")
    if cfg.get("indexnow_key"):          # ключ IndexNow (Яндекс, Bing) лежит в корне сайта
        with open(f"{new}/{cfg['indexnow_key']}.txt", "w", encoding="utf-8") as fh:
            fh.write(cfg["indexnow_key"])
    with open(f"{new}/robots.txt", "w", encoding="utf-8") as fh:
        fh.write(f"User-agent: *\nAllow: /\nSitemap: {cfg['base_url']}/sitemap.xml\n")

    old = f"{ROOT}/out.old"
    shutil.rmtree(old, ignore_errors=True)
    if os.path.exists(OUT):
        os.rename(OUT, old)
    os.rename(new, OUT)
    shutil.rmtree(old, ignore_errors=True)
    # Переадресации — только для живой папки: пробная сборка (OUT подменён) Caddy не трогает.
    if OUT == f"{ROOT}/out" and write_redirects(con, {c["slug"] for c in city_list}):
        r = subprocess.run(["systemctl", "reload", "caddy"], capture_output=True, text=True)
        if r.returncode:
            raise SystemExit(f"Caddy не перечитал переадресации: {r.stderr.strip()[:300]}")
    return {"pages": len(zs), "skipped_thin": len(thin_ids),
            "skipped_no_address": sum(1 for z in zs_all if not z["address"] and z["id"] not in thin_ids),
            "cities": len(city_list), "urls": len(locs)}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--freeze", action="store_true", help="закрепить адреса страниц (перед выкладкой)")
    a = p.parse_args()
    t0 = time.time()
    r = build(a.freeze)
    print(f"собрано за {time.time() - t0:.1f} с: страниц ЖК {r['pages']}, без адреса пропущено "
          f"{r['skipped_no_address']}, мало сообщений о доме (реклама, барахолки) {r['skipped_thin']}, "
          f"городов {r['cities']}, адресов в sitemap {r['urls']}")


if __name__ == "__main__":
    main()
