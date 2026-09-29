#!/usr/bin/env python3
"""Telegram-бот «Тили-бом» (@Tili_bom_bot): чаты жильцов ЖК по ссылке со страницы сайта.

Кнопка на странице ЖК ведёт на t.me/<бот>?start=z<id> — бот присылает название ЖК,
ссылку на его страницу и все чаты жильцов из нашей базы. Можно и просто написать
название комплекса — бот найдёт его среди опубликованных страниц.

Решение Давида: чаты отдаём не прямыми ссылками на сайте, а через бота, чтобы аудитория
оставалась у нас; поэтому каждый пользователь и каждый запрос пишутся в data/bot.db.

    bot.py            # работать (long polling; так его держит zhk-bot.service)
    bot.py --self-test Z   # без Telegram: что бот ответил бы на ЖК с id Z
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = "/opt/zhk-site"
SITE_DB = f"{ROOT}/data/site.db"
BOT_DB = f"{ROOT}/data/bot.db"
SEARCH = f"{ROOT}/out/search.json"
CONFIG = f"{ROOT}/data/config.json"
MAX_CHATS = 40


def token() -> str:
    for line in open(f"{ROOT}/.env", encoding="utf-8"):
        k, _, v = line.partition("=")
        if k.strip() == "TILIBOM_BOT_TOKEN":
            return v.strip()
    sys.exit("нет TILIBOM_BOT_TOKEN в /opt/zhk-site/.env")


def api(method: str, **params) -> dict:
    data = json.dumps(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=70) as r:
        return json.loads(r.read())


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def bot_db() -> sqlite3.Connection:
    d = sqlite3.connect(BOT_DB, timeout=30)
    d.executescript("""
        CREATE TABLE IF NOT EXISTS user (id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            first_seen TEXT, last_seen TEXT, requests INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS request (at TEXT, user_id INTEGER, kind TEXT, zhk_id INTEGER, query TEXT);""")
    return d


def remember(u: dict, kind: str, zhk_id: int | None = None, query: str | None = None) -> None:
    d = bot_db()
    t = now()
    d.execute("INSERT INTO user (id, username, first_name, first_seen, last_seen, requests) VALUES (?,?,?,?,?,1)"
              " ON CONFLICT(id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name,"
              " last_seen=excluded.last_seen, requests=requests+1",
              (u["id"], u.get("username"), u.get("first_name"), t, t))
    d.execute("INSERT INTO request VALUES (?,?,?,?,?)", (t, u["id"], kind, zhk_id, query))
    d.commit()


def pages() -> dict[int, dict]:
    """Опубликованные ЖК из последней сборки: id → имя, город, адрес страницы."""
    try:
        return {x["i"]: x for x in json.load(open(SEARCH, encoding="utf-8")) if "i" in x}
    except OSError:
        return {}


def base_url() -> str:
    try:
        return json.load(open(CONFIG, encoding="utf-8"))["base_url"].rstrip("/")
    except (OSError, KeyError):
        return "https://tilibom.automatiko.ru"


def canon(zid: int) -> int:
    d = sqlite3.connect(f"file:{SITE_DB}?mode=ro", uri=True, timeout=30)
    try:
        r = d.execute("SELECT canon_id FROM zhk WHERE id = ?", (zid,)).fetchone()
    except sqlite3.OperationalError:
        return zid
    return r[0] if r and r[0] else zid


def zhk_answer(zid: int) -> str | None:
    zid = canon(zid)
    p = pages().get(zid)
    if not p:
        return None
    d = sqlite3.connect(f"file:{SITE_DB}?mode=ro", uri=True, timeout=30)
    chats = d.execute("SELECT title, link, members FROM chat WHERE zhk_id = ? AND link IS NOT NULL"
                      " ORDER BY COALESCE(members, 0) DESC", (zid,)).fetchall()
    head = (f'<b>ЖК {html.escape(p["n"])}</b>, {html.escape(p["c"])}\n'
            f'На что жалуются жильцы: {base_url()}{p["u"]}\n')
    if not chats:
        return head + "\nОткрытых чатов этого ЖК в нашей базе пока нет."
    lines = []
    for title, link, members in chats[:MAX_CHATS]:
        m = f" · {members} уч." if members else ""
        lines.append(f'• <a href="{html.escape(link)}">{html.escape(title or "Чат")}</a>{m}')
    more = f"\n…и ещё {len(chats) - MAX_CHATS}" if len(chats) > MAX_CHATS else ""
    return head + f"\nЧаты жильцов ({len(chats)}):\n" + "\n".join(lines) + more


def search(q: str) -> list[dict]:
    q = q.lower().replace("ё", "е").replace("жк", "").strip(" «»\"'")
    if len(q) < 2:
        return []
    hits = []
    for x in pages().values():
        name = x["n"].lower().replace("ё", "е")
        if q in name or q in f'{name} {x["c"].lower()}':
            hits.append((0 if name.startswith(q) else 1, len(name), x))
    return [x for *_, x in sorted(hits, key=lambda h: (h[0], h[1]))[:8]]


HELLO = ("Это бот сайта «Тили-бом» — на что жалуются жильцы ЖК, по их собственным чатам.\n\n"
         "Напишите название жилого комплекса — пришлю ссылки на чаты его жильцов и страницу "
         "с жалобами и похвалой по темам. Или откройте ЖК на сайте: " + "{url}")


def handle(upd: dict) -> None:
    if "callback_query" in upd:
        cq = upd["callback_query"]
        api("answerCallbackQuery", callback_query_id=cq["id"])
        data = cq.get("data", "")
        if data.startswith("z") and data[1:].isdigit():
            zid = int(data[1:])
            remember(cq["from"], "button", zid)
            send(cq["message"]["chat"]["id"], zhk_answer(zid) or "Этого ЖК на сайте сейчас нет.")
        return
    msg = upd.get("message")
    if not msg or "text" not in msg:
        return
    chat_id, text, user = msg["chat"]["id"], msg["text"].strip(), msg["from"]
    if text.startswith("/start"):
        arg = text[6:].strip()
        if arg.startswith("z") and arg[1:].isdigit():
            zid = int(arg[1:])
            remember(user, "start", zid)
            send(chat_id, zhk_answer(zid) or "Этого ЖК на сайте сейчас нет. Напишите название — поищу.")
            return
        remember(user, "hello")
        send(chat_id, HELLO.format(url=base_url()))
        return
    if text.startswith("/"):
        send(chat_id, HELLO.format(url=base_url()))
        return
    hits = search(text)
    remember(user, "search", hits[0]["i"] if len(hits) == 1 else None, text[:200])
    if not hits:
        send(chat_id, "Такого ЖК среди наших страниц не нашёл. Попробуйте часть названия — «Прокшино», «Метрополия».")
    elif len(hits) == 1:
        send(chat_id, zhk_answer(hits[0]["i"]) or "Этого ЖК на сайте сейчас нет.")
    else:
        kb = [[{"text": f'{x["n"]} · {x["c"]}', "callback_data": f'z{x["i"]}'}] for x in hits]
        api("sendMessage", chat_id=chat_id, text="Какой из них?", reply_markup={"inline_keyboard": kb})


def send(chat_id: int, text: str) -> None:
    api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True)


def main() -> None:
    global TOKEN
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", type=int, metavar="Z")
    a = ap.parse_args()
    if a.self_test is not None:
        print(zhk_answer(a.self_test) or "НЕТ СТРАНИЦЫ")
        return
    TOKEN = token()
    api("setMyCommands", commands=[{"command": "start", "description": "Как пользоваться ботом"}])
    offset = 0
    print("бот запущен", flush=True)
    while True:
        try:
            r = api("getUpdates", offset=offset, timeout=50, allowed_updates=["message", "callback_query"])
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    handle(upd)
                except Exception as e:                 # один кривой запрос не роняет бота
                    print(f"ошибка обработки: {e!r}", flush=True)
        except Exception as e:
            print(f"ошибка связи: {e!r}", flush=True)
            time.sleep(5)


TOKEN = ""
if __name__ == "__main__":
    main()
