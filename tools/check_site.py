#!/usr/bin/env python3
"""Проверки гейтов GATES.md для «Тили-бом». Каждая печатает <ИМЯ>_OK только при успехе.

    check_site.py dataset       # G1: в базе все ЖК с ≥30 чистыми сообщениями (пересчёт по sources.db)
    check_site.py address       # G2: у каждой страницы ЖК адрес до улицы
    check_site.py developer     # G3: застройщик или «застройщик не найден в ЕРЗ»
    check_site.py uk            # G4: УК или «УК не указана в реестрах»
    check_site.py pages         # G5: страницы ЖК, городов, главная, методика; блоки на месте
    check_site.py privacy       # G6: ни ников, ни телефонов, ни цитат; подброшенный образец ловится
    check_site.py sitemap       # G7: sitemap.xml = все страницы, robots.txt на него ссылается
    check_site.py timer         # G8: таймер включён, последний прогон удачный и свежий
    check_site.py fail-alert    # G9: нарочный сбой пересборки шлёт сообщение в Telegram
    check_site.py deploy        # G11: HTTPS на поддомене: главная и три случайные страницы ЖК
    check_site.py gold [--labels PATH]     # G13: эталон размечен целиком, метки N/P/U/A
    check_site.py tone-model               # G14: локальная модель ≥ 90% на отложенных днях
    check_site.py tone-pages    # G15: у каждой темы видны жалобы и похвала, реклама в цифры не входит
    check_site.py sort          # G16: данные сортировки по жалобам на страницах городов и в rating.json
    check_site.py chats         # G19: украинские чаты, барахолки, знакомства не входят в цифры (пересчёт)
    check_site.py geo           # G20: город страницы — город России, регион по координатам совпадает
    check_site.py moved         # G21: 301 со старых адресов, снятые страницы — 404, IndexNow получил
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import glob
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/opt/zhk-site/tools")

ROOT = "/opt/zhk-site"
OUT = os.environ.get("SITE_OUT", f"{ROOT}/out")   # SITE_OUT — проверить пробную сборку
DB = f"{ROOT}/data/site.db"
SRC = "/opt/leadhunter/data/sources.db"
SPAM = "\ufeff%"

GOLD_N = 5000
TONE_MIN_ACC = 0.90


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def check_gold(labels_path: str) -> None:
    from gold import SAMPLE
    xs = [json.loads(l) for l in open(SAMPLE, encoding="utf-8")]
    if len(xs) != GOLD_N:
        fail(f"в выборке {len(xs)} сообщений, нужно {GOLD_N}")
    if len({x["id"] for x in xs}) != GOLD_N:
        fail("в выборке повторяются сообщения")
    if sorted(x["n"] for x in xs) != list(range(GOLD_N)):
        fail("номера выборки не 0..4999")
    lab = json.load(open(labels_path, encoding="utf-8"))
    missing = [n for n in range(GOLD_N) if str(n) not in lab]
    if missing:
        fail(f"не размечено {len(missing)}: {missing[:10]}")
    extra = [k for k in lab if not k.isdigit() or int(k) >= GOLD_N]
    if extra:
        fail(f"лишние ключи: {extra[:10]}")
    bad = {k: v for k, v in lab.items() if v not in ("N", "P", "U", "A")}
    if bad:
        fail(f"недопустимые метки: {list(bad.items())[:10]}")
    from collections import Counter
    c = Counter(lab.values())
    print(f"эталон: {len(lab)} меток, {dict(c)}")
    print("GOLD_OK")


def check_tone_model() -> None:
    import numpy as np
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    from gold import twin_key
    from tone_model import BERT_DIR, CLASSES, CUT, POOL, load_gold

    torch.set_num_threads(2)
    gold = load_gold()
    test = [x for x in gold if x["posted_at"] >= CUT]          # пересчёт, а не split.json
    train_keys = {twin_key(x["text"]) for x in gold if x["posted_at"] < CUT}
    pool_after = 0
    for line in open(POOL, encoding="utf-8"):
        x = json.loads(line)
        train_keys.add(twin_key(x["text"]))
        if x["posted_at"] >= CUT:
            pool_after += 1
    if pool_after:
        fail(f"в обучении {pool_after} сообщений с {CUT} и позже")
    meta = json.load(open(f"{BERT_DIR}/train_meta.json"))
    if meta.get("cut") != CUT:
        fail(f"модель обучена с разрезом {meta.get('cut')}, а проверка с {CUT}")
    test = [x for x in test if twin_key(x["text"]) not in train_keys]
    if len(test) < 800:
        fail(f"на проверке всего {len(test)} сообщений")
    tok = AutoTokenizer.from_pretrained(BERT_DIR)
    model = AutoModelForSequenceClassification.from_pretrained(BERT_DIR).eval()
    bias = np.asarray(meta.get("bias", [0.0] * len(CLASSES)), dtype=float)
    pred = []
    with torch.no_grad():
        for s in range(0, len(test), 64):
            enc = tok([x["text"] for x in test[s:s + 64]], truncation=True,
                      max_length=meta["maxlen"], padding=True, return_tensors="pt")
            lp = torch.log_softmax(model(**enc).logits, dim=1).numpy() + bias
            pred += [CLASSES[int(i)] for i in np.argmax(lp, axis=1)]
    acc = sum(p == x["label"] for p, x in zip(pred, test)) / len(test)
    print(f"отложенные дни с {CUT}: {len(test)} сообщений, верно {acc * 100:.1f}% "
          f"(порог {TONE_MIN_ACC * 100:.0f}%)")
    if acc < TONE_MIN_ACC:
        fail("точность ниже порога")
    print("TONE_MODEL_OK")


# ── страницы ─────────────────────────────────────────────────────────────
def zhk_pages(out: str = OUT) -> list[str]:
    """Страница ЖК — out/<город>/<жк>/index.html (глубина 2)."""
    return sorted(p for p in glob.glob(f"{out}/*/*/index.html")
                  if not p.startswith(f"{out}/static/"))


def read(p: str) -> str:
    return open(p, encoding="utf-8").read()


def dd_after(html_text: str, dt: str) -> str | None:
    m = re.search(rf"<dt>{dt}</dt><dd>(.*?)</dd>", html_text, re.S)
    return re.sub(r"<[^>]+>", " ", m.group(1)).strip() if m else None


STREET_WORDS = re.compile(r"\b(ул|улица|проспект|пр-кт|пр|шоссе|ш|набережная|наб|переулок|пер|бульвар|б-р|"
                          r"проезд|дорога|линия|площадь|пл|аллея|тракт|тупик|квартал|кв-л|микрорайон|мкр)\b"
                          r"|\bд\.?\s*\d", re.I)


def check_address() -> None:
    pages = zhk_pages()
    if len(pages) < 500:                   # страховка от сломанной сборки, а не план
        fail(f"страниц ЖК всего {len(pages)}")
    bad = []
    for p in pages:
        m = re.search(r'<p class="lead">(.*?)</p>', read(p), re.S)
        addr = (m.group(1).strip() if m else "")
        if not addr or not STREET_WORDS.search(addr):
            bad.append((p, addr))
    if bad:
        fail(f"без адреса до улицы: {len(bad)}, напр. {bad[:3]}")
    print(f"страниц ЖК: {len(pages)}, у всех адрес с улицей")
    print("ADDRESS_OK")


def check_fact(dt: str, placeholder: str, token: str) -> None:
    pages = zhk_pages()
    empty, placeholders = [], 0
    for p in pages:
        v = dd_after(read(p), dt)
        if not v:
            empty.append(p)
        elif v.startswith(placeholder):
            placeholders += 1
    if empty:
        fail(f"нет поля «{dt}»: {len(empty)}, напр. {empty[:3]}")
    print(f"«{dt}»: указано у {len(pages) - placeholders}, пометка «{placeholder}» у {placeholders}")
    print(token)


def check_pages() -> None:
    d = sqlite3.connect(DB)
    pages = zhk_pages()
    must = ["<h1 data-zhk=", 'class="facts"', "Жалобы и похвала по темам", 'class="legend"', "На что жалуются",
            "За что хвалят", "Чаты ЖК"]
    missing = [(p, m) for p in pages for m in must if m not in read(p)]
    if missing:
        fail(f"нет обязательных блоков: {len(missing)}, напр. {missing[:3]}")
    for f in ("index.html", "metodika/index.html", "404.html", "sitemap.xml", "robots.txt", "search.json"):
        if not os.path.exists(f"{OUT}/{f}"):
            fail(f"нет {f}")
    cities = {r[0] for r in d.execute("SELECT DISTINCT city_slug FROM zhk WHERE active = 1")}
    page_cities = {p.split("/")[-3] for p in pages}
    no_city_page = [c for c in page_cities if not os.path.exists(f"{OUT}/{c}/index.html")]
    if no_city_page:
        fail(f"нет страниц городов: {no_city_page[:5]}")
    search = json.load(open(f"{OUT}/search.json", encoding="utf-8"))
    if len(search) != len(pages):
        fail(f"в поиске {len(search)} ЖК, страниц {len(pages)}")
    print(f"страниц ЖК {len(pages)}, городов {len(page_cities)} (из {len(cities)} в базе), главная, методика, 404 — на месте")
    print("PAGES_OK")


# ── приватность ──────────────────────────────────────────────────────────
NICK = re.compile(r"(?<![\w/.])@(?!context\b|type\b|media\b|id\b)[A-Za-z][A-Za-z0-9_]{3,}")
PHONE = re.compile(r"(?<!\d)(?:\+7|8)[\s\-(]*9\d{2}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")


def norm(t: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", t.lower()).split())


def privacy_scan(out: str) -> dict:
    snippets = []
    for line in open(f"{ROOT}/data/gold/sample.jsonl", encoding="utf-8"):
        t = norm(json.loads(line)["text"])
        if len(t) >= 60:
            mid = len(t) // 2
            snippets.append(t[mid - 20:mid + 20])
    found = {"nick": [], "phone": [], "quote": []}
    files = glob.glob(f"{out}/**/*.html", recursive=True) + [f"{out}/search.json"]
    for p in files:
        if not os.path.exists(p):
            continue
        raw = read(p)
        text = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        for m in NICK.finditer(text):
            found["nick"].append((p, m.group(0)))
        for m in PHONE.finditer(text):
            found["phone"].append((p, m.group(0)))
        nt = norm(text)
        for sn in snippets:
            if sn in nt:
                found["quote"].append((p, sn))
    return found


def check_privacy() -> None:
    import shutil
    import tempfile
    real = privacy_scan(OUT)
    tmp = tempfile.mkdtemp(prefix="privacy-plant-")
    try:
        page = zhk_pages()[0]
        dst = os.path.join(tmp, "x", "y")
        os.makedirs(dst)
        quote = json.loads(next(l for l in open(f"{ROOT}/data/gold/sample.jsonl", encoding="utf-8")
                                if len(json.loads(l)["text"]) > 120))["text"]
        planted = read(page).replace("</main>", f"<p>Пишите @ivan_petrov_77, +7 (916) 123-45-67. {quote}</p></main>")
        open(os.path.join(dst, "index.html"), "w", encoding="utf-8").write(planted)
        caught = privacy_scan(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not all(caught[k] for k in ("nick", "phone", "quote")):
        fail(f"проверка не ловит подброшенный образец: {({k: len(v) for k, v in caught.items()})}")
    if any(real.values()):
        fail(f"в сайте найдено: { {k: v[:3] for k, v in real.items() if v} }")
    print(f"подброшенный образец пойман (ник, телефон, цитата); в собранном сайте — чисто")
    print("PRIVACY_OK")


def check_sitemap() -> None:
    cfg = json.load(open(f"{ROOT}/data/config.json", encoding="utf-8"))
    base = cfg["base_url"].rstrip("/")
    sm = read(f"{OUT}/sitemap.xml")
    loc_list = re.findall(r"<loc>(.*?)</loc>", sm)
    locs = set(loc_list)
    if len(loc_list) != len(locs):
        dup = [l for l in locs if loc_list.count(l) > 1]
        fail(f"в sitemap повторы: {dup[:5]} — два раздела пишут одну страницу")
    want = {base + "/", base + "/metodika/"}
    want |= {base + "/" + p[len(OUT) + 1:-len("index.html")] for p in zhk_pages()}
    want |= {base + "/" + os.path.basename(os.path.dirname(p)) + "/"
             for p in glob.glob(f"{OUT}/*/index.html") if not p.startswith(f"{OUT}/metodika")}
    miss, extra = want - locs, locs - want
    if miss or extra:
        fail(f"sitemap: нет {len(miss)} ({sorted(miss)[:3]}), лишних {len(extra)} ({sorted(extra)[:3]})")
    robots = read(f"{OUT}/robots.txt")
    if f"Sitemap: {base}/sitemap.xml" not in robots:
        fail("robots.txt не ссылается на sitemap")
    print(f"sitemap: {len(locs)} адресов = все страницы; robots.txt ссылается")
    print("SITEMAP_OK")


# ── данные ───────────────────────────────────────────────────────────────
def skipped(d: sqlite3.Connection) -> str:
    """Чаты, которые по решению Давида не считаются чатами ЖК (их проверяет G19)."""
    if not d.execute("SELECT 1 FROM sqlite_master WHERE name = 'chat_skip'").fetchone():
        return ""
    ids = [r[0] for r in d.execute("SELECT source_id FROM chat_skip")]
    return f" AND s.id NOT IN ({','.join(map(str, ids))})" if ids else ""


def check_dataset() -> None:
    d = sqlite3.connect(DB)
    s = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True, timeout=60)
    sel = d.execute("SELECT MAX(selected_at) FROM zhk").fetchone()[0]
    cnt = dict(s.execute(
        "SELECT s.zhk_id, COUNT(*) FROM message m JOIN source s ON s.id = m.source_id"
        " WHERE s.is_zhk = 1 AND s.zhk_id IS NOT NULL AND m.dup_of IS NULL AND m.text NOT LIKE ?"
        "   AND m.posted_at >= datetime(?, '-30 days') AND m.fetched_at <= ?" + skipped(d) +
        " GROUP BY s.zhk_id",
        (SPAM, sel, sel)).fetchall())            # как было на момент отбора: докачанное позже не в счёт
    known = {r[0] for r in s.execute("SELECT id FROM zhk")}
    qual = {z for z, n in cnt.items() if n >= 30 and z in known}
    border = {z for z, n in cnt.items() if 27 <= n <= 33}
    cols = {r[1] for r in d.execute("PRAGMA table_info(zhk)")}
    q = "SELECT id FROM zhk WHERE active = 1" + (" OR canon_id IS NOT NULL" if "canon_id" in cols else "")
    act_ids = {r[0] for r in d.execute("SELECT id FROM zhk WHERE active = 1")}
    # Склеенный дубль «наш», только если сам проходит порог: после исключения барахолок
    # (решение 24.09.2026) у старого дубля может не остаться 30 сообщений — страницы у него нет.
    ours = act_ids | ({r[0] for r in d.execute(q)} & (qual | border))
    missing, extra = qual - ours - border, ours - qual - border
    if missing or extra:
        fail(f"расхождение с пересчётом: нет в базе {len(missing)} {sorted(missing)[:5]}, "
             f"лишних {len(extra)} {sorted(extra)[:5]}")
    act = d.execute("SELECT COUNT(*) FROM zhk WHERE active = 1").fetchone()[0]
    print(f"пересчёт на момент отбора {sel}: {len(qual)} ЖК с ≥30 сообщениями; в базе {len(ours)} "
          f"(страниц {act} + склеенных дублей {len(ours) - act}); пограничных 27–33: {len(border)}")
    print("DATASET_OK")


# ── таймер и оповещение ──────────────────────────────────────────────────
def check_timer() -> None:
    def show(unit, *props):
        r = subprocess.run(["systemctl", "show", unit, "-p", ",".join(props)], capture_output=True, text=True)
        return dict(l.split("=", 1) for l in r.stdout.strip().splitlines() if "=" in l)
    t = show("zhk-site-daily.timer", "UnitFileState", "ActiveState")
    if t.get("UnitFileState") != "enabled" or t.get("ActiveState") != "active":
        fail(f"таймер не включён: {t}")
    sv = show("zhk-site-daily.service", "Result", "ExecMainStatus")
    if sv.get("Result") != "success" or sv.get("ExecMainStatus") not in ("0", ""):
        fail(f"последний запуск службы неудачный: {sv}")
    last = json.load(open(f"{ROOT}/data/daily/last_ok.json", encoding="utf-8"))
    age = datetime.now(timezone.utc) - datetime.strptime(last["finished_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    if age > timedelta(hours=26):
        fail(f"последняя удачная пересборка {last['finished_at']} — {age} назад")
    print(f"таймер включён; последняя удачная пересборка {last['finished_at']} UTC за {last['minutes']} мин")
    print("TIMER_OK")


def check_fail_alert() -> None:
    r = subprocess.run(["/usr/bin/python3", f"{ROOT}/tools/daily.py", "--fail-test"], capture_output=True, text=True)
    out = r.stdout + r.stderr
    if r.returncode == 0:
        fail("нарочный сбой не дал ненулевой код выхода")
    if "ALERT_SENT" not in out:
        fail(f"оповещение не ушло: {out.strip()[-300:]}")
    print("нарочный сбой: код выхода ненулевой, сообщение в Telegram доставлено")
    print("FAIL_ALERT_OK")


def check_deploy() -> None:
    import urllib.request
    cfg = json.load(open(f"{ROOT}/data/config.json", encoding="utf-8"))
    base = cfg["base_url"].rstrip("/")
    if not base.startswith("https://"):
        fail(f"base_url не https: {base}")
    pages = zhk_pages()
    rnd = random.Random(datetime.now().strftime("%Y%m%d%H"))
    paths = ["/"] + ["/" + p[len(OUT) + 1:-len("index.html")] for p in rnd.sample(pages, 3)]
    for path in paths:
        local = read(f"{OUT}{path}index.html")
        title = re.search(r"<title>(.*?)</title>", local, re.S).group(1)
        req = urllib.request.Request(base + path, headers={"User-Agent": "tilibom-check"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            if resp.status != 200 or title not in body:
                fail(f"{path}: HTTP {resp.status}, заголовок {'есть' if title in body else 'не совпал'}")
        print(f"  {base + path} — 200, заголовок совпал")
    print("DEPLOY_OK")


# ── тон на страницах ─────────────────────────────────────────────────────
def page_table(html_text: str) -> dict[str, list[int]]:
    """Таблица «Жалобы и похвала по темам»: тема → [жалобы, похвала, нейтрально, всего]."""
    sec = html_text.split("Жалобы и похвала по темам", 1)[1].split("</table>", 1)[0]
    rows = {}
    for tr in re.findall(r"<tr><td>(.*?)</td>(.*?)</tr>", sec, re.S):
        nums = [int(x) for x in re.findall(r'<td class="num">(\d+)</td>', tr[1])]
        if len(nums) == 4:
            rows[tr[0]] = nums
    return rows


def check_tone_pages() -> None:
    from zhk_data import TOPICS
    pages = zhk_pages()
    bad = []
    for p in pages:
        h = read(p)
        rows = page_table(h)
        if len(rows) != len(TOPICS):
            bad.append((p, f"тем в таблице {len(rows)}"))
            continue
        for label, (neg, pos, neu, tot) in rows.items():
            if neg + pos + neu != tot:
                bad.append((p, f"{label}: {neg}+{pos}+{neu} ≠ {tot}"))
    if bad:
        fail(f"таблицы тона не сходятся: {len(bad)}, напр. {bad[:3]}")
    # Независимый пересчёт для пяти случайных ЖК: всего по теме = сообщения с темой
    # без рекламы (метка A), по sources.db и таблице tone, за те же 30 дней.
    d = sqlite3.connect(DB)
    s = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True, timeout=60)
    tone = dict(d.execute("SELECT message_id, label FROM tone"))
    label_of = {k: v for k, v, _ in TOPICS}
    rx = {k: re.compile(p, re.I) for k, _, p in TOPICS}
    cols = {r[1] for r in d.execute("PRAGMA table_info(zhk)")}
    rnd = random.Random(7)
    checked, ads_seen = 0, 0
    built = datetime.fromtimestamp(os.path.getmtime(f"{OUT}/index.html"), timezone.utc)
    zhk_done = sum_shown = sum_no_ad = sum_all = 0
    for p in rnd.sample(pages, 30):
        if zhk_done >= 5 and ads_seen >= 20:
            break
        zhk_done += 1
        path = "/" + p[len(OUT) + 1:-len("index.html")]
        m = re.search(r'<h1 data-zhk="(\d+)"', read(p))
        if not m:
            fail(f"{path}: нет номера ЖК на странице")
        zid = int(m.group(1))
        ids = [zid] + ([r[0] for r in d.execute("SELECT id FROM zhk WHERE canon_id = ?", (zid,))]
                       if "canon_id" in cols else [])
        ph = ",".join("?" * len(ids))
        since = (built - timedelta(days=30)).date().isoformat()
        rows = s.execute(f"SELECT m.id, m.text FROM message m JOIN source s ON s.id = m.source_id"
                         f" WHERE s.is_zhk = 1 AND s.zhk_id IN ({ph}) AND m.dup_of IS NULL AND m.text NOT LIKE ?"
                         f"   AND m.posted_at >= ? AND m.posted_at < ?" + skipped(d),
                         (*ids, SPAM, since, built.strftime("%Y-%m-%dT%H:%M:%S"))).fetchall()
        table = page_table(read(p))
        for key in ("voda", "uk", "lift", "parking"):
            hit = [mid for mid, t in rows if rx[key].search((t or "").lower())]
            no_ad = sum(1 for mid in hit if tone.get(mid) != "A")
            ads = len(hit) - no_ad
            ads_seen += ads
            shown = table[label_of[key]][3]
            sum_shown, sum_no_ad, sum_all = sum_shown + shown, sum_no_ad + no_ad, sum_all + len(hit)
            if abs(shown - no_ad) > max(3, 0.05 * no_ad):
                fail(f"{path} «{label_of[key]}»: на странице {shown}, пересчёт без рекламы {no_ad} (реклама {ads})")
            checked += 1
    if checked < 20:
        fail(f"пересчитано всего {checked} тем")
    if ads_seen < 20:
        fail(f"в пересчёте всего {ads_seen} рекламных сообщений — исключение рекламы не проверено")
    # Сумма по всем пересчитанным темам: цифры сайта должны быть ближе к счёту без рекламы,
    # чем к счёту с рекламой, — иначе реклама в цифрах.
    if abs(sum_shown - sum_no_ad) >= abs(sum_shown - sum_all):
        fail(f"сумма на страницах {sum_shown}: без рекламы {sum_no_ad}, с рекламой {sum_all} — реклама не исключена")
    print(f"страниц ЖК {len(pages)}: у каждой темы жалобы, похвала, нейтрально, сумма сходится; "
          f"пересчёт {checked} тем у {zhk_done} случайных ЖК совпал: на страницах {sum_shown}, "
          f"без рекламы {sum_no_ad}, с рекламой было бы {sum_all}")
    print("TONE_PAGES_OK")


def check_bot() -> None:
    """G12: служба бота жива, Telegram узнаёт бота, кнопка на странице ведёт к нему,
    и на ЖК со страницы бот отдаёт все его чаты из базы."""
    import urllib.request
    import bot
    cfg = json.load(open(f"{ROOT}/data/config.json", encoding="utf-8"))
    name = cfg.get("bot_username")
    if not name:
        fail("в config.json нет bot_username")
    st = subprocess.run(["systemctl", "is-active", "zhk-bot.service"], capture_output=True, text=True).stdout.strip()
    if st != "active":
        fail(f"служба zhk-bot не работает: {st}")
    bot.TOKEN = bot.token()
    me = bot.api("getMe")["result"]["username"]
    if me != name:
        fail(f"токен от @{me}, а на сайте @{name}")
    d = sqlite3.connect(DB)
    rnd = random.Random(datetime.now().strftime("%Y%m%d%H"))
    for p in rnd.sample(zhk_pages(), 5):
        h = read(p)
        m = re.search(rf'href="https://t.me/{name}\?start=z(\d+)"', h)
        if not m:
            fail(f"{p}: нет кнопки бота")
        zid = int(m.group(1))
        ans = bot.zhk_answer(zid) or ""
        links = [r[0] for r in d.execute("SELECT link FROM chat WHERE zhk_id = ? AND link IS NOT NULL", (zid,))]
        miss = [l for l in links[:bot.MAX_CHATS] if l not in ans]
        if not ans or miss:
            fail(f"ЖК {zid}: бот не отдал {len(miss)} из {len(links)} чатов")
        print(f"  ЖК {zid}: кнопка на странице, бот отдаёт {len(links)} чатов")
    print(f"@{name} работает (служба active)")
    print("BOT_OK")


# ── сортировка по жалобам ────────────────────────────────────────────────
def check_sort() -> None:
    """G16: у каждой строки таблицы города доли жалоб совпадают с базой (все темы — сумма),
    переключатель есть там, где сравнивать есть с чем, rating.json — только крупные ЖК."""
    from build_site import SORT_KEYS
    from zhk_data import RANK_MIN_MSGS, TOPICS
    d = sqlite3.connect(DB)
    by_url = {r[0]: r[1] for r in d.execute("SELECT path, zhk_id FROM url")}
    for p in zhk_pages():                       # новые страницы ещё не закреплены — берём номер со страницы
        m = re.search(r'<h1 data-zhk="(\d+)"', read(p))
        if m:
            by_url["/" + p[len(OUT) + 1:-len("index.html")]] = int(m.group(1))
    msgs = {r[0]: r[1] for r in d.execute("SELECT id, msgs_30d FROM zhk")}
    neg = defaultdict(dict)
    for zid, t, v in d.execute("SELECT zhk_id, topic, neg_per_1000 FROM topic"):
        neg[zid][t] = v or 0
    row_re = re.compile(r'<tr data-m="(\d+)" data-v="([^"]+)"( data-small)?><td><a href="([^"]+)"')
    n_rows = n_cities = n_sorter = 0
    for f in sorted(glob.glob(f"{OUT}/*/index.html")):
        h = read(f)
        if 'id="zhk-table"' not in h:
            continue
        n_cities += 1
        keys = re.search(r'id="zhk-table" data-keys="([^"]+)"', h).group(1).split(",")
        if keys != SORT_KEYS:
            fail(f"{f}: ключи {keys}")
        rows = row_re.findall(h)
        if len(rows) != h.count("<tr data-m="):
            fail(f"{f}: не все строки разобраны")
        big = 0
        for m, v, small, url in rows:
            zid = by_url.get(url)
            if zid is None:
                fail(f"{f}: {url} нет в таблице адресов")
            if int(m) != msgs[zid] or bool(small) != (msgs[zid] < RANK_MIN_MSGS):
                fail(f"{url}: сообщений {m}, в базе {msgs[zid]}")
            per = [neg[zid].get(k, 0) for k, _, _ in TOPICS]
            want = [sum(per)] + per
            got = [float(x) for x in v.split(",")]
            if len(got) != len(want) or any(abs(a - b) > 0.06 for a, b in zip(got, want)):
                fail(f"{url}: доли {got} ≠ база {want}")
            big += not small
            n_rows += 1
        has = "data-sorter" in h
        if has != (big >= 2):
            fail(f"{f}: крупных ЖК {big}, переключатель {'есть' if has else 'нет'}")
        n_sorter += has
    r = json.load(open(f"{OUT}/rating.json", encoding="utf-8"))
    cnt = defaultdict(int)
    for p, zid in by_url.items():
        if os.path.exists(f"{OUT}{p}index.html") and msgs[zid] >= RANK_MIN_MSGS:
            cnt[p.split("/")[1]] += 1
    want_cities = {c for c, n in cnt.items() if n >= 5}
    got_cities = {c["s"] for c in r["cities"]}
    if want_cities != got_cities:
        fail(f"города виджета: лишние {got_cities - want_cities}, нет {want_cities - got_cities}")
    for c in r["cities"]:
        if len(c["z"]) != cnt[c["s"]] or any(z[3] < RANK_MIN_MSGS for z in c["z"]):
            fail(f"виджет {c['s']}: {len(c['z'])} ЖК, ожидалось {cnt[c['s']]}")
    idx = read(f"{OUT}/index.html")
    if 'id="r-list"' not in idx or idx.count("<li><a href=") < r["top"]:
        fail("на главной нет виджета с первыми ЖК")
    print(f"городов {n_cities}, с переключателем {n_sorter}, строк {n_rows} — доли совпадают с базой; "
          f"виджет: {len(got_cities)} городов")
    print("SORT_OK")


# ── привязка к городу ────────────────────────────────────────────────────
# Независимые от zhk_data.py правила (решения Давида 24.09.2026): свои регулярки,
# свой пересчёт по sources.db, справочник населённых пунктов и сырой ответ геокодера.
UA_RX = re.compile(r"[іїєґІЇЄҐ]")
SKIP_RX = re.compile(r"барахолк|купля[\s\-–]*продаж|отдам\s+даром|приму\s+в\s+дар|маркет|market|знакомств", re.I)
KYIV = ("Софія", "Svitlo Park", "Бульвар Фонтанів", "Метрополіс", "LIKO GRAD")


def published() -> dict[int, dict]:
    out = {}
    for p in zhk_pages():
        h = read(p)
        m = re.search(r'<h1 data-zhk="(\d+)"[^>]*>(.*?)</h1>', h, re.S)
        if not m:
            fail(f"{p}: нет номера ЖК")
        crumbs = re.search(r'<div class="crumbs">.*?<a href="/([^/]+)/">([^<]+)</a>', h, re.S)
        lead = re.search(r'<p class="lead">(.*?)</p>', h, re.S)
        out[int(m.group(1))] = {"path": "/" + p[len(OUT) + 1:-len("index.html")], "h1": html_unescape(m.group(2)),
                                "city_slug": crumbs.group(1), "city": html_unescape(crumbs.group(2)),
                                "address": html_unescape(lead.group(1)) if lead else ""}
    return out


def html_unescape(t: str) -> str:
    import html
    return html.unescape(re.sub(r"<[^>]+>", "", t)).strip()


def check_chats() -> None:
    d = sqlite3.connect(DB)
    s = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True, timeout=60)
    pub = published()
    for zid, z in pub.items():
        if any(k.lower() in z["h1"].lower() for k in KYIV):
            fail(f"{z['path']}: киевский ЖК «{z['h1']}» опубликован")
    canon = defaultdict(list)
    for a, c in d.execute("SELECT id, canon_id FROM zhk WHERE canon_id IS NOT NULL"):
        canon[c].append(a)
    sel = d.execute("SELECT MAX(selected_at) FROM zhk").fetchone()[0]
    stat = defaultdict(lambda: [0, 0])
    owner = {}
    for sid, zid, title in s.execute("SELECT id, zhk_id, title FROM source WHERE is_zhk = 1 AND zhk_id IS NOT NULL"):
        owner[sid] = (zid, title or "")
    for sid, text in s.execute(
            "SELECT m.source_id, m.text FROM message m JOIN source s ON s.id = m.source_id"
            " WHERE s.is_zhk = 1 AND s.zhk_id IS NOT NULL AND m.dup_of IS NULL AND m.text NOT LIKE ?"
            "   AND m.posted_at >= datetime(?, '-30 days') AND m.fetched_at <= ?", (SPAM, sel, sel)):
        c = stat[sid]
        c[0] += 1
        c[1] += bool(UA_RX.search(text or ""))
    bad = {sid for sid, (n, ua) in stat.items() if n >= 20 and ua / n >= 0.2}
    bad |= {sid for sid, (_, t) in owner.items() if SKIP_RX.search(t)}
    listed = {(zid, sid) for zid, sid in d.execute("SELECT zhk_id, source_id FROM chat")}
    msgs = dict(d.execute("SELECT id, msgs_30d FROM zhk"))
    n_bad_pub = n_checked = 0
    for zid, z in pub.items():
        ids = {zid, *canon.get(zid, [])}
        mine = [sid for sid, (o, _) in owner.items() if o in ids]
        for sid in mine:
            if sid in bad:
                n_bad_pub += 1
                if (zid, sid) in listed:
                    fail(f"{z['path']}: в списке чатов исключённый «{owner[sid][1]}»")
        # Точного равенства не ждём: LeadHunter помечает повторы уже после отбора.
        # Проверяем суть — число на странице ближе к пересчёту без исключённых чатов,
        # чем к пересчёту с ними.
        excl = sum(stat[sid][0] for sid in mine if sid in bad)
        if excl:
            want = sum(stat[sid][0] for sid in mine if sid not in bad)
            if abs(msgs[zid] - want) >= abs(msgs[zid] - (want + excl)):
                fail(f"{z['path']}: сообщений {msgs[zid]} — ближе к пересчёту с исключёнными чатами "
                     f"({want + excl}), чем без них ({want})")
            n_checked += 1
    ua_pub = sum(1 for zid in pub for sid, (o, _) in owner.items()
                 if o in {zid, *canon.get(zid, [])} and sid in bad and stat[sid][0] >= 20
                 and stat[sid][1] / stat[sid][0] >= 0.2)
    print(f"ЖК с исключёнными чатами {n_checked}: число сообщений на странице — без них; исключённых чатов "
          f"у опубликованных ЖК {n_bad_pub} (из них украинских {ua_pub}) — в цифры и списки не входят; "
          f"киевских ЖК на сайте нет")
    print("CHATS_OK")


def check_geo() -> None:
    sys.path.insert(0, f"{ROOT}/tools")
    from zhk_data import haversine, reg_key
    d = sqlite3.connect(DB)
    s = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True, timeout=60)
    fed = {"Москва": "москва", "Санкт-Петербург": "санкт-пе", "Севастополь": "севастоп"}
    towns = defaultdict(list)
    for name, typ, region, lat, lon, pop, src in s.execute(
            "SELECT name, type, region, geo_lat, geo_lon, population, source FROM settlement WHERE region IS NOT NULL"):
        typ = "г" if typ is None and src == "osm" else typ      # города из OSM (Москва, Химки) — без типа
        towns[name.lower().replace("ё", "е")].append((typ, reg_key(region), lat, lon, pop or 0))
    rev = {(round(a, 6), round(b, 6)): (c, st) for a, b, c, st in d.execute("SELECT lat, lon, country, state FROM rev")}
    place = {r[0]: r[1:] for r in d.execute("SELECT zhk_id, lat, lon, how FROM place")}
    pub = published()
    far = []
    for zid, z in pub.items():
        city = z["city"]
        if zid not in place:
            fail(f"{z['path']}: нет сверки места")
        lat, lon, how = place[zid]
        country, state = rev.get((round(lat, 6), round(lon, 6)), (None, None))
        if state is None:
            fail(f"{z['path']}: нет ответа геокодера для координат {lat},{lon}")
        rk = reg_key(state)
        if country != "ru" and rk not in ("крым", "севастоп", "донецкая", "луганска", "запорожс", "херсонск"):
            fail(f"{z['path']}: координаты вне России ({country}, {state})")
        if city in fed:
            ck, center = fed[city], None
        else:
            cand = [t for t in towns.get(city.lower().replace("ё", "е"), []) if t[2] is not None]
            if not cand:
                fail(f"{z['path']}: «{city}» — не населённый пункт России")
            match = [t for t in cand if t[1] == rk]
            named = city.lower() in [x.strip().lower() for x in z["address"].split(",")]
            if not match and named and any(frozenset((t[1], rk)) in (frozenset(("москва", "московск")),
                                                                     frozenset(("санкт-пе", "ленингра"))) for t in cand):
                match = cand                      # граница столицы и области: адрес дома называет город
            if not match:
                fail(f"{z['path']}: регион по координатам «{state}», а город «{city}» — в {[t[1] for t in cand]}")
            best = max(match, key=lambda t: (t[0] == "г", t[4]))
            ck, center = best[1], (best[2], best[3])
        neighbours = {frozenset(("москва", "московск")), frozenset(("санкт-пе", "ленингра"))}
        named = city.lower() in [x.strip().lower() for x in z["address"].split(",")]
        if rk != ck and not (frozenset((rk, ck)) in neighbours and named):
            # На границе столицы и области верим адресу дома, если он называет город страницы.
            fail(f"{z['path']}: регион по координатам «{state}» ≠ регион города «{city}»")
        if center:
            dist = haversine(lat, lon, *center)
            if dist > 30:
                far.append((z["path"], round(dist)))
        # В показанном адресе не должно быть города из другого региона.
        for part in (x.strip() for x in z["address"].split(",")):
            part = re.sub(r"^(город|г)\.?\s+", "", part).lower().replace("ё", "е")
            ts = [t for t in towns.get(part, []) if t[0] == "г" and (t[4] >= 10000 or not t[4])
                  and t[2] is not None and haversine(lat, lon, t[2], t[3]) <= 50]      # тёзки далеко — не город
            if ts and all(t[1] != ck for t in ts) and part not in (city.lower(),):
                fail(f"{z['path']}: в адресе «{z['address']}» город другого региона «{part}»")
    if far:
        fail(f"дальше 30 км от центра города: {len(far)}, напр. {far[:5]}")
    for slug in ("minsk", "semey", "spb", "naberezhnyh", "komi", "krym", "moskovskaya-oblast",
                 "leningradskaya-oblast", "sverdlovskaya", "krasnodarskiy", "bashkortostan"):
        if os.path.exists(f"{OUT}/{slug}/index.html"):
            fail(f"есть страница города /{slug}/")
    for zid, z in pub.items():
        for bad_place in ("Новая Адыгея", "Ханты-Мансийск", "Азьмушкино"):
            if bad_place in z["address"] and z["city"] not in (bad_place,):
                fail(f"{z['path']}: адрес «{z['address']}» на странице города {z['city']}")
    hows = Counter(v[2] for k, v in place.items() if k in pub)
    print(f"страниц {len(pub)}: город — населённый пункт России, регион по координатам совпадает, "
          f"вне столиц ≤30 км от центра, в адресах нет городов других регионов; как решено: "
          + ", ".join(f"{k} {v}" for k, v in hows.most_common()))
    print("GEO_OK")


def check_moved() -> None:
    import http.client
    cfg = json.load(open(f"{ROOT}/data/config.json", encoding="utf-8"))
    host = cfg["base_url"].split("//", 1)[1].rstrip("/")
    d = sqlite3.connect(DB)
    moved = d.execute("SELECT old_path, new_path FROM moved").fetchall()
    urls = {p for (p,) in d.execute("SELECT path FROM url")}
    pub_paths = {z["path"] for z in published().values()}
    removed = sorted(p for p in urls if p not in pub_paths)

    def get(path: str) -> tuple[int, str | None]:
        c = http.client.HTTPSConnection(host, timeout=30)
        c.request("GET", path, headers={"User-Agent": "tilibom-check"})
        r = c.getresponse()
        r.read()
        return r.status, r.getheader("Location")

    for old, new in moved:
        if new not in urls:
            fail(f"{new} не закреплён")
        st, loc = get(old)
        if st != 301 or not (loc or "").endswith(new):
            fail(f"{old}: HTTP {st} → {loc}, ожидалось 301 → {new}")
        st, _ = get(new)
        if st != 200:
            fail(f"{new}: HTTP {st}")
    for p in removed:
        st, loc = get(p)
        if st not in (404, 301):
            fail(f"снятая {p}: HTTP {st}")
    sent_path = f"{ROOT}/data/indexnow_last.json"
    if not os.path.exists(sent_path):
        fail("IndexNow ещё не отправлялся после исправлений")
    sent = json.load(open(sent_path, encoding="utf-8"))
    sent_paths = {u.split(host, 1)[1] for u in sent["urls"]}
    miss = [p for o, n in moved for p in (o, n) if p not in sent_paths] + [p for p in removed if p not in sent_paths]
    if miss:
        fail(f"в IndexNow не ушло {len(miss)}: {miss[:5]}")
    if not any(str(r).startswith("200") or str(r).startswith("202") for r in sent["results"].values()):
        fail(f"IndexNow не принял: {sent['results']}")
    print(f"переехало {len(moved)}: старый адрес → 301 → новый (200, закреплён); снято {len(removed)} — 404 или 301; "
          f"IndexNow {sent['sent_at']}: {sent['results']}")
    print("MOVED_OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("check")
    ap.add_argument("--labels", default="/opt/zhk-site/data/gold/labels.json")
    a = ap.parse_args()
    run = {"gold": lambda: check_gold(a.labels), "tone-model": check_tone_model,
           "dataset": check_dataset, "address": check_address,
           "developer": lambda: check_fact("Застройщик", "застройщик не найден в ЕРЗ", "DEVELOPER_OK"),
           "uk": lambda: check_fact("Управляющая компания", "УК не указана в реестрах", "UK_OK"),
           "pages": check_pages, "privacy": check_privacy, "sitemap": check_sitemap,
           "timer": check_timer, "fail-alert": check_fail_alert, "deploy": check_deploy,
           "tone-pages": check_tone_pages, "bot": check_bot, "sort": check_sort,
           "chats": check_chats, "geo": check_geo, "moved": check_moved}
    if a.check not in run:
        fail(f"нет проверки «{a.check}»")
    run[a.check]()
