#!/usr/bin/env python3
"""Эталон тональности: выборка 5 000 сообщений, показ пачками, запись ручных меток.

    gold.py sample             # собрать data/gold/sample.jsonl (5 000, поровну по темам, без близнецов)
    gold.py show 0 125         # напечатать сообщения 0..124 для разметки
    gold.py save 0 UNNAP...    # записать метки для сообщений начиная с 0 (N/P/U/A, пробелы игнорируются)
    gold.py status             # сколько размечено и как распределено

Правила разметки — data/gold/GUIDE.md.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, "/opt/zhk-site/tools")
from zhk_data import DB, SPAM, SRC, TOPICS  # noqa: E402

DIR = "/opt/zhk-site/data/gold"
SAMPLE = f"{DIR}/sample.jsonl"
LABELS = f"{DIR}/labels.json"
N_TOTAL = 5000
OLD = "/tmp/claude-0/-root/582c7a85-c56d-4807-bc44-4ee9f99727d7/scratchpad"


def twin_key(t: str) -> str:
    """Ключ близнеца, как в /opt/ml-label: первые 100 символов нормализованного текста."""
    n = re.sub(r"[^\w]+", " ", (t or "").lower()).strip()
    return hashlib.blake2b(n[:100].encode(), digest_size=10).hexdigest()


def cmd_sample() -> None:
    d = sqlite3.connect(DB)
    ids = {r[0] for r in d.execute("SELECT id FROM zhk WHERE active = 1")}
    s = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True, timeout=60)
    rx = [(k, re.compile(p, re.I)) for k, _, p in TOPICS]
    pool: dict[str, list] = {k: [] for k, _, _ in TOPICS}
    seen = set()
    for mid, zid, pa, t in s.execute(
            "SELECT m.id, so.zhk_id, m.posted_at, m.text FROM message m JOIN source so ON so.id = m.source_id"
            " WHERE so.is_zhk = 1 AND so.zhk_id IS NOT NULL AND m.dup_of IS NULL AND m.text NOT LIKE ?"
            "   AND m.posted_at >= datetime('now','-30 days') AND length(m.text) BETWEEN 10 AND 700",
            (SPAM,)):
        if zid not in ids:
            continue
        low = t.lower()
        hit = [k for k, r in rx if r.search(low)]
        if not hit:
            continue
        tk = twin_key(t)
        if tk in seen:
            continue
        seen.add(tk)
        pool[hit[0]].append({"id": mid, "zhk_id": zid, "posted_at": pa, "topics": hit, "text": t})
    random.seed(20260923)
    per = N_TOTAL // len(TOPICS)
    out = []
    for k, _, _ in TOPICS:
        out += random.sample(pool[k], min(per, len(pool[k])))
    rest = [x for k in pool for x in pool[k] if x not in out]
    out += random.sample(rest, N_TOTAL - len(out))
    random.shuffle(out)
    for i, x in enumerate(out):
        x["n"] = i
    with open(SAMPLE, "w", encoding="utf-8") as fh:
        for x in out:
            fh.write(json.dumps(x, ensure_ascii=False) + "\n")
    print(f"выборка: {len(out)} сообщений; по первой теме: "
          + ", ".join(f"{k} {sum(1 for x in out if x['topics'][0] == k)}" for k, _, _ in TOPICS))
    print(f"близнецов отброшено по пути; пул без близнецов: {len(seen)}")


def load() -> list[dict]:
    return [json.loads(l) for l in open(SAMPLE, encoding="utf-8")]


def labels() -> dict:
    try:
        return {int(k): v for k, v in json.load(open(LABELS, encoding="utf-8")).items()}
    except FileNotFoundError:
        return {}


def cmd_show(start: int, count: int) -> None:
    xs = load()[start:start + count]
    for x in xs:
        print(f"{x['n']}| {re.sub(r'\s+', ' ', x['text'])[:350]}")


def cmd_save(start: int, raw: str) -> None:
    labs = re.sub(r"\s+", "", raw.upper())
    bad = set(labs) - set("NPUA")
    if bad:
        sys.exit(f"недопустимые метки: {bad}")
    lab = labels()
    for i, c in enumerate(labs):
        lab[start + i] = c
    json.dump({str(k): v for k, v in sorted(lab.items())}, open(LABELS, "w", encoding="utf-8"))
    print(f"записано {len(labs)} меток ({start}..{start + len(labs) - 1}); всего размечено {len(lab)}")


def cmd_status() -> None:
    lab = labels()
    print(f"размечено {len(lab)} из {N_TOTAL}: {dict(Counter(lab.values()))}")


if __name__ == "__main__":
    c = sys.argv[1] if len(sys.argv) > 1 else ""
    if c == "sample":
        cmd_sample()
    elif c == "show":
        cmd_show(int(sys.argv[2]), int(sys.argv[3]))
    elif c == "save":
        cmd_save(int(sys.argv[2]), "".join(sys.argv[3:]))
    elif c == "status":
        cmd_status()
    else:
        sys.exit(__doc__)
