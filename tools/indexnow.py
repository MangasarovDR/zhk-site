#!/usr/bin/env python3
"""Сообщить поисковикам (IndexNow: Яндекс и Bing) об адресах из sitemap.xml,
а также о старых адресах переехавших страниц (301) и о снятых страницах (404) —
чтобы поисковик перепроверил их, а не держал в выдаче.

    indexnow.py          # отправить все адреса (после запуска и после крупных изменений)
"""
import json
import os
import re
import sqlite3
import urllib.request
from datetime import datetime, timezone

ROOT = "/opt/zhk-site"
cfg = json.load(open(f"{ROOT}/data/config.json", encoding="utf-8"))
host = cfg["base_url"].split("//", 1)[1].rstrip("/")
urls = re.findall(r"<loc>(.*?)</loc>", open(f"{ROOT}/out/sitemap.xml", encoding="utf-8").read())
base = cfg["base_url"].rstrip("/")
d = sqlite3.connect(f"{ROOT}/data/site.db")
extra = [r[0] for r in d.execute("SELECT old_path FROM moved")] if d.execute(
    "SELECT 1 FROM sqlite_master WHERE name = 'moved'").fetchone() else []
extra += [p for (p,) in d.execute("SELECT path FROM url")
          if not os.path.exists(f"{ROOT}/out{p}index.html")]          # снятые страницы
seen = set(urls)
urls += [base + p for p in extra if base + p not in seen]
body = json.dumps({"host": host, "key": cfg["indexnow_key"],
                   "keyLocation": f"{cfg['base_url'].rstrip('/')}/{cfg['indexnow_key']}.txt",
                   "urlList": urls}).encode()
results = {}
for ep in ("https://yandex.com/indexnow", "https://www.bing.com/indexnow"):
    req = urllib.request.Request(ep, data=body, headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            print(f"{ep}: {r.status}, адресов {len(urls)}")
            results[ep] = str(r.status)
    except urllib.error.HTTPError as e:
        print(f"{ep}: HTTP {e.code} {e.read()[:200]!r}")
        results[ep] = f"{e.code}"
# Что и когда ушло — для проверки G21.
with open(f"{ROOT}/data/indexnow_last.json", "w", encoding="utf-8") as fh:
    json.dump({"sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "urls": urls,
               "results": results}, fh, ensure_ascii=False)
