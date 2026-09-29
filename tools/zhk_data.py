#!/usr/bin/env python3
"""Данные сайта «ЖК глазами жильцов»: отбор ЖК, ЕРЗ, адреса, УК, метрики.

База LeadHunter (sources.db) только читается. Всё своё лежит в data/site.db,
поэтому сайт не может ничего сломать в сборе.

    zhk_data.py chats       # какие чаты не считать чатами ЖК (украинские, барахолки, знакомства)
    zhk_data.py select      # отобрать ЖК: ≥30 чистых сообщений за 30 дней
    zhk_data.py erz         # застройщик, рейтинг, сроки из API ЕРЗ (5 с между запросами)
    zhk_data.py geocode     # адрес по координатам тем, у кого нет адреса дома
    zhk_data.py place       # город и регион по координатам; ложные координаты, ЖК вне России
    zhk_data.py uk          # УК по упоминаниям жильцов
    zhk_data.py merge       # склеить дубли одного ЖК (один и тот же ЖК в ЕРЗ)
    zhk_data.py metrics     # темы, тон, недели, места в городе
    zhk_data.py status      # покрытие полей

Чистое сообщение — без дубля (dup_of IS NULL) и без спама вербовщиков,
который начинается с невидимого символа U+FEFF.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/opt/leadhunter/tools")

SRC = "/opt/leadhunter/data/sources.db"
DB = "/opt/zhk-site/data/site.db"
ENV = "/opt/claude-bot/.env"
MIN_MSGS = 30
RANK_MIN_MSGS = 100      # с какого объёма ЖК участвует в местах по городу
SPAM = "﻿%"
# Без почты Давида: адрес для связи в заголовке запроса — только сайт.
UA = "ZhkSite/1.0 (+https://automatiko.ru)"
ERZ_API = "https://erzrf.ru/erz-rest/api/v1/gk/index/"
ERZ_DELAY = 5.0          # Crawl-delay из robots.txt ЕРЗ
NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
GEO_DELAY = 1.1          # политика Nominatim — не чаще раза в секунду
PROGRESS_EVERY = 15 * 60  # правило Давида: длинная задача не молчит дольше 15 минут

SCHEMA = """
CREATE TABLE IF NOT EXISTS zhk (
    id INTEGER PRIMARY KEY, name TEXT, name_raw TEXT, slug TEXT,
    city TEXT, city_slug TEXT, region TEXT, district TEXT, metro TEXT,
    lat REAL, lon REAL, developer_base TEXT, address_base TEXT,
    msgs_30d INTEGER, authors_30d INTEGER, chats INTEGER,
    active INTEGER DEFAULT 1, selected_at TEXT);
CREATE TABLE IF NOT EXISTS erz (
    zhk_id INTEGER PRIMARY KEY, slug TEXT, how TEXT, fetched_at TEXT,
    brand TEXT, brand_place TEXT, brand_rating TEXT, developers TEXT,
    date_start TEXT, date_end TEXT, stage TEXT, raion TEXT, locality TEXT,
    metro TEXT, site TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS geo (
    zhk_id INTEGER PRIMARY KEY, address TEXT, road TEXT, house TEXT,
    locality TEXT, fetched_at TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS uk (
    zhk_id INTEGER PRIMARY KEY, name TEXT, source TEXT, mentions INTEGER,
    updated_at TEXT);
CREATE TABLE IF NOT EXISTS topic (
    zhk_id INTEGER, topic TEXT, n_30d INTEGER, n_7d INTEGER, n_prev7d INTEGER,
    per_1000 REAL, city_rank INTEGER, city_total INTEGER,
    n_neg INTEGER, n_pos INTEGER, n_neu INTEGER, n_unk INTEGER, n_ad INTEGER,
    neg_7d INTEGER, neg_prev7d INTEGER, neg_per_1000 REAL,
    PRIMARY KEY (zhk_id, topic));
CREATE TABLE IF NOT EXISTS weekly (
    zhk_id INTEGER, week_start TEXT, n INTEGER, PRIMARY KEY (zhk_id, week_start));
CREATE TABLE IF NOT EXISTS chat (
    zhk_id INTEGER, source_id INTEGER, title TEXT, link TEXT, members INTEGER,
    PRIMARY KEY (zhk_id, source_id));
CREATE TABLE IF NOT EXISTS chat_skip (
    source_id INTEGER PRIMARY KEY, reason TEXT, title TEXT, msgs INTEGER, ua_share REAL,
    updated_at TEXT);
CREATE TABLE IF NOT EXISTS rev (
    lat REAL, lon REAL, country TEXT, state TEXT, city TEXT, raw TEXT, fetched_at TEXT,
    PRIMARY KEY (lat, lon));
CREATE TABLE IF NOT EXISTS place (
    zhk_id INTEGER PRIMARY KEY, city TEXT, city_slug TEXT, region TEXT, lat REAL, lon REAL,
    how TEXT, hide TEXT, address TEXT, note TEXT, src_lat REAL, src_lon REAL, updated_at TEXT);
"""

# Темы для покупателя. Сообщение может попасть в несколько тем сразу.
# Порядок — порядок показа на странице.
TOPICS = [
    ("uk", "УК и платежи", r"\bук\b|управляющ|квитанц|тариф|начислен|жкх"),
    ("voda", "Вода и протечки", r"протеч|затоп|течёт|течет|нет воды|без воды|отключ\w* вод|горяч\w* вод|холодн\w* вод|стояк|напор"),
    ("lift", "Лифты", r"лифт"),
    ("teplo", "Отопление", r"отоплен|батаре|радиатор|холодно в квартир"),
    ("shum", "Шум", r"шум|перфоратор|сверл|громк|орут|музык"),
    ("parking", "Парковка", r"парков|машиномест|паркинг|шлагбаум"),
    ("internet", "Интернет и ТВ", r"интернет|провайдер|роутер|wi-?fi|вай-?фай"),
    ("zastr", "Застройщик и недоделки", r"застройщик|гарантийн|дефект|недодел|трещин|приёмк|приемк"),
    ("musor", "Мусор и уборка", r"мусор|уборк|грязн|контейнер"),
    ("bezop", "Безопасность и домофон", r"домофон|охран|консьерж|пропуск|видеонаблюд|украли|краж"),
    ("deti", "Садики и школы", r"садик|детск\w* сад|школ|детск\w* площадк"),
    ("svet", "Электричество", r"электричеств|свет отключ|отключ\w* свет|без света|счётчик|счетчик"),
]


def src_ro() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True, timeout=60)
    con.row_factory = sqlite3.Row
    return con


def site() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=60)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def skip_sql(d: sqlite3.Connection | None = None, col: str = "s.id") -> str:
    """Условие «чат не исключён» для запросов к sources.db (см. cmd_chats)."""
    d = d or site()
    ids = [r[0] for r in d.execute("SELECT source_id FROM chat_skip")]
    return f" AND {col} NOT IN ({','.join(map(str, ids))})" if ids else ""


def env(name: str) -> str | None:
    try:
        for line in open(ENV, encoding="utf-8"):
            k, _, v = line.partition("=")
            if k.strip() == name:
                return v.strip().strip('"\'') or None
    except OSError:
        return None
    return None


def tg(text: str) -> None:
    """Сообщение Давиду в его бота. Сбой отправки работу не останавливает."""
    token, chat = env("TELEGRAM_TOKEN"), env("ALLOWED_USER_ID")
    if not token or not chat:
        return
    try:
        data = json.dumps({"chat_id": int(chat), "text": text,
                           "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                     data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=25).read()
    except Exception:
        pass


class Progress:
    """Строка в терминал на каждом шаге и сообщение в Telegram раз в 15 минут."""

    def __init__(self, title: str, total: int):
        self.title, self.total, self.t0 = title, total, time.time()
        self.last_tg = self.t0

    def step(self, i: int, extra: str = "") -> None:
        pct = 100 * i / max(self.total, 1)
        bar = "█" * int(pct // 5) + "░" * (20 - int(pct // 5))
        left = (time.time() - self.t0) / max(i, 1) * (self.total - i) / 60
        line = f"{self.title}: {bar} {i}/{self.total} ({pct:.0f}%), осталось ~{left:.0f} мин {extra}"
        print(line, flush=True)
        if time.time() - self.last_tg >= PROGRESS_EVERY:
            tg(f"⏳ Сайт ЖК — {line}")
            self.last_tg = time.time()

    def done(self, extra: str = "") -> None:
        tg(f"✅ Сайт ЖК — {self.title}: готово, {self.total} шт. {extra}".strip())


# ── ИМЕНА, ГОРОДА, СЛАГИ ─────────────────────────────────────────────────
TRANSLIT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
            "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
            "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
            "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
            "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya", "і": "i"}

CITY_TAIL = re.compile(
    r"[\s,\-–—]+(санкт[\s\-]*петербург|спб|питер|москва|мск|мо|ло|казань|"
    r"краснодар|екатеринбург|новосибирск|тюмень|воронеж|ростов[\s\-]*на[\s\-]*дону)\s*$", re.I)
SMALL = {"на", "в", "у", "и", "по", "за", "над", "под", "с", "со", "от", "до", "de", "la", "le"}


def slugify(s: str) -> str:
    t = "".join(TRANSLIT.get(ch, ch) for ch in (s or "").lower())
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t or "zhk"


# Хвосты из названий чатов, которые в справочник ЖК попали вместе с именем:
# «Аквилон ZALIVE Чаты», «Квартал Некрасовка Важные», «Триумф Парк Все».
NAME_TAIL = {"чат", "чаты", "чата", "важные", "важное", "главный", "главная", "общий",
             "общая", "все", "новости", "инфо", "официальный", "официальная", "жильцов",
             "жильцы", "соседи", "дольщики", "барахолка", "объявления", "оценка", "приёмка",
             # хвосты из названий чатов, найденные на сайте 23.09: «Okla ДЕТИ Родительский»,
             # «Хорошевский основная», «Мой адрес на», «Тополиная Топольковая Обмен»
             "основная", "основной", "родительский", "родительская", "родители", "дети", "детей",
             "мамы", "крд", "доска", "обмен", "территория", "тут", "собственники", "собственников",
             "владельцы", "новоселы", "новосёлы", "заселение", "для", "по", "в", "на", "и", "г",
             "город", "приемка"}


def nice_name(raw: str, city: str | None = None) -> str:
    n = (raw or "").strip()
    n = n.replace("«", "").replace("»", "").replace('"', "").replace("_", " ").strip()
    n = re.sub(r"\.\s*\d+\s*$", "", n)                       # «ID Мурино. 3» → «ID Мурино»
    n = re.sub(r"\s+г\.?\s*[А-ЯЁ][а-яё]+(?:-[А-ЯЁа-яё]+)*\s*$", "", n)   # «… г.Тюмень»

    # «Союз ЖК Союз» — префикс чата + «ЖК» + настоящее имя: берём то, что после «ЖК».
    parts = re.split(r"\s+жк\s+", n, flags=re.I)
    if len(parts) > 1 and parts[-1].strip():
        n = parts[-1]
    n = re.sub(r"^\s*жк\s+", "", n, flags=re.I)
    # Хвосты снимаем по кругу: город, служебные слова чата — «Галактика Краснодар Тут».
    # Город не снимаем, если от имени почти ничего не остаётся: «ID Мурино» — это имя.
    city_rx = re.compile(rf"[\s,.\-–—]+{re.escape(city.strip())}\s*$", re.I) if city else None
    while True:
        before = n
        for rx in (CITY_TAIL, city_rx):
            if rx is None:
                continue
            cut = rx.sub("", n).strip(" -–—,.")
            if sum(ch.isalpha() for ch in cut) >= 4:
                n = cut
        words = n.split()
        while len(words) > 1 and words[-1].lower().strip(".,!") in NAME_TAIL:
            words.pop()
        n = " ".join(words).strip(" -–—,.")
        if n == before:
            break
    letters = [c for c in n if c.isalpha()]
    latin = letters and all("a" <= c.lower() <= "z" for c in letters)
    if letters and len(letters) > 3 and (all(c.isupper() for c in letters) or
                                         (latin and all(c.islower() for c in letters))):
        # «НОВАЯ ОХТА» → «Новая Охта», «astra marine» → «Astra Marine»
        words = n.lower().split()
        n = " ".join(w if (i and w in SMALL) else w[:1].upper() + w[1:]
                     for i, w in enumerate(words))
    elif n[:1].islower():
        n = n[:1].upper() + n[1:]              # «ленинградский квартал» → «Ленинградский квартал»
    return n or (raw or "").strip()


# Адрес годится, если в нём есть улица: «Кушелевка, округ Пискарёвка» — это
# район, по нему дом не найти.
STREET = re.compile(r"\b(ул|улица|пр-кт|проспект|пр\.|ш\.|шоссе|наб|набережная|пер|переулок|"
                    r"б-р|бульвар|проезд|дорога|линия|пл\.|площадь|аллея|тракт|тупик|кв-л|"
                    r"квартал|мкр|микрорайон)\b|\bд\.?\s*\d", re.I)
ADDR_DROP = re.compile(r",?\s*(Россия|[А-ЯЁ][А-ЯЁа-яё\-]+ федеральный округ|\d{6})\s*(?=,|$)")


def street_level(addr: str | None) -> bool:
    return bool(addr) and bool(STREET.search(addr))


def clean_address(addr: str | None) -> str | None:
    if not addr:
        return None
    a = ADDR_DROP.sub("", addr).strip(" ,")
    a = re.sub(r"^ЖК\s+[«\"]?[^,]+[»\"]?,\s*", "", a)          # «ЖК "Респект", …»
    a = re.sub(r"^г\.?\s+", "", a)                               # «г Санкт-Петербург», «г. Москва»
    a = re.sub(r",\s*г\.?\s+", ", ", a)
    a = re.sub(r"\bд\s+(\d)", r"д. \1", a)
    a = re.sub(r"\bк\s+(\d)", r"к. \1", a)
    return re.sub(r"\s{2,}", " ", a).strip(" ,") or None


def norm_city(c: str | None) -> str:
    c = re.sub(r"^\s*г\.?\s*", "", (c or "").strip())
    return {"Московская": "Московская область", "Ленинградская": "Ленинградская область",
            "Спб": "Санкт-Петербург", "СПб": "Санкт-Петербург", "спб": "Санкт-Петербург",
            "Питер": "Санкт-Петербург", "Мск": "Москва", "мск": "Москва"}.get(c, c) or "Город не указан"


# ── ЧАТЫ, КОТОРЫЕ НЕ СЧИТАЕМ ─────────────────────────────────────────────
# Решение Давида 24.09.2026. Справочник LeadHunter привязывает чат к ЖК по имени,
# и киевский «ЖК СОФІЯ» попал к петербургской «Софии». Украинский текст наши темы
# (русские слова) не ловят — у такого ЖК «0% жалоб», и он первый в «меньше жалоб».
# Барахолки и знакомства — не про жизнь дома.
UA_LETTERS = re.compile(r"[іїєґІЇЄҐ]")
UA_SHARE = 0.20                     # доля сообщений с украинскими буквами
UA_MIN_MSGS = 20                    # меньше сообщений — долю не считаем
SKIP_TITLE = re.compile(r"барахолк|купля[\s\-–]*продаж|отдам\s+даром|приму\s+в\s+дар|\bдаром\b"
                        r"|маркет|market|знакомств", re.I)


def cmd_chats(a) -> None:
    s, d = src_ro(), site()
    stat = defaultdict(lambda: [0, 0])
    for r in s.execute(
            "SELECT m.source_id sid, m.text FROM message m JOIN source s ON s.id = m.source_id"
            " WHERE s.is_zhk = 1 AND s.zhk_id IS NOT NULL AND m.dup_of IS NULL AND m.text NOT LIKE ?"
            "   AND m.posted_at >= datetime('now','-30 days')", (SPAM,)):
        c = stat[r["sid"]]
        c[0] += 1
        c[1] += bool(UA_LETTERS.search(r["text"] or ""))
    skip = []
    for r in s.execute("SELECT id, title FROM source WHERE is_zhk = 1 AND zhk_id IS NOT NULL"):
        n, ua = stat.get(r["id"], (0, 0))
        share = ua / n if n else 0.0
        reason = None
        if n >= UA_MIN_MSGS and share >= UA_SHARE:
            reason = "украинский"
        elif SKIP_TITLE.search(r["title"] or ""):
            m = SKIP_TITLE.search(r["title"]).group(0).lower()
            reason = "знакомства" if m.startswith("знакомств") else "барахолка"
        if reason:
            skip.append((r["id"], reason, r["title"], n, round(share, 3), now()))
    d.execute("DELETE FROM chat_skip")
    d.executemany("INSERT INTO chat_skip VALUES (?,?,?,?,?,?)", skip)
    d.commit()
    by = Counter(x[1] for x in skip)
    print(f"не считаем чатами ЖК: {len(skip)} — " + ", ".join(f"{k} {v}" for k, v in by.most_common()))
    for x in sorted(skip, key=lambda x: -x[3])[:12]:
        print(f"  {x[1]:<11} {x[3]:>6} сообщ., укр. {x[4]:.0%}  {x[2]}")


# ── ОТБОР ────────────────────────────────────────────────────────────────
def put_chats(s: sqlite3.Connection, d: sqlite3.Connection, owner: int, of: int) -> int:
    """Чаты ЖК `of` из sources.db — в список чатов ЖК `owner` (кроме исключённых)."""
    n = 0
    for c in s.execute("SELECT id, title, username, invite_hash, members FROM source"
                       " WHERE is_zhk = 1 AND zhk_id = ? AND status <> 'dead'" + skip_sql(d, "id"), (of,)):
        if c["username"]:
            link = f"https://t.me/{c['username']}"
        elif c["invite_hash"]:
            link = f"https://t.me/+{c['invite_hash']}"
        else:
            continue
        d.execute("INSERT OR REPLACE INTO chat VALUES (?,?,?,?,?)", (owner, c["id"], c["title"], link, c["members"]))
        n += 1
    return n


def cmd_select(a) -> None:
    s, d = src_ro(), site()
    skip = skip_sql(d)
    rows = s.execute(
        "SELECT s.zhk_id, COUNT(*) n, COUNT(DISTINCT m.author_username) au"
        " FROM message m JOIN source s ON s.id = m.source_id"
        " WHERE s.is_zhk = 1 AND s.zhk_id IS NOT NULL"
        "   AND m.dup_of IS NULL AND m.text NOT LIKE ?"
        "   AND m.posted_at >= datetime('now','-30 days')" + skip +
        " GROUP BY s.zhk_id HAVING n >= ?", (SPAM, MIN_MSGS)).fetchall()
    stats = {r["zhk_id"]: (r["n"], r["au"]) for r in rows}
    d.execute("UPDATE zhk SET active = 0")
    d.execute("DELETE FROM chat")          # списки чатов — заново: старые могли держать исключённые чаты
    used: dict[tuple, int] = {}
    for zid, (n, au) in sorted(stats.items()):
        z = s.execute("SELECT * FROM zhk WHERE id = ?", (zid,)).fetchone()
        if not z:
            continue
        city = norm_city(z["city"])
        name = nice_name(z["name"], city)
        base = slugify(name)
        key = (city, base)
        used[key] = used.get(key, 0) + 1
        slug = base if used[key] == 1 else f"{base}-{used[key]}"
        addr = s.execute(
            "SELECT address FROM building WHERE zhk_id = ? AND COALESCE(address,'') <> ''"
            " ORDER BY (geo_precision = 'exact') DESC, id LIMIT 1", (zid,)).fetchone()
        chats = s.execute(
            "SELECT id, title, username, invite_hash, members FROM source"
            " WHERE is_zhk = 1 AND zhk_id = ? AND status <> 'dead'" + skip_sql(d, "id"), (zid,)).fetchall()
        d.execute(
            "INSERT INTO zhk (id, name, name_raw, slug, city, city_slug, region, district,"
            " metro, lat, lon, developer_base, address_base, msgs_30d, authors_30d, chats,"
            " active, selected_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)"
            " ON CONFLICT(id) DO UPDATE SET name=excluded.name, name_raw=excluded.name_raw,"
            " slug=excluded.slug, city=excluded.city, city_slug=excluded.city_slug,"
            " region=excluded.region, district=excluded.district, metro=excluded.metro,"
            " lat=excluded.lat, lon=excluded.lon, developer_base=excluded.developer_base,"
            " address_base=excluded.address_base, msgs_30d=excluded.msgs_30d,"
            " authors_30d=excluded.authors_30d, chats=excluded.chats, active=1,"
            " selected_at=excluded.selected_at",
            (zid, name, z["name"], slug, city, slugify(city), z["region"], z["district"],
             z["metro"], z["lat"], z["lon"], (z["developer"] or "").strip() or None,
             addr["address"] if addr else None, n, au, len(chats), now()))
        d.execute("DELETE FROM chat WHERE zhk_id = ?", (zid,))
        put_chats(s, d, zid, zid)
    d.commit()
    n_act = d.execute("SELECT COUNT(*) FROM zhk WHERE active = 1").fetchone()[0]
    print(f"отобрано ЖК: {n_act} (≥{MIN_MSGS} чистых сообщений за 30 дней)")
    for r in d.execute("SELECT city, COUNT(*) n FROM zhk WHERE active = 1"
                       " GROUP BY city ORDER BY n DESC LIMIT 12"):
        print(f"  {r['city']:<28} {r['n']}")


# ── ЕРЗ ──────────────────────────────────────────────────────────────────
def get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def region_of(s: sqlite3.Connection, city: str | None) -> str | None:
    """Субъект для отсева тёзок в ЕРЗ: у городов-субъектов — сам город."""
    c = re.sub(r"^\s*г\.?\s*", "", (city or "").strip())
    if c in ("Санкт-Петербург", "Москва", "Московская", "Ленинградская", "Севастополь"):
        return c
    r = s.execute("SELECT region FROM settlement WHERE name = ? AND region IS NOT NULL"
                  " LIMIT 1", (c,)).fetchone()
    return r["region"] if r else None


AGGLO_CITIES = {"Москва", "Санкт-Петербург", "Московская область", "Ленинградская область"}


def same_city(ours: str, erz_locality: str | None) -> bool:
    """Тот ли это город. Тёзки внутри одного субъекта — обычное дело:
    «Самолёт» из Краснодара ЕРЗ сопоставил с одноимённым ЖК в Крымске.
    В столичных агломерациях у ЕРЗ деревни и посёлки Новой Москвы и области,
    поэтому там город не сверяем — субъект уже сверен при поиске."""
    if ours in AGGLO_CITIES or not erz_locality:
        return True
    loc = re.sub(r"^(г|гор|пос|пгт|дер|д|с|рп|мкр)\.?\s+", "", erz_locality.strip(), flags=re.I)
    return loc.lower().replace("ё", "е") == ours.lower().replace("ё", "е")


def cmd_erz(a) -> None:
    from lh_erz import build_index, key_of, lookup
    from lh_geocode import clean_name
    s, d = src_ro(), site()
    if "name" not in {r[1] for r in d.execute("PRAGMA table_info(erz)")}:
        d.execute("ALTER TABLE erz ADD COLUMN name TEXT")
    idx, keys = build_index(s)
    todo = d.execute(
        "SELECT z.id, z.name_raw, z.city FROM zhk z LEFT JOIN erz e ON e.zhk_id = z.id"
        " WHERE z.active = 1 AND (e.zhk_id IS NULL OR (e.slug IS NOT NULL AND e.fetched_at IS NULL))"
        " ORDER BY z.msgs_30d DESC").fetchall()
    if a.limit:
        todo = todo[:a.limit]
    print(f"к сопоставлению с ЕРЗ: {len(todo)}")
    p, found, fetched = Progress("ЕРЗ", len(todo)), 0, 0
    for i, r in enumerate(todo, 1):
        name = clean_name(r["name_raw"]) or r["name_raw"]
        cand, how = lookup(key_of(name), idx, keys, region_of(s, r["city"]))
        if not cand or len(cand) != 1:
            d.execute("INSERT OR REPLACE INTO erz (zhk_id, how) VALUES (?, ?)",
                      (r["id"], "неоднозначно" if cand else "не найден"))
            d.commit()
            p.step(i, f"| найдено {found}")
            continue
        slug = cand[0][0]
        row = {"zhk_id": r["id"], "slug": slug, "how": how, "fetched_at": now()}
        try:
            j = get_json(ERZ_API + slug)
            bd = (j.get("brandAndDevelopers") or [{}])[0]
            brand = bd.get("brand") or {}
            row.update(
                brand=brand.get("organizationName") or brand.get("name"),
                brand_place=brand.get("place"), brand_rating=brand.get("ratingErzV2"),
                developers="; ".join(x.get("developerName", "") for x in bd.get("developers") or []
                                     if x.get("developerName")) or None,
                date_start=j.get("dateBuildStart"), date_end=j.get("dateBuildEnd"),
                stage=str(j.get("buildStage")) if j.get("buildStage") is not None else None,
                raion=j.get("raion"), locality=j.get("nasel_punkt"),
                metro=", ".join(m.get("name", "") for m in j.get("metroList") or [] if m.get("name")) or None,
                site=j.get("site"), name=j.get("name"), error=None)
            if not same_city(r["city"], row.get("locality")):
                row["how"] = f"другой город: {row.get('locality')}"
            elif row.get("brand") or row.get("developers"):
                found += 1
            fetched += 1
        except Exception as e:
            row.update(error=f"{type(e).__name__}: {e}"[:200])
        cols = ", ".join(row)
        d.execute(f"INSERT OR REPLACE INTO erz ({cols}) VALUES ({', '.join('?' * len(row))})",
                  list(row.values()))
        d.commit()
        p.step(i, f"| найдено {found}")
        time.sleep(ERZ_DELAY)
    p.done(f"застройщик найден у {found}, запрошено {fetched}")


# ── АДРЕС ПО КООРДИНАТАМ ─────────────────────────────────────────────────
def cmd_geocode(a) -> None:
    d = site()
    todo = [r for r in d.execute(
        "SELECT z.id, z.lat, z.lon, z.address_base FROM zhk z LEFT JOIN geo g ON g.zhk_id = z.id"
        " WHERE z.active = 1 AND z.lat IS NOT NULL"
        "   AND (g.zhk_id IS NULL OR g.error IS NOT NULL)").fetchall()
        if not street_level(r["address_base"])]
    if a.limit:
        todo = todo[:a.limit]
    print(f"к геокодированию: {len(todo)}")
    p, ok = Progress("адреса", len(todo)), 0
    for i, r in enumerate(todo, 1):
        q = urllib.parse.urlencode({"format": "jsonv2", "lat": r["lat"], "lon": r["lon"],
                                    "zoom": 18, "addressdetails": 1, "accept-language": "ru"})
        try:
            j = get_json(f"{NOMINATIM}?{q}")
            ad = j.get("address") or {}
            road = ad.get("road") or ad.get("pedestrian") or ad.get("residential") or ad.get("neighbourhood")
            house = ad.get("house_number")
            loc = (ad.get("city") or ad.get("town") or ad.get("village")
                   or ad.get("municipality") or ad.get("county") or ad.get("state"))
            addr = ", ".join(x for x in (loc, road, f"д. {house}" if house else None) if x) or None
            d.execute("INSERT OR REPLACE INTO geo VALUES (?,?,?,?,?,?,NULL)",
                      (r["id"], addr, road, house, loc, now()))
            ok += 1 if road else 0
        except Exception as e:
            d.execute("INSERT OR REPLACE INTO geo (zhk_id, fetched_at, error) VALUES (?,?,?)",
                      (r["id"], now(), f"{type(e).__name__}: {e}"[:200]))
        d.commit()
        p.step(i, f"| с улицей {ok}")
        time.sleep(GEO_DELAY)
    p.done(f"с улицей {ok}")


# ── ГОРОД И РЕГИОН ПО КООРДИНАТАМ ────────────────────────────────────────
# Решение Давида 24.09.2026. Справочник LeadHunter даёт городу три беды: регион вместо
# города («Московская область», «Свердловская»), обрубок имени («Набережных») и чужие
# координаты — геокодер по имени нашёл тёзку в другом регионе, и у московского «Баланса»
# на странице адрес в Новой Адыгее. Правило:
#   регион по координатам = регион города          → город остаётся (вне столиц — ≤30 км от центра);
#   город — это регион или непонятное слово        → город по координатам;
#   соседний регион (Москва↔МО, СПб↔ЛО)             → город по координатам;
#   дальний регион или >30 км от центра            → координаты ложные: ищем ЖК по имени
#                                                    в пределах города, не нашли — снимаем;
#   страна не Россия (Крым и Севастополь — Россия)  → снимаем.
# Город по координатам — ближайший город из справочника населённых пунктов (settlement)
# того же региона: Nominatim на уровне города отвечает «Ленинский городской округ».
CITY_R_KM = 30
FED_CITIES = {"Москва", "Санкт-Петербург", "Севастополь"}
NEIGHBOURS = {frozenset(("москва", "московск")), frozenset(("санкт-пе", "ленингра"))}
NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
SEARCH_CLASSES = {"landuse", "building", "place", "residential"}


def reg_key(r: str | None) -> str:
    """Ключ субъекта: «Республика Татарстан (Татарстан)», «Татарстан» → «татарста»."""
    r = (r or "").lower().replace("ё", "е")
    r = re.sub(r"\(.*?\)|/.*?/", " ", r)
    r = re.sub(r"\b(республика|респ|автономная|автономный|округ|область|обл|край|город|г)\b\.?", " ", r)
    r = r.replace("—", " ").replace(" - ", " ").strip()
    k = (r.split() or [""])[0][:8]
    return REG_ALIAS.get(k, k)


# Nominatim и справочник называют субъект по-разному: «Чувашия» — «Чувашская Республика».
REG_ALIAS = {"чувашия": "чувашска", "удмуртия": "удмуртск", "кабардин": "кабардин",
             "карачаев": "карачаев", "чечня": "чеченска", "якутия": "саха"}
# Субъекты, которые справочник населённых пунктов LeadHunter ведёт как российские,
# хотя Nominatim отдаёт у них код страны ua (решение по Крыму — как у справочника).
RU_ADMIN = {"крым", "севастоп", "донецкая", "луганска", "запорожс", "херсонск"}


def haversine(a_lat, a_lon, b_lat, b_lon) -> float:
    import math
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    x = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b_lon - a_lon) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(x))


class Places:
    """Справочник населённых пунктов LeadHunter: город → регион и центр; ближайший город."""

    def __init__(self, s: sqlite3.Connection):
        self.city: dict[str, dict] = {}
        self.towns: dict[str, list[dict]] = defaultdict(list)
        self.regions: set[str] = set()
        for r in s.execute("SELECT name, type, region, geo_lat, geo_lon, population, source FROM settlement"
                           " WHERE region IS NOT NULL"):
            r = dict(r)
            self.regions.add(reg_key(r["region"]))
            if r["geo_lat"] is None:
                continue
            r["key"] = reg_key(r["region"])
            # Город — «г» из hflabs или запись, добавленная из OSM (Москва, Химки: в hflabs их нет).
            r["town"] = r["type"] == "г" or (r["type"] is None and r["source"] == "osm")
            if r["town"]:
                self.towns[r["key"]].append(r)
            k = r["name"].lower().replace("ё", "е")
            old = self.city.get(k)
            rank = (r["town"], r["population"] or 0)
            if not old or rank > (old["town"], old["population"] or 0):
                self.city[k] = r
        self.regions |= RU_ADMIN

    def get(self, name: str) -> dict | None:
        return self.city.get((name or "").lower().replace("ё", "е"))

    def is_region(self, name: str) -> bool:
        if name in FED_CITIES:            # в справочнике Москва и Петербург — без типа «г»
            return False
        c = self.get(name)
        if c and c["town"]:
            return False
        return reg_key(name) in self.regions

    def locality(self, key: str, lat: float, lon: float, raw: str | None) -> tuple[dict | None, float]:
        """Город по координатам внутри субъекта key: город из ответа геокодера, затем
        городской округ («городской округ Люберцы», «Одинцовский городской округ»),
        и только потом ближайший город справочника."""
        ad = json.loads(raw or "{}")
        names = [re.sub(r"^город\s+", "", ad.get(k) or "") for k in ("city", "town")]
        county = ad.get("county") or ""
        cm = re.sub(r"(городской|муниципальный)\s+округ|район|город", " ", county).strip()
        for n in names:
            t = self.get(n) if n else None
            if t and t["town"] and t["key"] == key:
                return t, haversine(lat, lon, t["geo_lat"], t["geo_lon"])
        # Точка внутри города (≤5 км от центра): Мурино, Кудрово — сами города,
        # хотя геокодер называет только «Всеволожский район».
        t, dist = self.nearest(key, lat, lon)
        if t and dist <= 5:
            return t, dist
        if cm:
            stem = cm.lower().replace("ё", "е")[:5]
            cand = [t for t in self.towns.get(key, []) if t["name"].lower().replace("ё", "е").startswith(stem)
                    and haversine(lat, lon, t["geo_lat"], t["geo_lon"]) <= 40]
            if len(stem) >= 4 and cand:
                t = max(cand, key=lambda t: t["population"] or 0)
                return t, haversine(lat, lon, t["geo_lat"], t["geo_lon"])
        return self.nearest(key, lat, lon)

    def nearest(self, key: str, lat: float, lon: float) -> tuple[dict | None, float]:
        best, dist = None, 1e9
        for t in self.towns.get(key, []):
            dd = haversine(lat, lon, t["geo_lat"], t["geo_lon"])
            if dd < dist:
                best, dist = t, dd
        return best, dist


def reverse(d: sqlite3.Connection, lat: float, lon: float) -> sqlite3.Row:
    """Регион и страна по координатам (Nominatim, zoom 10), с кэшем в таблице rev."""
    lat, lon = round(lat, 6), round(lon, 6)
    r = d.execute("SELECT * FROM rev WHERE lat = ? AND lon = ?", (lat, lon)).fetchone()
    if r:
        return r
    q = urllib.parse.urlencode({"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 10,
                                "addressdetails": 1, "accept-language": "ru"})
    j = get_json(f"{NOMINATIM}?{q}")
    time.sleep(GEO_DELAY)
    ad = j.get("address") or {}
    city = re.sub(r"^город\s+", "", ad.get("city") or ad.get("town") or "") or None
    d.execute("INSERT OR REPLACE INTO rev VALUES (?,?,?,?,?,?,?)",
              (lat, lon, ad.get("country_code"), ad.get("state"), city,
               json.dumps(ad, ensure_ascii=False), now()))
    d.commit()
    return d.execute("SELECT * FROM rev WHERE lat = ? AND lon = ?", (lat, lon)).fetchone()


def in_russia(rv) -> bool:
    return rv["country"] == "ru" or reg_key(rv["state"]) in RU_ADMIN


def street_address(lat: float, lon: float) -> str | None:
    """Адрес до улицы по координатам (zoom 18) — как в cmd_geocode."""
    q = urllib.parse.urlencode({"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 18,
                                "addressdetails": 1, "accept-language": "ru"})
    ad = get_json(f"{NOMINATIM}?{q}").get("address") or {}
    time.sleep(GEO_DELAY)
    road = ad.get("road") or ad.get("pedestrian") or ad.get("residential")
    loc = ad.get("city") or ad.get("town") or ad.get("village") or ad.get("municipality")
    if not road:
        return None
    house = ad.get("house_number")
    return ", ".join(x for x in (loc, road, f"д. {house}" if house else None) if x)


def bare_name(t: str) -> str:
    t = (t or "").lower().replace("ё", "е")
    t = re.sub(r"^\s*(жк|жилой\s+комплекс)\s+", "", t)
    return re.sub(r"[«»\"“”„']", "", t).strip()


def search_in_city(name: str, c: dict) -> tuple[float, float, str] | None:
    """ЖК по имени в пределах города. Берём только жилой объект (landuse, building, place)
    с тем же именем целиком («Новая» ≠ «ЖК Новая жизнь»), в адресе которого есть сам
    город, не дальше 30 км от центра — иначе «Центральный» из Балашихи нашёлся в Пушкино."""
    box = f"{c['geo_lon'] - 0.5},{c['geo_lat'] + 0.3},{c['geo_lon'] + 0.5},{c['geo_lat'] - 0.3}"
    city = c["name"].lower().replace("ё", "е")
    for q in (f"ЖК {name}", name):
        qs = urllib.parse.urlencode({"format": "jsonv2", "q": q, "viewbox": box, "bounded": 1,
                                     "limit": 10, "accept-language": "ru"})
        res = get_json(f"{NOMINATIM_SEARCH}?{qs}")
        time.sleep(GEO_DELAY)
        for x in res:
            parts = [p.strip().lower().replace("ё", "е") for p in x.get("display_name", "").split(",")]
            lat, lon = float(x["lat"]), float(x["lon"])
            if (x.get("category", x.get("class")) in SEARCH_CLASSES and parts
                    and bare_name(parts[0]) == bare_name(name) and city in parts[1:]
                    and haversine(lat, lon, c["geo_lat"], c["geo_lon"]) <= CITY_R_KM):
                return lat, lon, x.get("display_name", "")[:200]
    return None


def search_city(name: str) -> dict | None:
    """Город, которого нет в справочнике России: где он? (Минск, Семей)."""
    qs = urllib.parse.urlencode({"format": "jsonv2", "q": name, "featureType": "city", "limit": 1,
                                 "addressdetails": 1, "accept-language": "ru"})
    res = get_json(f"{NOMINATIM_SEARCH}?{qs}")
    time.sleep(GEO_DELAY)
    if not res:
        return None
    ad = res[0].get("address") or {}
    return {"country": ad.get("country_code"), "state": ad.get("state"),
            "geo_lat": float(res[0]["lat"]), "geo_lon": float(res[0]["lon"])}


def cmd_place(a) -> None:
    s, d = src_ro(), site()
    P = Places(s)
    rows = d.execute("SELECT id, name, name_raw, city, lat, lon, address_base FROM zhk"
                     " WHERE active = 1 AND lat IS NOT NULL ORDER BY msgs_30d DESC").fetchall()
    if a.limit:
        rows = rows[:a.limit]
    old = {r["zhk_id"]: r for r in d.execute("SELECT * FROM place")}
    todo = sum(1 for r in rows if not d.execute("SELECT 1 FROM rev WHERE lat=? AND lon=?",
                                                (round(r["lat"], 6), round(r["lon"], 6))).fetchone())
    print(f"ЖК: {len(rows)}, координат без кэша: {todo} (~{todo * GEO_DELAY / 60:.0f} мин)")
    prog, stats = Progress("города", len(rows)), Counter()
    foreign_city: dict[str, dict | None] = {}
    for i, z in enumerate(rows, 1):
        prog.step(i, "| " + ", ".join(f"{k} {v}" for k, v in stats.most_common(4)))
        dir_city = norm_city(z["city"])
        name = nice_name(z["name_raw"] or z["name"], dir_city)
        prev = old.get(z["id"])
        # Координаты те же, решение уже принято поиском — не ищем заново каждый день.
        if prev and prev["how"] == "поиск" and prev["src_lat"] == z["lat"] and prev["src_lon"] == z["lon"]:
            stats[prev["how"]] += 1
            continue
        rv = reverse(d, z["lat"], z["lon"])
        lat, lon, how, hide, city, note, address = z["lat"], z["lon"], None, None, None, None, None
        c = P.get(dir_city)
        if c is None and not P.is_region(dir_city) and dir_city != "Город не указан":
            if dir_city not in foreign_city:
                foreign_city[dir_city] = search_city(dir_city)
            fc = foreign_city[dir_city]
            if fc and fc["country"] and fc["country"] != "ru" and reg_key(fc["state"]) not in RU_ADMIN:
                hide, how, note = "вне России", "город вне России", f"{dir_city}: {fc['country']}"
        rk = reg_key(rv["state"])
        if hide:
            pass
        elif not in_russia(rv) and c is None:
            hide, how, note = "вне России", "координаты вне России", f"{rv['country']} {rv['state']}"
        elif c is None or P.is_region(dir_city):
            # Регион или непонятное слово вместо города: город по координатам.
            t, dist = P.locality(rk, lat, lon, rv["raw"])
            if rk in ("москва", "санкт-пе", "севастоп"):
                city, how = {"москва": "Москва", "санкт-пе": "Санкт-Петербург", "севастоп": "Севастополь"}[rk], "регион→город"
            elif t and dist <= CITY_R_KM and (c is None or reg_key(dir_city) == rk):
                city, how = t["name"], "регион→город"
            else:
                hide, how, note = "город не определён", "регион→?", f"{rv['state']}, ближайший {t and t['name']} {dist:.0f} км"
        else:
            ck = c["key"]
            dist = haversine(lat, lon, c["geo_lat"], c["geo_lon"])
            if in_russia(rv) and rk == ck and (dir_city in FED_CITIES or dist <= CITY_R_KM):
                city, how = c["name"], "совпал"
            elif in_russia(rv) and (frozenset((rk, ck)) in NEIGHBOURS or dist <= CITY_R_KM):
                # Соседний субъект: Подмосковье у «московского» ЖК, Новая Адыгея у краснодарского.
                t, tdist = P.locality(rk, lat, lon, rv["raw"])
                if rk in ("москва", "санкт-пе"):
                    city = "Москва" if rk == "москва" else "Санкт-Петербург"
                elif t and tdist <= CITY_R_KM:
                    city = t["name"]
                if city:
                    how, note = "соседний регион", f"{dir_city} → {city}"
                else:
                    hide, how, note = "город не определён", "соседний регион→?", f"{rv['state']} {tdist:.0f} км"
            else:
                # Дальний регион или далеко от центра: координаты от тёзки. Ищем по имени в городе.
                found = search_in_city(name, c)
                if found:
                    lat, lon, note = found[0], found[1], f"было {rv['state']}, {dist:.0f} км; найдено: {found[2]}"
                    rv2 = reverse(d, lat, lon)
                    if reg_key(rv2["state"]) == ck:
                        city, how = c["name"], "поиск"
                        address = street_address(lat, lon)
                    else:
                        hide, how = "ложные координаты", "поиск: чужой регион"
                else:
                    hide, how, note = "ложные координаты", "не найден", f"{rv['state']}, {dist:.0f} км от центра {dir_city}"
        # Показанный адрес не должен называть город другого субъекта. На границе
        # Москвы и области геокодер отвечает по-разному на разном масштабе: регион —
        # «Московская область», а адрес дома — «Москва, Новосходненское шоссе».
        # Соседний субъект — верим адресу дома (он точнее), дальний — берём адрес заново.
        if city and not hide:
            ck = (P.get(city) or {}).get("key") or reg_key(city)
            g = d.execute("SELECT address, road FROM geo WHERE zhk_id = ?", (z["id"],)).fetchone()
            shown = address or (z["address_base"] if street_level(z["address_base"]) else None) \
                or (g["address"] if g and g["road"] else None)
            for part in (x.strip() for x in (clean_address(shown) or "").split(",")):
                t = P.get(re.sub(r"^(город|г)\.?\s+", "", part))
                # Город другого субъекта — только если он рядом: «Мирный» у Томилино — посёлок,
                # а не якутский город, «Павловск» у «Южного форта» — петербургский.
                if not t or not t["town"] or t["key"] == ck or t["name"] == city \
                        or haversine(lat, lon, t["geo_lat"], t["geo_lon"]) > 50:
                    continue
                if frozenset((t["key"], ck)) in NEIGHBOURS or haversine(lat, lon, t["geo_lat"], t["geo_lon"]) <= CITY_R_KM:
                    note = f"адрес дома в {t['name']}: {city} → {t['name']}"
                    city, how = t["name"], how + ", адрес"
                else:
                    address = street_address(lat, lon)
                    fresh = [x.strip() for x in (address or "").split(",")]
                    if not address or any((P.get(x) or {}).get("town") and P.get(x)["key"] != ck for x in fresh):
                        hide, note = "адрес чужого региона", f"{shown}"
                break
        stats[how] += 1
        d.execute("INSERT OR REPLACE INTO place VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (z["id"], city if not hide else None, slugify(city) if city and not hide else None,
                   rv["state"] if how != "поиск" else c["region"], lat, lon, how, hide, address, note,
                   z["lat"], z["lon"], now()))
        d.commit()
    prog.done(", ".join(f"{k} {v}" for k, v in stats.most_common()))
    for r in d.execute("SELECT p.*, z.name, z.city dir_city FROM place p JOIN zhk z ON z.id = p.zhk_id"
                       " WHERE z.active = 1 AND (p.hide IS NOT NULL OR p.how IN ('поиск', 'соседний регион'))"
                       " ORDER BY p.hide IS NULL, p.how"):
        print(f"  {r['how']:<18} {r['hide'] or '':<18} {r['name']} ({r['dir_city']} → {r['city']}) {r['note'] or ''}")


# ── УК ПО УПОМИНАНИЯМ ────────────────────────────────────────────────────
LEGAL = r"(?:(?:ООО|АО|ЗАО|ПАО|ОАО|ГУП|ГБУ|МУП|СПб\s+ГБУ)\s+)?"
UK_Q = re.compile(r'(?:\bУК|[Уу]правляющ\w*\s+компани\w*)\s*' + LEGAL
                  + r'[«"“„]([^»"”“\n]{2,40})[»"”“]')
UK_C = re.compile(r'(?:\bУК|[Уу]правляющ\w*\s+компани\w*)\s+' + LEGAL
                  + r'([А-ЯЁA-Z][А-ЯЁA-Zа-яёa-z0-9]+(?:[\-‑\s][А-ЯЁA-Z][А-ЯЁA-Zа-яёa-z0-9]+){0,2})')
UK_STOP = {"сказали", "ответили", "ответила", "сказала", "пишет", "говорит", "обещали",
           "это", "они", "там", "нет", "да", "все", "только", "тоже", "опять", "уже",
           "если", "когда", "как", "что", "почему", "сегодня", "вчера", "просто", "вообще",
           "ооо", "ао", "зао", "пао", "лдпр", "кпрф", "единая россия", "жк", "тсж", "ук",
           "мы", "вы", "он", "она", "спб", "мск", "москва", "санкт-петербург", "россия"}


def uk_key(s: str) -> str:
    k = s.lower().replace("ё", "е").replace("‑", "-")
    k = re.sub(r"\s*-\s*", "-", k)
    return re.sub(r"\s+", " ", k).strip(" .,!?-")


def cmd_uk(a) -> None:
    s, d = src_ro(), site()
    ids = [r[0] for r in d.execute("SELECT id FROM zhk WHERE active = 1")]
    idset = set(ids)
    glob, chats = Counter(), defaultdict(set)
    per: dict[int, Counter] = defaultdict(Counter)
    shown: dict[str, Counter] = defaultdict(Counter)
    for r in s.execute(
            "SELECT s.zhk_id, s.id sid, m.text FROM message m JOIN source s ON s.id = m.source_id"
            " WHERE s.is_zhk = 1 AND s.zhk_id IS NOT NULL AND m.dup_of IS NULL"
            "   AND (m.text LIKE '%УК%' OR m.text LIKE '%правляющ%')" + skip_sql(d)):
        t = r["text"] or ""
        for rx in (UK_Q, UK_C):
            for mm in rx.finditer(t):
                raw = mm.group(1).strip(" .,!?-")
                k = uk_key(raw)
                if len(k) < 3 or k in UK_STOP or k.split()[0] in UK_STOP:
                    continue
                glob[k] += 1
                chats[k].add(r["sid"])
                shown[k][raw] += 1
                if r["zhk_id"] in idset:
                    per[r["zhk_id"]][k] += 1
    good = {k for k, v in glob.items() if v >= 5 and len(chats[k]) >= 2}
    d.execute("DELETE FROM uk WHERE source = 'упоминания жильцов'")
    n = 0
    for zid in ids:
        cand = [(k, v) for k, v in per[zid].most_common() if k in good and v >= 3]
        if not cand:
            continue
        k, v = cand[0]
        d.execute("INSERT OR REPLACE INTO uk VALUES (?,?,?,?,?)",
                  (zid, shown[k].most_common(1)[0][0], "упоминания жильцов", v, now()))
        n += 1
    d.commit()
    print(f"словарь УК: {len(good)} имён; УК определена у {n} из {len(ids)} ЖК")


# ── СКЛЕЙКА ДУБЛЕЙ ───────────────────────────────────────────────────────
def km(a, b) -> float:
    """Расстояние по прямой между двумя ЖК (lat, lon), км."""
    import math
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


MERGE_HOW = ("точно", "по началу", "по префиксу")   # «неоднозначно» не склеиваем


def aliases(d: sqlite3.Connection) -> dict[int, int]:
    """ЖК-дубль → основной ЖК. Пусто, пока склейка ни разу не запускалась."""
    if "canon_id" not in {r[1] for r in d.execute("PRAGMA table_info(zhk)")}:
        return {}
    return dict(d.execute("SELECT id, canon_id FROM zhk WHERE canon_id IS NOT NULL"))


def cmd_merge(a) -> None:
    """Один комплекс в LeadHunter часто заведён несколько раз: чат дома, чат корпуса,
    барахолка («Метрополия», «Метрополия — Токио», «Метрополия — Вена»). Если ЕРЗ
    уверенно сопоставил их с одним и тем же ЖК, страница одна: самый живой чат —
    основной, остальные становятся его частью. Запускать после select и erz."""
    s, d = src_ro(), site()
    if "canon_id" not in {r[1] for r in d.execute("PRAGMA table_info(zhk)")}:
        d.execute("ALTER TABLE zhk ADD COLUMN canon_id INTEGER")
    # Повторный запуск без select: вернуть прошлые дубли в строй, иначе они потеряются.
    d.execute("UPDATE zhk SET active = 1 WHERE canon_id IS NOT NULL")
    d.execute("UPDATE zhk SET canon_id = NULL")
    groups = defaultdict(list)
    q = ("SELECT e.slug, z.id, z.msgs_30d FROM erz e JOIN zhk z ON z.id = e.zhk_id"
         f" WHERE z.active = 1 AND e.slug IS NOT NULL AND e.how IN ({','.join('?' * len(MERGE_HOW))})")
    for slug, zid, n in d.execute(q, MERGE_HOW):
        groups[slug].append((n, zid))
    erz_ids = {zid for members in groups.values() for _, zid in members}
    # Второй проход — чаты, которых ЕРЗ не нашёл, но по названию это тот же комплекс
    # в том же городе: «Метрополия МОСКВА» (то же имя) и корпуса через тире —
    # «Метрополия - Стокгольм», «Метрополия - Токио». Чат, который ЕРЗ сопоставил
    # с другим комплексом, сюда не попадает.
    named = defaultdict(list)                       # (город, имя) → slug группы
    for slug, members in groups.items():
        for _, zid in members:
            r = d.execute("SELECT city, name FROM zhk WHERE id = ?", (zid,)).fetchone()
            named[(r["city"], r["name"].lower())].append(slug)
    rest = d.execute("SELECT z.id, z.city, z.name, z.name_raw, z.msgs_30d FROM zhk z"
                     " LEFT JOIN erz e ON e.zhk_id = z.id"
                     " WHERE z.active = 1 AND (e.zhk_id IS NULL OR e.slug IS NULL)").fetchall()
    by_city = defaultdict(list)
    for (city, name), slugs in named.items():
        if len(set(slugs)) == 1:
            by_city[city].append((name, slugs[0]))
    extra = 0
    for r in rest:
        own = r["name"].lower()
        raw = (r["name_raw"] or "").lower()
        for name, slug in by_city.get(r["city"], []):
            if own == name or re.match(rf"{re.escape(name)}\s*[-–—:]\s*\w", raw):
                groups[slug].append((r["msgs_30d"], r["id"]))
                extra += 1
                break
    print(f"по названию в том же городе добавлено к ЖК из ЕРЗ: {extra}")
    # Третий проход — одноимённые ЖК без ЕРЗ в одном городе не дальше 1,5 км друг от друга:
    # это корпуса или чаты одного комплекса («ID Мурино» ×4). Дальше — разные комплексы-тёзки.
    already = {zid for m in groups.values() for _, zid in m}
    by_name = defaultdict(list)
    for r in d.execute("SELECT z.id, z.city, z.name, z.name_raw, z.lat, z.lon, z.msgs_30d FROM zhk z"
                       " LEFT JOIN erz e ON e.zhk_id = z.id"
                       " WHERE z.active = 1 AND (e.zhk_id IS NULL OR e.slug IS NULL)"):
        if r["id"] not in already:
            by_name[(r["city"], nice_name(r["name_raw"] or r["name"], r["city"]).lower())].append(r)
    near_n = 0
    for (city, name), members in by_name.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda r: -(r["msgs_30d"] or 0))
        main = members[0]
        near = [m for m in members[1:] if main["lat"] and m["lat"] and km(main, m) <= 1.5]
        if near:
            groups[f"name:{city}:{name}"] = [(main["msgs_30d"], main["id"])] + [(m["msgs_30d"], m["id"]) for m in near]
            near_n += len(near)
    print(f"одноимённых без ЕРЗ в пределах 1,5 км склеено: {near_n}")
    merged = 0
    for slug, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(reverse=True)
        # Основной — самый живой из тех, кого сопоставил ЕРЗ: у него застройщик и сроки.
        main_id = next((zid for _, zid in members if zid in erz_ids), members[0][1])
        ids = [main_id] + [zid for _, zid in members if zid != main_id]
        others = ids[1:]
        d.executemany("UPDATE zhk SET active = 0, canon_id = ? WHERE id = ?", [(main_id, o) for o in others])
        ph = ",".join("?" * len(ids))
        n, au = s.execute(
            "SELECT COUNT(*), COUNT(DISTINCT m.author_username) FROM message m JOIN source s ON s.id = m.source_id"
            f" WHERE s.is_zhk = 1 AND s.zhk_id IN ({ph}) AND m.dup_of IS NULL AND m.text NOT LIKE ?"
            "   AND m.posted_at >= datetime('now','-30 days')" + skip_sql(d), (*ids, SPAM)).fetchone()
        for o in others:
            put_chats(s, d, main_id, o)          # из sources.db: дубль мог не пройти отбор
        chats = d.execute("SELECT COUNT(*) FROM chat WHERE zhk_id = ?", (main_id,)).fetchone()[0]
        d.execute("UPDATE zhk SET msgs_30d = ?, authors_30d = ?, chats = MAX(chats, ?) WHERE id = ?",
                  (n, au, chats, main_id))
        merged += len(others)
    d.commit()
    n_act = d.execute("SELECT COUNT(*) FROM zhk WHERE active = 1").fetchone()[0]
    print(f"склеено: {merged} дублей в {sum(1 for m in groups.values() if len(m) > 1)} ЖК; "
          f"страниц ЖК теперь {n_act}")


# ── МЕТРИКИ ──────────────────────────────────────────────────────────────
def cmd_metrics(a) -> None:
    s, d = src_ro(), site()
    zs = {r["id"]: dict(r) for r in d.execute("SELECT id, city, msgs_30d FROM zhk WHERE active = 1")}
    alias = {k: v for k, v in aliases(d).items() if v in zs}   # чаты дублей считаем основному ЖК
    rx = [(key, re.compile(p, re.I)) for key, _, p in TOPICS]
    # Тональность из таблицы tone (DeepSeek или локальная модель). Реклама (A) в цифры
    # тем не входит совсем: объявление «сдам паркинг» — не мнение о парковке.
    tone = dict(d.execute("SELECT message_id, label FROM tone WHERE label IS NOT NULL")) \
        if d.execute("SELECT 1 FROM sqlite_master WHERE name='tone'").fetchone() else {}
    today = datetime.now(timezone.utc).date()
    week0 = today - timedelta(days=today.weekday())           # понедельник этой недели
    weeks = [week0 - timedelta(weeks=k) for k in range(6, 0, -1)]  # 6 полных недель
    t30, t7, t14 = today - timedelta(days=30), today - timedelta(days=7), today - timedelta(days=14)
    # zid → topic → [30d, 7d, prev7d, neg, pos, neu, unk, ad, neg_7d, neg_prev7d]
    cnt = defaultdict(lambda: defaultdict(lambda: [0] * 10))
    wk = defaultdict(Counter)
    ids = ",".join(map(str, list(zs) + list(alias)))
    q = s.execute(
        "SELECT m.id, s.zhk_id, m.posted_at, m.text FROM message m JOIN source s ON s.id = m.source_id"
        f" WHERE s.is_zhk = 1 AND s.zhk_id IN ({ids}) AND m.dup_of IS NULL AND m.text NOT LIKE ?"
        "   AND m.posted_at >= datetime('now','-50 days')" + skip_sql(d), (SPAM,))
    n = labelled = 0
    for r in q:
        n += 1
        zid = alias.get(r["zhk_id"], r["zhk_id"])
        day = datetime.fromisoformat(r["posted_at"][:10]).date()
        ws = day - timedelta(days=day.weekday())
        if weeks[0] <= ws < week0:
            wk[zid][ws.isoformat()] += 1
        if day < t30:
            continue
        t = (r["text"] or "").lower()
        hit = [key for key, rgx in rx if rgx.search(t)]
        if not hit:
            continue
        lab = tone.get(r["id"])
        labelled += lab is not None
        for key in hit:
            c = cnt[zid][key]
            if lab == "A":
                c[7] += 1
                continue
            c[0] += 1
            c[{"N": 3, "P": 4, "U": 5}.get(lab, 6)] += 1
            if day >= t7:
                c[1] += 1
                c[8] += lab == "N"
            elif day >= t14:
                c[2] += 1
                c[9] += lab == "N"
        if n % 200000 == 0:
            print(f"  … {n} сообщений", flush=True)
    d.execute("DROP TABLE IF EXISTS topic")        # производная таблица: схема могла вырасти
    d.executescript(SCHEMA)
    d.execute("DELETE FROM weekly")
    for zid, z in zs.items():
        m30 = max(z["msgs_30d"], 1)
        for key, _, _ in TOPICS:
            c = cnt[zid][key]
            d.execute("INSERT INTO topic (zhk_id, topic, n_30d, n_7d, n_prev7d, per_1000, n_neg, n_pos,"
                      " n_neu, n_unk, n_ad, neg_7d, neg_prev7d, neg_per_1000)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (zid, key, c[0], c[1], c[2], round(1000 * c[0] / m30, 1), c[3], c[4], c[5],
                       c[6], c[7], c[8], c[9], round(1000 * c[3] / m30, 1)))
        for w in weeks:
            d.execute("INSERT INTO weekly VALUES (?,?,?)", (zid, w.isoformat(), wk[zid][w.isoformat()]))
    # Место в городе: 1 — жалуются чаще всех (жалоб на 1000 сообщений чата). Сравниваем
    # только ЖК со 100+ сообщениями за месяц: у чата с тридцатью сообщениями
    # две жалобы на протечки дают 67 на тысячу и первое место, хотя это шум.
    # И только в городах, где таких ЖК хотя бы 5 — иначе место ничего не говорит.
    by_city = defaultdict(list)
    for zid, z in zs.items():
        if z["msgs_30d"] >= RANK_MIN_MSGS:
            by_city[z["city"]].append(zid)
    for city, members in by_city.items():
        if len(members) < 5:
            continue
        for key, _, _ in TOPICS:
            vals = sorted(((d.execute("SELECT neg_per_1000, per_1000 FROM topic WHERE zhk_id=? AND topic=?",
                                      (zid, key)).fetchone(), zid) for zid in members),
                          key=lambda v: (v[0][0], v[0][1]), reverse=True)
            for rank, (_, zid) in enumerate(vals, 1):
                d.execute("UPDATE topic SET city_rank=?, city_total=? WHERE zhk_id=? AND topic=?",
                          (rank, len(members), zid, key))
    d.commit()
    print(f"метрики: {n} сообщений, {len(zs)} ЖК, {len(TOPICS)} тем, недели {weeks[0]}…{weeks[-1]}; "
          f"с тональностью {labelled} сообщений с темами")


def cmd_status(a) -> None:
    d = site()
    rows = [
        ("ЖК", "SELECT COUNT(*) FROM zhk WHERE active = 1"),
        ("  адрес дома из базы", "SELECT COUNT(*) FROM zhk WHERE active = 1 AND COALESCE(address_base, '') <> ''"),
        ("  адрес по координатам", "SELECT COUNT(*) FROM geo g JOIN zhk z ON z.id = g.zhk_id"
                                   " WHERE z.active = 1 AND g.road IS NOT NULL"),
        ("  застройщик в базе", "SELECT COUNT(*) FROM zhk WHERE active = 1 AND developer_base IS NOT NULL"),
        ("  ЕРЗ сопоставлено", "SELECT COUNT(*) FROM erz e JOIN zhk z ON z.id = e.zhk_id"
                               " WHERE z.active = 1 AND e.slug IS NOT NULL"),
        ("  ЕРЗ: застройщик", "SELECT COUNT(*) FROM erz e JOIN zhk z ON z.id = e.zhk_id"
                              " WHERE z.active = 1 AND (e.brand IS NOT NULL OR e.developers IS NOT NULL)"),
        ("  УК", "SELECT COUNT(*) FROM uk u JOIN zhk z ON z.id = u.zhk_id WHERE z.active = 1"),
        ("  чатов со ссылкой", "SELECT COUNT(*) FROM chat c JOIN zhk z ON z.id = c.zhk_id WHERE z.active = 1"),
    ]
    for label, sql in rows:
        print(f"{label:<24} {d.execute(sql).fetchone()[0]}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("chats", cmd_chats), ("select", cmd_select), ("place", cmd_place), ("erz", cmd_erz), ("geocode", cmd_geocode),
                     ("uk", cmd_uk), ("merge", cmd_merge), ("metrics", cmd_metrics),
                     ("status", cmd_status)):
        sp = sub.add_parser(name)
        sp.add_argument("--limit", type=int, default=0, help="обработать не больше N (для пробы)")
        sp.set_defaults(fn=fn)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
