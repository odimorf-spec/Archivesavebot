"""Ручная постановка спама от имени пользователя (через юзербота).

Использование:
    python spam_user.py <чат> <количество> <текст...>
    python spam_user.py 555 10 hello
    python spam_user.py @username 5 привет --wait

Юзербот (run_userbot.bat) должен быть запущен — он заберёт задачу из очереди
и отправит сообщения с твоего аккаунта. Скрипту НЕ нужна сессия Telegram,
он только пишет задачу в data.db, поэтому конфликтов с демоном нет.
"""
import argparse
import os
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import database


def _parse_chat(raw: str):
    raw = (raw or "").strip().lstrip("@")
    try:
        return int(raw)
    except ValueError:
        return raw  # username строкой


def _userbot_alive(max_age_s: int = 45) -> bool:
    try:
        hb = database.meta_get("userbot_heartbeat")
    except Exception:
        return False
    if not hb:
        return False
    try:
        dt = datetime.fromisoformat(hb)
    except ValueError:
        return False
    return (datetime.now() - dt).total_seconds() <= max_age_s


def main() -> int:
    ap = argparse.ArgumentParser(description="Спам от имени пользователя (через юзербота).")
    ap.add_argument("chat", help="ID чата или @username")
    ap.add_argument("count", type=int, help="Количество сообщений (1-100)")
    ap.add_argument("text", nargs="+", help="Текст сообщений")
    ap.add_argument("--wait", action="store_true",
                    help="Ждать выполнения и показать итог")
    args = ap.parse_args()

    if not 1 <= args.count <= 100:
        print("ОШИБКА: количество — от 1 до 100.")
        return 1
    text = " ".join(args.text).strip()
    if not text:
        print("ОШИБКА: пустой текст.")
        return 1

    db_path = os.getenv("DB_PATH", "data.db").strip() or "data.db"
    database.init_db(db_path)

    if not _userbot_alive():
        print("ПРЕДУПРЕЖДЕНИЕ: юзербот не запущен (нет свежего heartbeat).")
        print("Задача будет записана, но отправится только после запуска run_userbot.bat.")

    try:
        owner_raw = database.meta_get("userbot_self")
        owner_id = int(owner_raw) if owner_raw else 0
    except (ValueError, TypeError):
        owner_id = 0

    job_id = database.enqueue_spam_job(
        _parse_chat(args.chat), args.count, text[:4000], owner_id)
    print(f"Задача #{job_id}: {args.count} сообщений в {args.chat} — в очереди.")

    if args.wait:
        deadline = time.time() + args.count * 5 + 120
        while time.time() < deadline:
            time.sleep(2)
            job = database.get_spam_job(job_id)
            if not job:
                print("Задача пропала из базы.")
                return 1
            if job["status"] in ("done", "failed", "expired"):
                print(f"Итог: {job['status']}, отправлено {job['sent']} из {job['msg_count']}. {job['error']}")
                return 0 if job["status"] == "done" else 1
            print(f"... {job['status']}, отправлено {job['sent']}", flush=True)
        print("Таймаут ожидания. Проверь лог юзербота.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
