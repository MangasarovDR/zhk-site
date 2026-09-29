#!/usr/bin/env python3
"""Локальная модель тональности «Тили-бом»: N жалоба, P похвала, U нейтрально, A реклама.

Учится на ручном эталоне (data/gold: 5 000 сообщений, разметка Claude по GUIDE.md) и на
разметке DeepSeek (таблица tone в site.db). Проверка — только на отложенных по времени днях
эталона (posted_at >= CUT); из обучения выброшены близнецы проверочных текстов.

    tone_model.py split            # разбить данные, записать data/tone/split.json
    tone_model.py linear           # линейные модели — опорная точка
    tone_model.py bert-pool        # этап 1: rubert-tiny2 на разметке DeepSeek (около часа)
    tone_model.py bert-gold        # этап 2: доучить на эталоне, подбор по валидации
    tone_model.py bert-gold --final  # то же на всём эталоне до CUT и одна проверка
    tone_model.py eval             # замер сохранённой модели → data/tone/eval.json
    tone_model.py label --days 3   # разметить моделью новые сообщения для сайта (--all — всё окно)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter

sys.path.insert(0, "/opt/zhk-site/tools")
from gold import LABELS as GOLD_LABELS, SAMPLE as GOLD_SAMPLE, twin_key  # noqa: E402
from zhk_data import DB, SRC  # noqa: E402

CUT = "2026-09-16"                      # проверка — всё, что написано с этого дня
CLASSES = ["N", "P", "U", "A"]
DIR = "/opt/zhk-site/data/tone"
SPLIT = f"{DIR}/split.json"
POOL = f"{DIR}/pool.jsonl"              # тексты DeepSeek-разметки до CUT
POOL_V2 = f"{DIR}/pool_v2.json"         # те же тексты, метки DeepSeek v2 (id → метка)
BERT_DIR = f"{DIR}/bert"
POOL_DIR = f"{DIR}/bert_pool"         # после этапа 1 (только DeepSeek)
VAL_CUT = "2026-09-11"                  # валидация для подбора: эталон с этого дня до CUT
EVAL = f"{DIR}/eval.json"
BIAS = f"{DIR}/bias.json"               # сдвиги классов, подобранные на валидации


def clean(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


def load_gold() -> list[dict]:
    lab = {int(k): v for k, v in json.load(open(GOLD_LABELS, encoding="utf-8")).items()}
    xs = [json.loads(l) for l in open(GOLD_SAMPLE, encoding="utf-8")]
    for x in xs:
        x["label"] = lab[x["n"]]
        x["text"] = clean(x["text"])
    return xs


def cmd_split() -> None:
    os.makedirs(DIR, exist_ok=True)
    gold = load_gold()
    test = [x for x in gold if x["posted_at"] >= CUT]
    test_twins = {twin_key(x["text"]) for x in test}
    gtrain = [x for x in gold if x["posted_at"] < CUT and twin_key(x["text"]) not in test_twins]
    dropped_gold = sum(1 for x in gold if x["posted_at"] < CUT) - len(gtrain)
    gold_ids = {x["id"] for x in gold}

    d = sqlite3.connect(DB)
    rows = d.execute("SELECT message_id, posted_at, label FROM tone "
                     "WHERE label IS NOT NULL AND posted_at < ?", (CUT,)).fetchall()
    rows = [r for r in rows if r[0] not in gold_ids]
    s = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True, timeout=60)
    text = {}
    ids = [r[0] for r in rows]
    for i in range(0, len(ids), 900):
        part = ids[i:i + 900]
        q = f"SELECT id, text FROM message WHERE id IN ({','.join('?' * len(part))})"
        text.update(s.execute(q, part).fetchall())
    pool, dropped_pool, seen = [], 0, set()
    for mid, pa, lab in rows:
        t = clean(text.get(mid, ""))
        if not t:
            continue
        k = twin_key(t)
        if k in test_twins:
            dropped_pool += 1
            continue
        if k in seen:                   # внутри обучения близнецы только раздувают шаблоны
            continue
        seen.add(k)
        pool.append({"id": mid, "posted_at": pa, "label": lab, "text": t})
    with open(POOL, "w", encoding="utf-8") as fh:
        for x in pool:
            fh.write(json.dumps(x, ensure_ascii=False) + "\n")
    json.dump({"cut": CUT,
               "test": [x["n"] for x in test],
               "gold_train": [x["n"] for x in gtrain],
               "pool_size": len(pool)}, open(SPLIT, "w"))
    print(f"проверка (эталон с {CUT}): {len(test)} — {dict(Counter(x['label'] for x in test))}")
    print(f"обучение, эталон до {CUT}: {len(gtrain)} (выброшено близнецов проверки: {dropped_gold})")
    print(f"обучение, разметка DeepSeek до {CUT}: {len(pool)} "
          f"(выброшено близнецов проверки: {dropped_pool}) — {dict(Counter(x['label'] for x in pool))}")


def load_split(teacher: str = "v1") -> tuple[list[dict], list[dict], list[dict]]:
    """teacher: v1 — метки DeepSeek из базы; v2 — переразметка с полными правилами и
    примерами из эталона (data/tone/pool_v2.json), она ближе к ручной разметке."""
    sp = json.load(open(SPLIT))
    gold = {x["n"]: x for x in load_gold()}
    test = [gold[n] for n in sp["test"]]
    gtrain = [gold[n] for n in sp["gold_train"]]
    pool = [json.loads(l) for l in open(POOL, encoding="utf-8")]
    if teacher == "v2":
        v2 = json.load(open(POOL_V2, encoding="utf-8"))
        pool = [dict(x, label=v2[str(x["id"])]) for x in pool if str(x["id"]) in v2]
    return test, gtrain, pool


def report(name: str, y: list[str], p: list[str]) -> dict:
    acc = sum(a == b for a, b in zip(y, p)) / len(y)
    per = {}
    for c in CLASSES:
        tp = sum(1 for a, b in zip(y, p) if a == c and b == c)
        n_true = sum(1 for a in y if a == c)
        n_pred = sum(1 for b in p if b == c)
        per[c] = {"recall": tp / n_true if n_true else None,
                  "precision": tp / n_pred if n_pred else None, "n": n_true}
    cm = {a: {b: sum(1 for u, v in zip(y, p) if u == a and v == b) for b in CLASSES} for a in CLASSES}
    line = "  ".join(f"{c}: полнота {per[c]['recall']*100:.0f}% точность "
                     f"{(per[c]['precision'] or 0)*100:.0f}%" for c in CLASSES if per[c]["n"])
    print(f"{name}: верно {acc*100:.1f}% из {len(y)} | {line}")
    return {"accuracy": acc, "per_class": per, "confusion": cm, "n": len(y)}


def deepseek_ref(test: list[dict]) -> dict:
    d = sqlite3.connect(DB)
    ds = dict(d.execute("SELECT message_id, label FROM tone WHERE label IS NOT NULL"))
    xs = [x for x in test if x["id"] in ds]
    return report("DeepSeek на тех же днях", [x["label"] for x in xs], [ds[x["id"]] for x in xs])


def cmd_linear() -> None:
    import numpy as np
    from scipy.sparse import hstack
    from sklearn.feature_extraction.text import HashingVectorizer, TfidfTransformer
    from sklearn.linear_model import LogisticRegression

    test, gtrain, pool = load_split()
    deepseek_ref(test)
    ch = HashingVectorizer(analyzer="char_wb", ngram_range=(2, 5), n_features=2**20,
                           alternate_sign=False, norm=None, lowercase=True)
    wd = HashingVectorizer(analyzer="word", ngram_range=(1, 2), n_features=2**19,
                           alternate_sign=False, norm=None, lowercase=True,
                           token_pattern=r"(?u)\b\w+\b")

    def feats(texts, tf=None):
        m = hstack([ch.transform(texts), wd.transform(texts)]).tocsr()
        if tf is None:
            tf = TfidfTransformer(sublinear_tf=True).fit(m)
        return tf.transform(m), tf

    for name, train, w_gold in (("линейная, только эталон", gtrain, 1.0),
                                ("линейная, эталон + DeepSeek", gtrain + pool, 5.0)):
        X, tf = feats([x["text"] for x in train])
        y = [x["label"] for x in train]
        w = np.array([w_gold if "n" in x else 1.0 for x in train])
        clf = LogisticRegression(C=4.0, max_iter=2000)
        clf.fit(X, y, sample_weight=w)
        Xt, _ = feats([x["text"] for x in test], tf)
        report(name, [x["label"] for x in test], list(clf.predict(Xt)))


def bert_parts(a, init_path: str):
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    torch.set_num_threads(2)
    torch.manual_seed(a.seed)
    tok = AutoTokenizer.from_pretrained(init_path)
    model = AutoModelForSequenceClassification.from_pretrained(
        init_path, num_labels=4, id2label=dict(enumerate(CLASSES)),
        label2id={c: i for i, c in enumerate(CLASSES)}, ignore_mismatched_sizes=True)
    return tok, model


def predict(tok, model, xs: list[dict], maxlen: int) -> list[list[float]]:
    import torch
    was = model.training
    model.eval()
    out = []
    with torch.no_grad():
        for s in range(0, len(xs), 64):
            enc = tok([x["text"] for x in xs[s:s + 64]], truncation=True,
                      max_length=maxlen, padding=True, return_tensors="pt")
            out += torch.softmax(model(**enc).logits, dim=1).tolist()
    model.train(was)
    return out


def train(a, tok, model, stage: str, data: list[dict], epochs: int, lr: float,
          check: list[dict], check_name: str, save_to: str | None = None) -> None:
    """Обучение с отчётом после каждой эпохи на check (валидация, не проверка)."""
    import numpy as np
    import torch
    from torch.utils.data import DataLoader
    from zhk_data import Progress

    idx = {c: i for i, c in enumerate(CLASSES)}

    def collate(b):
        enc = tok([x["text"] for x in b], truncation=True, max_length=a.maxlen,
                  padding=True, return_tensors="pt")
        enc["labels"] = torch.tensor([idx[x["label"]] for x in b])
        return enc

    dl = DataLoader(data, batch_size=a.batch, shuffle=True, collate_fn=collate)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)
    total = len(dl) * epochs
    warm = max(1, total // 20)
    sched = torch.optim.lr_scheduler.LambdaLR(
        opt, lambda s: min(1.0, (s + 1) / warm) * max(0.0, 1 - s / total))
    prog, step = Progress(f"тональность: {stage}", total), 0
    model.train()
    for ep in range(epochs):
        for batch in dl:
            y = batch.pop("labels")
            loss = torch.nn.functional.cross_entropy(model(**batch).logits, y)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step(); sched.step(); opt.zero_grad()
            step += 1
            if step % 50 == 0:
                prog.step(step, f"| потеря {loss.item():.3f}")
        if check:
            probs = predict(tok, model, check, a.maxlen)
            report(f"{stage}, эпоха {ep + 1} — {check_name}", [x["label"] for x in check],
                   [CLASSES[int(np.argmax(p))] for p in probs])
        if save_to:
            model.save_pretrained(save_to)
            tok.save_pretrained(save_to)
    prog.done("")


def val_split(gtrain: list[dict]) -> tuple[list[dict], list[dict]]:
    """Валидация — последние дни ДО разреза проверки: по ней выбираем настройки,
    а отложенные дни проверки трогаем один раз, в самом конце."""
    return ([x for x in gtrain if x["posted_at"] < VAL_CUT],
            [x for x in gtrain if x["posted_at"] >= VAL_CUT])


def cmd_bert_pool(a) -> None:
    """Этап 1: rubert-tiny2 учится на разметке DeepSeek (всё до CUT)."""
    _, gtrain, pool = load_split(a.teacher)
    _, val = val_split(gtrain)
    init = {"tiny": "cointegrated/rubert-tiny2",
            "seara": "seara/rubert-tiny2-russian-sentiment"}[a.init]
    tok, model = bert_parts(a, init)
    data = pool if a.pool_limit <= 0 else pool[:a.pool_limit]
    os.makedirs(POOL_DIR, exist_ok=True)
    t0 = time.time()
    train(a, tok, model, "DeepSeek", data, a.pool_epochs, a.lr, val, "валидация", POOL_DIR)
    json.dump({"init": init, "maxlen": a.maxlen, "cut": CUT, "pool": len(data), "teacher": a.teacher,
               "pool_epochs": a.pool_epochs, "lr": a.lr,
               "minutes": round((time.time() - t0) / 60)}, open(f"{POOL_DIR}/pool_meta.json", "w"))


def cmd_bert_gold(a) -> None:
    """Этап 2: доучивание на эталоне. --final: на всём эталоне до CUT, затем одна проверка."""
    test, gtrain, _ = load_split()
    tr, val = val_split(gtrain)
    pmeta = json.load(open(f"{POOL_DIR}/pool_meta.json"))
    a.maxlen = pmeta["maxlen"]
    tok, model = bert_parts(a, POOL_DIR)
    if not a.final:
        train(a, tok, model, "эталон (подбор)", tr, a.gold_epochs, a.lr_gold, val, "валидация")
        fit_bias(a, predict(tok, model, val, a.maxlen), val)
        return
    t0 = time.time()
    train(a, tok, model, "эталон", gtrain, a.gold_epochs, a.lr_gold, [], "")
    os.makedirs(BERT_DIR, exist_ok=True)
    model.save_pretrained(BERT_DIR)
    tok.save_pretrained(BERT_DIR)
    bias = [0.0] * len(CLASSES)
    if a.use_bias and os.path.exists(BIAS):
        bias = json.load(open(BIAS))["bias"]
    json.dump(dict(pmeta, gold_train=len(gtrain), gold_epochs=a.gold_epochs, lr_gold=a.lr_gold,
                   seed=a.seed, bias=bias, gold_minutes=round((time.time() - t0) / 60)),
              open(f"{BERT_DIR}/train_meta.json", "w"))
    cmd_eval()


def apply_bias(probs, bias) -> list[str]:
    """Метка = argmax(log p + сдвиг класса). Сдвиги подобраны на валидации, не на проверке."""
    import numpy as np
    lp = np.log(np.clip(np.asarray(probs, dtype=float), 1e-9, 1.0)) + np.asarray(bias, dtype=float)
    return [CLASSES[int(i)] for i in lp.argmax(axis=1)]


def fit_bias(a, probs, val: list[dict]) -> None:
    """Подбор сдвигов классов по валидации (сетка ±1 с шагом 0,2; U — опорный класс).
    Берём сдвиг, только если он даёт больше верных ответов, чем без сдвига."""
    import itertools
    import numpy as np
    y = np.array([CLASSES.index(x["label"]) for x in val])
    lp = np.log(np.clip(np.asarray(probs, dtype=float), 1e-9, 1.0))
    plain = float((lp.argmax(axis=1) == y).mean())
    best, best_acc = [0.0] * 4, plain
    grid = [round(v, 1) for v in np.arange(-1.0, 1.01, 0.2)]
    for bn, bp, ba in itertools.product(grid, grid, grid):
        b = np.array([bn, bp, 0.0, ba])
        acc = float(((lp + b).argmax(axis=1) == y).mean())
        if acc > best_acc + 1e-9:
            best, best_acc = [bn, bp, 0.0, ba], acc
    json.dump({"bias": best, "val_acc_plain": plain, "val_acc_bias": best_acc, "val_n": len(val),
               "gold_epochs": a.gold_epochs, "lr_gold": a.lr_gold}, open(BIAS, "w"))
    print(f"валидация: без сдвига {plain * 100:.1f}%, со сдвигом {best} — {best_acc * 100:.1f}%")


def cmd_eval() -> None:
    import numpy as np
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    torch.set_num_threads(2)
    test, gtrain, _ = load_split()
    meta = json.load(open(f"{BERT_DIR}/train_meta.json"))
    tok = AutoTokenizer.from_pretrained(BERT_DIR)
    model = AutoModelForSequenceClassification.from_pretrained(BERT_DIR).eval()
    probs = []
    with torch.no_grad():
        for s in range(0, len(test), 64):
            enc = tok([x["text"] for x in test[s:s + 64]], truncation=True,
                      max_length=meta["maxlen"], padding=True, return_tensors="pt")
            probs += torch.softmax(model(**enc).logits, dim=1).tolist()
    pred = apply_bias(probs, meta.get("bias", [0.0] * len(CLASSES)))
    train_twins = {twin_key(x["text"]) for x in gtrain}
    twins_in_test = sum(1 for x in test if twin_key(x["text"]) in train_twins)
    r = report("модель на отложенных днях", [x["label"] for x in test], pred)
    r.update({"cut": CUT, "twins_in_test": twins_in_test, "meta": meta,
              "deepseek": deepseek_ref(test),
              "predictions": [{"n": x["n"], "gold": x["label"], "pred": p,
                               "conf": round(max(pr), 4)} for x, p, pr in zip(test, pred, probs)]})
    json.dump(r, open(EVAL, "w"), ensure_ascii=False, indent=1)
    print(f"записано {EVAL}")


def cmd_label(a) -> None:
    """Разметить локальной моделью сообщения с темами за последние N дней (для сайта).
    По умолчанию — только те, у которых метки ещё нет; --all — переразметить всё окно."""
    import numpy as np
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    from datetime import datetime, timezone
    from zhk_data import SPAM, TOPICS

    torch.set_num_threads(2)
    meta = json.load(open(f"{BERT_DIR}/train_meta.json"))
    tok = AutoTokenizer.from_pretrained(BERT_DIR)
    model = AutoModelForSequenceClassification.from_pretrained(BERT_DIR).eval()
    d = sqlite3.connect(DB, timeout=60)
    d.execute("CREATE TABLE IF NOT EXISTS tone (message_id INTEGER PRIMARY KEY, zhk_id INTEGER,"
              " posted_at TEXT, label TEXT, src TEXT, at TEXT)")
    try:
        ids = {r[0] for r in d.execute("SELECT id FROM zhk WHERE active = 1 OR canon_id IS NOT NULL")}
    except sqlite3.OperationalError:
        ids = {r[0] for r in d.execute("SELECT id FROM zhk WHERE active = 1")}
    have = {} if a.all else dict(d.execute("SELECT message_id, src FROM tone WHERE label IS NOT NULL"))
    rx = re.compile("|".join(f"(?:{p})" for _, _, p in TOPICS), re.I)
    s = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True, timeout=60)
    todo = [(mid, zid, pa, clean(t)) for mid, zid, pa, t in s.execute(
        "SELECT m.id, so.zhk_id, m.posted_at, m.text FROM message m JOIN source so ON so.id = m.source_id"
        " WHERE so.is_zhk = 1 AND so.zhk_id IS NOT NULL AND m.dup_of IS NULL AND m.text NOT LIKE ?"
        f"   AND m.posted_at >= datetime('now','-{int(a.days)} days')", (SPAM,))
        if zid in ids and mid not in have and t and rx.search(t.lower())]
    print(f"к разметке локальной моделью: {len(todo)}")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for i in range(0, len(todo), 256):
        part = todo[i:i + 256]
        with torch.no_grad():
            enc = tok([x[3] for x in part], truncation=True, max_length=meta["maxlen"],
                      padding=True, return_tensors="pt")
            probs = torch.softmax(model(**enc).logits, dim=1).numpy()
        pred = apply_bias(probs, meta.get("bias", [0.0] * len(CLASSES)))
        d.executemany("INSERT OR REPLACE INTO tone VALUES (?,?,?,?, 'local', ?)",
                      [(mid, zid, pa, p, now) for (mid, zid, pa, _), p in zip(part, pred)])
        d.commit()
        if (i // 256) % 40 == 0:
            print(f"  … {i + len(part)} из {len(todo)}", flush=True)
    print(f"готово: размечено {len(todo)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["split", "linear", "bert-pool", "bert-gold", "eval", "label"])
    ap.add_argument("--init", choices=["tiny", "seara"], default="seara")
    ap.add_argument("--maxlen", type=int, default=128)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--pool-epochs", type=int, default=1)
    ap.add_argument("--pool-limit", type=int, default=0)
    ap.add_argument("--gold-epochs", type=int, default=4)
    ap.add_argument("--lr", type=float, default=5e-5)
    ap.add_argument("--lr-gold", type=float, default=3e-5)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--teacher", choices=["v1", "v2"], default="v2")
    ap.add_argument("--final", action="store_true", help="bert-gold: учить на всём эталоне и проверить")
    ap.add_argument("--days", type=int, default=3, help="label: окно в днях")
    ap.add_argument("--use-bias", action="store_true", help="bert-gold --final: взять сдвиги из bias.json")
    ap.add_argument("--all", action="store_true", help="label: переразметить всё окно")
    a = ap.parse_args()
    {"split": lambda: cmd_split(), "linear": lambda: cmd_linear(),
     "bert-pool": lambda: cmd_bert_pool(a), "bert-gold": lambda: cmd_bert_gold(a),
     "eval": lambda: cmd_eval(), "label": lambda: cmd_label(a)}[a.cmd]()
