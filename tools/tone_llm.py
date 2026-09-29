#!/usr/bin/env python3
"""Тональность сообщений о темах ЖК через DeepSeek: N жалоба, P похвала, U нейтрально, A реклама.

Размечает сообщения с темами (TOPICS) из чатов опубликованных ЖК за последние N дней,
пачками по 100 в одном запросе, в несколько потоков. Результат — таблица tone в
data/site.db; повторный запуск доразмечает только новое.

    tone_llm.py                 # 30 дней, потолок $4
    tone_llm.py --days 2        # ежедневная доразметка
    tone_llm.py --dry-run       # сколько сообщений и сколько это будет стоить
    tone_llm.py --prompt v2 --gold-check 2026-09-11 2026-09-16
                                # сверить v2 с ручным эталоном за эти дни (в базу не пишет)

Сверка на ручном эталоне 120 сообщений (23.09.2026): 86% верных меток, жалобы 78%,
реклама 100% — см. Чертоги, zhk-tonalnost-svereka-23-09.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

sys.path.insert(0, "/opt/zhk-site/tools")
sys.path.insert(0, "/opt/leadhunter/tools")
from zhk_data import DB, SPAM, SRC, TOPICS, Progress, tg  # noqa: E402

API = "https://api.deepseek.com/chat/completions"
PRICE_IN, PRICE_OUT = 0.27, 1.10          # $ за 1M токенов, как в lh_classify
BATCH = 100
PROMPT = (
    "Ты размечаешь сообщения из чатов жильцов жилых комплексов для покупателей квартир. "
    "Для каждого сообщения верни ОДНУ метку:\n"
    "N — жалоба или сообщение о проблеме в доме, ЖК, с УК, застройщиком, соседями, инфраструктурой "
    "(в том числе вопросы вида «у всех нет горячей воды?», «починили ли лифт?»);\n"
    "P — похвала, благодарность, проблему решили, что-то сделали хорошо;\n"
    "U — нейтрально: вопрос или совет без проблемы, информация, объявление о плановых работах, "
    "обсуждение не про жизнь в ЖК;\n"
    "A — реклама, вакансия, продажа, аренда, услуги, отдам.\n"
    "Ответ строго JSON-объект {\"номер\": \"метка\"} без пояснений.\n\n")


GOLD_DIR = "/opt/zhk-site/data/gold"
SHOTS_BEFORE = "2026-09-11"     # примеры для v2 — только из дней до валидации и проверки
SHOTS = {"N": 14, "U": 14, "A": 8, "P": 8}


def prompt_v2() -> str:
    """Правила из GUIDE.md целиком и примеры из ручного эталона (стратифицированно,
    только дни до SHOTS_BEFORE — валидация и проверка в подсказку не попадают)."""
    import random
    guide = open(f"{GOLD_DIR}/GUIDE.md", encoding="utf-8").read()
    guide = guide[guide.index("Метка ставится"):]
    lab = {int(k): v for k, v in json.load(open(f"{GOLD_DIR}/labels.json", encoding="utf-8")).items()}
    xs = [json.loads(l) for l in open(f"{GOLD_DIR}/sample.jsonl", encoding="utf-8")]
    xs = [x for x in xs if x["posted_at"] < SHOTS_BEFORE and len(x["text"]) <= 300]
    rnd = random.Random(7)
    shots = []
    for c, k in SHOTS.items():
        pool = [x for x in xs if lab[x["n"]] == c]
        shots += [(x, c) for x in rnd.sample(pool, min(k, len(pool)))]
    rnd.shuffle(shots)
    ex = "\n".join(f"— {re.sub(r'\s+', ' ', x['text'])} → {c}" for x, c in shots)
    return ("Ты размечаешь сообщения из чатов жильцов жилых комплексов. Ставь ОДНУ метку N, P, U или A "
            "по правилам ниже.\n\n" + guide + "\n\nПримеры разметки:\n" + ex +
            "\n\nОтвет строго JSON-объект {\"номер\": \"метка\"} без пояснений.\n\n")


def key() -> str:
    from lh_classify import get_key
    k = get_key("deepseek")
    if not k:
        sys.exit("нет ключа DeepSeek")
    return k


def label_batch(k: str, texts: list[str], prompt: str = PROMPT) -> tuple[list[str | None], int, int]:
    body = prompt + "\n".join(f"{i}: {re.sub(r'\s+', ' ', t)[:400]}" for i, t in enumerate(texts))
    for attempt in range(4):
        try:
            r = requests.post(API, headers={"Authorization": f"Bearer {k}"}, timeout=180,
                              json={"model": "deepseek-chat", "temperature": 0,
                                    "response_format": {"type": "json_object"},
                                    "messages": [{"role": "user", "content": body}]})
            r.raise_for_status()
            d = r.json()
            out = json.loads(d["choices"][0]["message"]["content"])
            u = d.get("usage", {})
            labels = [out.get(str(i)) for i in range(len(texts))]
            labels = [x if x in ("N", "P", "U", "A") else None for x in labels]
            return labels, u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
        except Exception:
            time.sleep(5 * (attempt + 1))
    return [None] * len(texts), 0, 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--max-usd", type=float, default=4.0)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--prompt", choices=["v1", "v2"], default="v1")
    p.add_argument("--gold-check", nargs=2, metavar=("FROM", "TO"))
    p.add_argument("--shots", default="", help="N,U,A,P — сколько примеров каждого класса в v2")
    p.add_argument("--pool-file", help="разметить тексты из jsonl (id, text) вместо базы")
    p.add_argument("--out", help="куда писать метки для --pool-file (json id → метка)")
    a = p.parse_args()
    if a.shots:
        SHOTS.update(zip("NUAP", map(int, a.shots.split(","))))
    prompt = prompt_v2() if a.prompt == "v2" else PROMPT
    if a.gold_check:
        gold_check(a, prompt)
        return
    if a.pool_file:
        label_file(a, prompt)
        return

    d = sqlite3.connect(DB, timeout=60)
    d.execute("CREATE TABLE IF NOT EXISTS tone (message_id INTEGER PRIMARY KEY, zhk_id INTEGER,"
              " posted_at TEXT, label TEXT, src TEXT, at TEXT)")
    d.commit()
    try:        # дубли, склеенные с основным ЖК, тоже размечаем: их сообщения идут в его цифры
        ids = {r[0] for r in d.execute("SELECT id FROM zhk WHERE active = 1 OR canon_id IS NOT NULL")}
    except sqlite3.OperationalError:
        ids = {r[0] for r in d.execute("SELECT id FROM zhk WHERE active = 1")}
    done = {r[0] for r in d.execute("SELECT message_id FROM tone WHERE label IS NOT NULL")}
    rx = re.compile("|".join(f"(?:{pat})" for _, _, pat in TOPICS), re.I)
    s = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True, timeout=60)
    todo = [(mid, zid, pa, t) for mid, zid, pa, t in s.execute(
        "SELECT m.id, so.zhk_id, m.posted_at, m.text FROM message m JOIN source so ON so.id = m.source_id"
        " WHERE so.is_zhk = 1 AND so.zhk_id IS NOT NULL AND m.dup_of IS NULL AND m.text NOT LIKE ?"
        f"   AND m.posted_at >= datetime('now','-{int(a.days)} days')", (SPAM,))
        if zid in ids and mid not in done and t and rx.search(t.lower())]
    est_in = sum(min(len(t), 400) for *_, t in todo) / 2.6 + len(todo) / BATCH * 300
    est = est_in / 1e6 * PRICE_IN + len(todo) * 6 / 1e6 * PRICE_OUT
    print(f"к разметке: {len(todo)} сообщений, запросов {len(todo) // BATCH + 1}, оценка ≈ ${est:.2f}")
    if a.dry_run:
        return
    k = key()
    src = "deepseek" if a.prompt == "v1" else "deepseek-v2"
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    prog, spent, n_lab = Progress("тональность DeepSeek", len(batches)), 0.0, 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with ThreadPoolExecutor(a.workers) as ex:
        futs = {}
        it = iter(batches)
        for _ in range(a.workers):
            b = next(it, None)
            if b:
                futs[ex.submit(label_batch, k, [x[3] for x in b], prompt)] = b
        i = 0
        while futs:
            for f in as_completed(list(futs)):
                b = futs.pop(f)
                labels, tin, tout = f.result()
                spent += tin / 1e6 * PRICE_IN + tout / 1e6 * PRICE_OUT
                d.executemany(f"INSERT OR REPLACE INTO tone VALUES (?,?,?,?, '{src}', ?)",
                              [(mid, zid, pa, lab, now) for (mid, zid, pa, _), lab in zip(b, labels) if lab])
                d.commit()
                n_lab += sum(1 for x in labels if x)
                i += 1
                prog.step(i, f"| размечено {n_lab}, потрачено ${spent:.2f}")
                if spent >= a.max_usd:
                    tg(f"🛑 Тили-бом: разметка тональности остановлена на потолке ${a.max_usd:.2f}")
                    print("потолок расходов — стоп")
                    return
                nb = next(it, None)
                if nb:
                    futs[ex.submit(label_batch, k, [x[3] for x in nb], prompt)] = nb
                break
    prog.done(f"размечено {n_lab}, потрачено ${spent:.2f}")
    print(f"готово: размечено {n_lab} из {len(todo)}, ${spent:.2f}")


def label_file(a, prompt: str) -> None:
    """Разметить тексты из jsonl в json-файл (для учителя локальной модели). Продолжает
    с места остановки: уже размеченные id пропускает. В базу сайта не пишет."""
    import os
    done = json.load(open(a.out, encoding="utf-8")) if os.path.exists(a.out) else {}
    xs = [json.loads(l) for l in open(a.pool_file, encoding="utf-8")]
    todo = [x for x in xs if str(x["id"]) not in done]
    print(f"к разметке: {len(todo)} из {len(xs)}")
    k = key()
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    prog, spent = Progress("учитель DeepSeek v2", len(batches)), 0.0
    with ThreadPoolExecutor(a.workers) as ex:
        for i, (b, (labels, tin, tout)) in enumerate(
                zip(batches, ex.map(lambda b: label_batch(k, [x["text"] for x in b], prompt), batches)), 1):
            spent += tin / 1e6 * PRICE_IN + tout / 1e6 * PRICE_OUT
            for x, l in zip(b, labels):
                if l:
                    done[str(x["id"])] = l
            if i % 20 == 0 or i == len(batches):
                json.dump(done, open(a.out, "w"))
                prog.step(i, f"| размечено {len(done)}, потрачено ${spent:.2f}")
            if spent >= a.max_usd:
                json.dump(done, open(a.out, "w"))
                tg(f"🛑 Тили-бом: разметка учителя остановлена на потолке ${a.max_usd:.2f}")
                print("потолок расходов — стоп")
                return
    json.dump(done, open(a.out, "w"))
    prog.done(f"размечено {len(done)}, потрачено ${spent:.2f}")
    print(f"готово: {len(done)} меток, ${spent:.2f}")


def gold_check(a, prompt: str) -> None:
    """Разметить ручной эталон за дни [FROM, TO) и сравнить. В базу ничего не пишет."""
    lab = {int(k): v for k, v in json.load(open(f"{GOLD_DIR}/labels.json", encoding="utf-8")).items()}
    xs = [json.loads(l) for l in open(f"{GOLD_DIR}/sample.jsonl", encoding="utf-8")]
    xs = [x for x in xs if a.gold_check[0] <= x["posted_at"] < a.gold_check[1]]
    k = key()
    batches = [xs[i:i + BATCH] for i in range(0, len(xs), BATCH)]
    pred, tin, tout = {}, 0, 0
    with ThreadPoolExecutor(a.workers) as ex:
        for b, (labels, i1, o1) in zip(batches, ex.map(lambda b: label_batch(k, [x["text"] for x in b], prompt), batches)):
            tin, tout = tin + i1, tout + o1
            for x, l in zip(b, labels):
                pred[x["n"]] = l
    got = [x for x in xs if pred.get(x["n"])]
    ok = sum(1 for x in got if pred[x["n"]] == lab[x["n"]])
    cost = tin / 1e6 * PRICE_IN + tout / 1e6 * PRICE_OUT
    print(f"эталон {a.gold_check[0]}…{a.gold_check[1]}: размечено {len(got)} из {len(xs)}, "
          f"совпадение {ok / max(len(got), 1) * 100:.1f}%, ${cost:.3f}")
    for c in "NPUA":
        tot = [x for x in got if lab[x["n"]] == c]
        hit = sum(1 for x in tot if pred[x["n"]] == c)
        prd = sum(1 for x in got if pred[x["n"]] == c)
        print(f"  {c}: полнота {hit / max(len(tot), 1) * 100:.0f}% из {len(tot)}, "
              f"точность {hit / max(prd, 1) * 100:.0f}%")


if __name__ == "__main__":
    main()
