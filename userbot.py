"""Юзербот (Telethon, от имени пользователя). Две задачи:

1. Мгновенно сохраняет ВСЮ медиу из переписок аккаунта — включая ту, что
   Bot API боту не отдаёт: исчезающие/view-once, спойлеры, protected-контент
   и платные фото. Путь дописывается в data.db (media_file), и отчёты бота
   о deletions/edits находят локальную копию.
2. Разбирает очередь spam_jobs из data.db и отправляет сообщения от имени
   пользователя (Bot API такое режет лимитами).

Запуск: run_userbot.bat (первый раз — вход по телефону + код + 2FA при
необходимости, дальше сессия хранится в <SESSION_NAME>.session).
Держать запущенным рядом с bot.py.
"""
import asyncio
import glob
import logging
import os

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO
)
log = logging.getLogger("userbot")

API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_NAME = os.getenv("SESSION_NAME", "save_session").strip() or "save_session"
DB_PATH = os.getenv("DB_PATH", "data.db").strip() or "data.db"
MEDIA_PATH = os.getenv("MEDIA_PATH", "media").strip() or "media"
SPAM_DELAY = float(os.getenv("SPAM_DELAY_USERBOT") or 0.34)  # ≈3 сообщ/сек
JOB_POLL_SECONDS = 2.0
HEARTBEAT_SECONDS = 15.0

import database  # noqa: E402
from telethon import TelegramClient, events  # noqa: E402
from telethon.errors import FloodWaitError  # noqa: E402


async def save_incoming_media(message, chat_id: int, my_id: int) -> str:
    """Скачать медиу сообщения сейчас. Возвращает путь или ''.

    Пропускает, если локальная копия уже есть в базе. Недостающую строку
    сообщения создаёт (бот мог быть офлайн).
    """
    if message is None or getattr(message, "media", None) is None:
        return ""
    mid = getattr(message, "id", 0)
    if not mid or not chat_id:
        return ""
    try:
        rec = database.get_message(mid, chat_id)
    except Exception as e:
        log.warning("get_message %s/%s: %r", chat_id, mid, e)
        rec = None
    if rec and rec.get("media_file"):
        return rec["media_file"]
    folder = os.path.join(MEDIA_PATH, str(chat_id))
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception as e:
        log.warning("mkdir %s: %r", folder, e)
        return ""
    try:
        done = glob.glob(os.path.join(folder, f"{mid}_ub.*"))
    except Exception:
        done = []
    if done:
        if not rec:
            _remember_message(message, chat_id, my_id, done[0])
        elif not rec.get("media_file"):
            try:
                database.set_media_file(mid, chat_id, done[0])
            except Exception as e:
                log.warning("set_media_file: %r", e)
        return done[0]
    try:
        path = await message.download_media(
            file=os.path.join(folder, f"{mid}_ub"))
    except Exception as e:
        log.warning("download_media %s/%s: %r", chat_id, mid, e)
        return ""
    if not path:
        return ""
    path = str(path)
    log.info("медиа сохранена юзерботом: %s", path)
    try:
        ttl = getattr(message, "ttl_period", 0) or 0
        if rec:
            database.set_media_file(mid, chat_id, path,
                                    is_ephemeral=1 if ttl else None)
        else:
            _remember_message(message, chat_id, my_id, path)
    except Exception as e:
        log.warning("set_media_file: %r", e)
    return path


def _remember_message(message, chat_id: int, my_id: int, media_file: str):
    """Создать строку архива для сообщения, которое бот не видел."""
    try:
        text = getattr(message, "text", None) or getattr(message, "message", "") or ""
    except Exception:
        text = ""
    try:
        sender = getattr(message, "sender", None)
        sender_id = getattr(sender, "id", None)
        sender_name = getattr(sender, "first_name", None) or str(sender_id or "?")
    except Exception:
        sender_id, sender_name = None, "?"
    try:
        ttl = getattr(message, "ttl_period", 0) or 0
    except Exception:
        ttl = 0
    try:
        date = message.date.isoformat(timespec="seconds") if getattr(message, "date", None) else ""
    except Exception:
        date = ""
    try:
        database.save_message(
            getattr(message, "id", 0), chat_id, sender_id,
            str(sender_name or "?"), str(chat_id), str(text or ""),
            "", date, media_file=media_file,
            is_ephemeral=1 if ttl else 0, owner_id=my_id or 0)
    except Exception as e:
        log.warning("save_message: %r", e)


async def process_one_job(client, job: dict, delay: float) -> tuple[str, str, int]:
    """Отправить одну задачу спама. Возвращает (status, error, sent)."""
    chat_id = job["chat_id"]
    total = max(1, int(job.get("msg_count") or 1))
    text = job.get("text") or ""
    sent = int(job.get("sent") or 0)
    start = sent
    log.info("spam job #%s: чат %s, %s шт. (уже %s)", job["id"], chat_id, total, sent)
    error = ""
    status = "done"
    for _ in range(start, total):
        try:
            try:
                await client.send_message(chat_id, text)
            except FloodWaitError as e:
                wait = int(getattr(e, "seconds", 0) or 0) + 1
                log.warning("spam job #%s: FloodWait %s сек", job["id"], wait)
                await asyncio.sleep(wait)
                await client.send_message(chat_id, text)
            sent += 1
        except FloodWaitError as e:
            wait = int(getattr(e, "seconds", 0) or 0) + 1
            log.warning("spam job #%s: повторный FloodWait %s сек — прерываю",
                        job["id"], wait)
            error = f"FloodWait {wait}с (отправлено {sent} из {total})"
            status = "failed"
            break
        except Exception as e:
            log.warning("spam job #%s: %r", job["id"], e)
            error = f"{type(e).__name__}: {e} (отправлено {sent} из {total})"
            status = "failed"
            break
        try:
            database.update_spam_progress(job["id"], sent)
        except Exception as e:
            log.warning("update_spam_progress: %r", e)
        if delay > 0:
            await asyncio.sleep(delay)
    return status, error, sent


async def _jobs_loop(client):
    while True:
        try:
            database.meta_set(
                "userbot_heartbeat",
                __import__("datetime").datetime.now().isoformat(timespec="seconds"))
        except Exception as e:
            log.warning("heartbeat: %r", e)
        try:
            job = database.claim_spam_job()
        except Exception as e:
            log.warning("claim_spam_job: %r", e)
            job = None
        if job:
            try:
                status, error, sent = await process_one_job(client, job, SPAM_DELAY)
            except Exception as e:
                log.exception("process_one_job: %s", e)
                status, error, sent = "failed", repr(e), int(job.get("sent") or 0)
            try:
                database.update_spam_progress(job["id"], sent)
                database.finish_spam_job(job["id"], status, error)
            except Exception as e:
                log.warning("finish_spam_job: %r", e)
            log.info("spam job #%s: %s (отправлено %s), %s",
                     job["id"], status, sent, error)
        await asyncio.sleep(JOB_POLL_SECONDS)


async def main():
    if not API_ID or not API_HASH:
        print("ОШИБКА: заполни API_ID и API_HASH в .env (берутся на https://my.telegram.org -> API development tools).")
        raise SystemExit(1)
    database.init_db(DB_PATH)
    os.makedirs(MEDIA_PATH, exist_ok=True)
    try:
        n = database.expire_stale_spam_jobs(60)
        if n:
            log.info("просрочено старых задач: %s", n)
    except Exception as e:
        log.warning("expire_stale: %r", e)
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start()  # первый запуск спросит телефон/код/2FA в консоли
    me = await client.get_me()
    my_id = me.id if me else 0
    log.info("юзербот запущен как %s (%s)", getattr(me, "first_name", "?"), my_id)
    try:
        database.meta_set("userbot_self", str(my_id))
    except Exception as e:
        log.warning("meta userbot_self: %r", e)

    @client.on(events.NewMessage)
    async def _on_message(event):
        try:
            await save_incoming_media(event.message, event.chat_id, my_id)
        except Exception as e:
            log.warning("autosave: %r", e)

    asyncio.get_running_loop().create_task(_jobs_loop(client))
    log.info("автосохранение медиа + очередь спама активны. Ctrl+C — стоп.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
