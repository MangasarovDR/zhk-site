#!/usr/bin/env python3
"""Ежедневная пересборка «Тили-бом»: данные → тональность → метрики → сайт.

Шаги идут по порядку; сбой любого — сообщение Давиду в Telegram, код выхода 1
(systemd видит его как упавший запуск), а сайт остаётся прошлой сборки: сборщик
подменяет папку out только после удачной сборки.

    daily.py                # полный прогон (так его запускает zhk-site-daily.timer)
    daily.py --fail-test    # проверка оповещения: нарочно падающий шаг
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = "/opt/zhk-site"
PY_SYS = "/usr/bin/python3"                  # jinja2 и sqlite — системный питон
PY_ML = f"{ROOT}/.venv/bin/python"           # torch и transformers — venv
LOG_DIR = f"{ROOT}/data/daily"
LAST_OK = f"{LOG_DIR}/last_ok.json"
CONFIG = f"{ROOT}/data/config.json"
sys.path.insert(0, f"{ROOT}/tools")


def tone_step() -> list[str]:
    """Кто ставит тон новым сообщениям: локальная модель (когда она прошла проверку
    и включена в config.json) или DeepSeek с потолком расходов на день."""
    cfg = json.load(open(CONFIG, encoding="utf-8")) if os.path.exists(CONFIG) else {}
    if cfg.get("tone") == "local":
        return [PY_ML, "tools/tone_model.py", "label", "--days", "3"]
    return [PY_ML, "tools/tone_llm.py", "--prompt", "v2", "--days", "3", "--max-usd", "0.5"]


def steps(fail_test: bool) -> list[tuple[str, list[str], int]]:
    if fail_test:
        return [("проверка оповещения", [PY_SYS, "-c", "import sys; print('нарочный сбой'); sys.exit(3)"], 60)]
    return [
        ("чаты не ЖК", [PY_SYS, "tools/zhk_data.py", "chats"], 1800),
        ("отбор ЖК", [PY_SYS, "tools/zhk_data.py", "select"], 1800),
        ("ЕРЗ", [PY_SYS, "tools/zhk_data.py", "erz"], 3 * 3600),
        ("адреса", [PY_SYS, "tools/zhk_data.py", "geocode"], 3600),
        ("УК", [PY_SYS, "tools/zhk_data.py", "uk"], 1800),
        ("склейка дублей", [PY_SYS, "tools/zhk_data.py", "merge"], 3600),
        ("город по координатам", [PY_SYS, "tools/zhk_data.py", "place"], 2 * 3600),
        ("тональность", tone_step(), 3 * 3600),
        ("метрики", [PY_SYS, "tools/zhk_data.py", "metrics"], 3600),
        ("сборка сайта", [PY_SYS, "tools/build_site.py"], 1800),
        ("поисковики (IndexNow)", [PY_SYS, "tools/indexnow.py"], 300),
    ]


def send_alert(text: str) -> bool:
    """Как zhk_data.tg, но возвращает, дошло ли сообщение: проверка гейта это видит."""
    import urllib.request
    from zhk_data import env
    token, chat = env("TELEGRAM_TOKEN"), env("ALLOWED_USER_ID")
    if not token or not chat:
        return False
    try:
        data = json.dumps({"chat_id": int(chat), "text": text, "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data,
                                     headers={"Content-Type": "application/json"})
        return bool(json.loads(urllib.request.urlopen(req, timeout=25).read()).get("ok"))
    except Exception:
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fail-test", action="store_true")
    a = ap.parse_args()
    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    log_path = f"{LOG_DIR}/{'failtest' if a.fail_test else 'run'}-{stamp}.log"
    t_all = time.time()
    done = []
    with open(log_path, "w", encoding="utf-8") as log:
        for name, cmd, timeout in steps(a.fail_test):
            t0 = time.time()
            log.write(f"\n=== {name}: {' '.join(cmd)}\n")
            log.flush()
            try:
                r = subprocess.run(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, timeout=timeout)
                out, code = r.stdout, r.returncode
            except subprocess.TimeoutExpired as e:
                out, code = (e.stdout or "") if isinstance(e.stdout, str) else "", f"таймаут {timeout} с"
            log.write(out)
            log.write(f"\n--- {name}: код {code}, {time.time() - t0:.0f} с\n")
            log.flush()
            if code != 0:
                tail = "\n".join(out.strip().splitlines()[-6:])[-900:]
                prefix = "🧪 Проверка оповещения — " if a.fail_test else "🛑 "
                ok = send_alert(f"{prefix}Тили-бом: ежедневная пересборка упала на шаге «{name}» "
                                f"(код {code}). Сайт остался вчерашним.\n\n{tail}\n\nЛог: {log_path}")
                print(f"шаг «{name}» упал (код {code}); оповещение {'ALERT_SENT' if ok else 'НЕ ОТПРАВЛЕНО'}")
                sys.exit(1)
            done.append({"step": name, "seconds": round(time.time() - t0)})
    json.dump({"finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
               "minutes": round((time.time() - t_all) / 60, 1), "steps": done, "log": log_path},
              open(LAST_OK, "w"), ensure_ascii=False, indent=1)
    print(f"пересборка готова за {(time.time() - t_all) / 60:.1f} мин; лог {log_path}")


if __name__ == "__main__":
    main()
