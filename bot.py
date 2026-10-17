"""
Сохранялка переписок через Telegram Business (Bot API, без мини-приложения).

Как работает:
- У владельца Telegram Premium, бот подключён: Настройки → Telegram Business → Чат-боты.
- Бот получает копии сообщений бизнес-аккаунта (лички 1-на-1), правки и удаления.
- Каждое новое сообщение сохраняется в SQLite; правка пишет «было/стало» в журнал
  events; удаление достаёт исходник из базы, пишет в журнал и присылает отчёт
  владельцу в личку с ботом.
- Обычные чаты, где бот просто добавлен: сохраняются новые + правки.
  Удаления там Bot API не отдаёт — ограничение Telegram (поэтому нужен Business).

Команд всего две (функциональных):
  /history — история удалений и правок (журнал events)
  /stats   — статистика (сколько сохранено / удалено / изменено)
Служебный /start — только приветствие, в меню не вынесен как функция.

Исчезающие / одноразовые (view-once) и авто-удаление по TTL:
  - медиа качается мгновенно при получении → копия живёт и после просмотра;
  - авто-таймер чата (message_auto_delete_timer_changed) ведётся в базе,
    помечает все сообщения чата как исчезающие (таймер 10с и т.п.);
  - сырые ttl-поля сообщения (api_kwargs) ловим эвристическим _detect_ephemeral;
  - для сообщений с известным ttl планируется проверка: если штатное удаление
    не пришло вовремя, в журнал пишется 'expired' и шлётся повтор отчёта.

Почему раньше «перестал сохранять» (найденные баги, исправлены здесь):
  1. Поле удалений в единственном числе (без финального «s») — такого поля
     в Update нет, правильное — deleted_business_messages (мн.ч.,
     класс BusinessMessagesDeleted).
     Прямое обращение к несуществующему атрибуту бросало AttributeError
     на КАЖДОМ апдейте, хендлер падал до сохранения → база оставалась пустой
     (в data.db были connections, но 0 messages). Исправлено + добавлен
     отдельный BusinessMessagesDeletedHandler, где опечатка в принципе
     невозможна (тип проверяет библиотека).
  2. Один общий TypeHandler(Update) без try/except — любая ошибка убивала
     обработку всего апдейта. Теперь хендлеры раздельные, каждый в try/except
     + общий error_handler с логом.
  3. Правки перезаписывали текст (UPDATE) без журнала — «было» терялось.
     Теперь каждая правка/удаление пишется в таблицу events, /history читает её.
  4. Потеря owner при протухшем business_connection_id: раньше
     get_business_owner()==None → return без отчёта. Теперь fallback на всех
     известных владельцев (get_all_business_owners).
  5. Неэкранированный HTML в отчётах ломал send_message при '<' в тексте.
     Теперь html.escape для всего пользовательского ввода.

Запуск: python bot.py (токен — BOT_TOKEN в .env)
"""
import asyncio
import html
import logging
import os
import re

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import RetryAfter
from telegram.ext import (
    Application,
    BusinessConnectionHandler,
    BusinessMessagesDeletedHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database

load_dotenv()


def _load_token() -> str:
    """Токен: сначала BOT_TOKEN из окружения/.env, иначе файл api.txt."""
    tok = os.getenv("BOT_TOKEN", "").strip()
    if tok:
        return tok
    for cand in ("api.txt", os.path.join(os.path.dirname(__file__), "api.txt")):
        try:
            if os.path.exists(cand):
                with open(cand, encoding="utf-8") as f:
                    tok = f.read().strip().split()[0] if f else ""
                if tok:
                    return tok
        except Exception:
            pass
    return ""


BOT_TOKEN: str = _load_token()
DB_PATH: str = os.getenv("DB_PATH", "data.db").strip() or "data.db"
MEDIA_PATH: str = os.getenv("MEDIA_PATH", "media").strip() or "media"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO
)
log = logging.getLogger("bot")

HISTORY_COMMANDS = ("history", "hist")
STATS_COMMANDS = ("stats", "stat", "statistics")
SPAM_COMMANDS = ("spam",)

# ---------- АРХИВ: брендинг и меню (ТЗ: «Название Архив бот.txt») ----------

NEWS_USERNAME = "archivesavem"
NEWS_URL = f"https://t.me/{NEWS_USERNAME}"

# Лимиты /spam: защита от бана за флуд.
SPAM_MIN_COUNT = 1
SPAM_MAX_COUNT = 100
# Пауза между сообщениями при отправке через Bot API (юзербота нет рядом).
# 0.34 сек ≈ 3 сообщения в секунду.
SPAM_DELAY_API = float(os.getenv("SPAM_DELAY_API") or 0.34)
# Попыток отправки одного сообщения при FloodWait.
SPAM_RETRIES = 5

WELCOME_TEXT = (
    "👋 <b>Добро пожаловать в АРХИВ!</b>\n\n"
    "Бот сохраняет удалённые и изменённые сообщения, фото и видео, "
    "а также помогает управлять сообщениями в чате.\n\n"
    "👇 Нажми «Далее», чтобы продолжить."
)

MAIN_MENU_TEXT = (
    "📋 <b>Меню АРХИВа</b>\n\n"
    "Выбери раздел:"
)

INSTRUCTION_TEXT = (
    "⚙️ <b>Как подключить АРХИВ?</b>\n\n"
    "1. Открой Telegram → Настройки → Telegram Business.\n"
    "2. Перейди в Чат-боты.\n"
    "3. Найди бота АРХИВ и подключи его.\n\n"
    "☑️ Готово! Бот подключён и сможет работать в выбранных чатах.\n\n"
    "💬 <b>Разрешение для спама (/spam):</b>\n"
    "Настройки → Telegram Business → Чат-боты → прокрути вниз до раздела "
    "«Управление сообщениями» → разверни его → включи "
    "«Отвечать на сообщения»."
)

COMMANDS_TEXT = (
    "📋 <b>Команды бота</b>\n\n"
    "/spam — отправляет несколько сообщений от твоего имени.\n"
    "Работает в чате с собеседником и в группе, "
    "в личке с ботом недоступна.\n"
    "Если запущен юзербот — отправляет он, иначе бот сам с паузами.\n\n"
    "Пример:\n"
    "<code>/spam 10 hello</code>\n\n"
    "Дополнительно:\n"
    "/history [N] — история удалений и правок\n"
    "/stats — статистика"
)

NOT_CONNECTED_NOTE = (
    "\n\n⚠️ <b>Бот не подключён.</b>\n"
    "После подключения к боту ты увидишь удалённые или изменённые сообщения.\n"
    "Чтобы бот начал работать, подключи его, следуя инструкции."
)


def _kb_welcome() -> InlineKeyboardMarkup:
    # Стартовый экран: только одна кнопка — ведёт в общее меню.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Далее ➡️", callback_data="menu:main")],
    ])


def _kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Инструкция", callback_data="menu:instruction"),
         InlineKeyboardButton("📋 Команды", callback_data="menu:commands")],
        [InlineKeyboardButton("📰 Новости", url=NEWS_URL)],
    ])


def _kb_instruction() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Команды", callback_data="menu:commands")],
        [InlineKeyboardButton("📰 Новости", url=NEWS_URL)],
        [InlineKeyboardButton("◀️ Назад", callback_data="menu:main")],
    ])


def _kb_commands() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Назад", callback_data="menu:main")],
        [InlineKeyboardButton("📰 Новости", url=NEWS_URL)],
    ])


# /spam текстом (без сущности bot_command — так приходит из чата с человеком,
# где нет бота и клиент команду не подсвечивает). Группа args — всё после /spam.
_SPAM_CMD_RE = re.compile(r"^/spam(?:@[\w_]+)?(?:\s+(?P<args>[\s\S]+)|\s*)$")

_SPAM_HINTS = {
    "usage": ("📋 <b>Использование:</b> <code>/spam 10 hello</code>\n"
              f"Количество: {SPAM_MIN_COUNT}–{SPAM_MAX_COUNT}, далее — текст."),
    "not_a_number": ("⚠️ Первое слово после /spam — число. "
                     "Пример: <code>/spam 10 hello</code>"),
    "out_of_range": (f"⚠️ Количество — от {SPAM_MIN_COUNT} "
                     f"до {SPAM_MAX_COUNT}."),
}


def _parse_spam(arg_text: str) -> tuple[int | None, str, str]:
    """Разбор '10 hello' -> (count, text, err_key); при успехе err_key == ''."""
    parts = (arg_text or "").strip().split(None, 1)
    if len(parts) < 2:
        return None, "", "usage"
    try:
        count = int(parts[0])
    except ValueError:
        return None, "", "not_a_number"
    if not SPAM_MIN_COUNT <= count <= SPAM_MAX_COUNT:
        return None, "", "out_of_range"
    spam_text = parts[1].strip()
    if not spam_text:
        return None, "", "usage"
    return count, spam_text[:4000], ""


def _userbot_status(max_age_s: int = 45) -> tuple[bool, int | None]:
    """Жив ли юзербот-демон (heartbeat) + его user id. Иначе (False, None)."""
    try:
        hb = database.meta_get("userbot_heartbeat")
    except Exception:
        return False, None
    if not hb:
        return False, None
    try:
        from datetime import datetime as _dt
        age = (_dt.now() - _dt.fromisoformat(hb)).total_seconds()
    except ValueError:
        return False, None
    if age > max_age_s:
        return False, None
    try:
        raw = database.meta_get("userbot_self")
        self_id = int(raw) if raw else None
    except (TypeError, ValueError):
        self_id = None
    return True, self_id


async def _send_via_bot_api(bot, chat_id: int, count: int, spam_text: str,
                           send_kwargs: dict) -> tuple[int, list[str]]:
    """Прямая отправка через Bot API с защитой от флуда.

    FloodWait пережидает и повторяет (до SPAM_RETRIES на сообщение),
    прочие ошибки — пропуск сообщения БЕЗ обрыва всей пачки.
    Возвращает (отправлено, [ошибки]).
    """
    sent = 0
    errors: list[str] = []
    for _ in range(count):
        ok = False
        last_err: Exception | None = None
        for _attempt in range(SPAM_RETRIES):
            try:
                await bot.send_message(chat_id, spam_text, **send_kwargs)
                ok = True
                break
            except RetryAfter as e:
                last_err = e
                wait = float(getattr(e, "retry_after", 0) or 0)
                log.info("spam FloodWait %s сек в чате %s — жду",
                         wait, chat_id)
                await asyncio.sleep(wait + 1.0)
                continue
            except Exception as e:
                last_err = e
                break
        if ok:
            sent += 1
        else:
            errors.append(f"{type(last_err).__name__}: {last_err}")
            log.warning("spam пропуск сообщения в чате %s: %r",
                        chat_id, last_err)
        if SPAM_DELAY_API > 0:
            await asyncio.sleep(SPAM_DELAY_API)
    return sent, errors


async def _submit_spam(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                       count: int, spam_text: str, requester_id: int,
                       is_business: bool, msg, send_kwargs: dict) -> bool:
    """Отправка спама.

    Если юзербот запущен — задача ему в очередь (строго от имени
    пользователя). Иначе — напрямую через Bot API с безопасными паузами
    и ретраями (работает сразу, без ключей my.telegram.org).
    True = принято/выполнено (подсказка уже показана).
    """

    async def _tell(text: str):
        if is_business:
            try:
                await context.bot.send_message(requester_id, text,
                                               parse_mode="HTML")
            except Exception as e:
                log.warning("spam tell to %s: %s", requester_id, e)
        else:
            try:
                await msg.reply_text(text, parse_mode="HTML")
            except Exception as e:
                log.warning("spam tell: %s", e)

    alive, _ = _userbot_status()
    if alive:
        try:
            job_id = database.enqueue_spam_job(chat_id, count, spam_text,
                                                requester_id)
        except Exception as e:
            log.warning("enqueue_spam_job: %r", e)
            await _tell("⚠️ Не смог записать задачу в очередь, попробуй позже.")
            return False
        log.info("spam задача #%s: чат %s, %s шт. от %s",
                 job_id, chat_id, count, requester_id)
        await _tell(f"✅ <b>Принято (задача #{job_id}): {count} сообщений.</b>\n"
                    "Отправляю от твоего имени — это займёт немного времени.")
        return True
    # Юзербота нет рядом — шлём сами через Bot API: медленно, с ретраями.
    bc = (send_kwargs or {}).get("business_connection_id") if send_kwargs else None
    if bc:
        try:
            await context.bot.get_business_connection(bc)
        except Exception as e:
            log.warning("business connection %s недействительно: %r", bc, e)
            await _tell("⚠️ <b>Business-подключение недействительно.</b>\n"
                        "Переподключи бота: Настройки → Telegram Business → "
                        "Чат-боты — и повтори команду.")
            return True
    sent, errors = await _send_via_bot_api(context.bot, chat_id, count,
                                            spam_text, send_kwargs)
    if sent >= count and not errors:
        await _tell(f"✅ Отправлено {sent} из {count}.")
    else:
        detail = (" Ошибка: " + errors[0][:200]) if errors else ""
        extra = ""
        if any("business" in e.lower() or "peer" in e.lower() for e in errors):
            extra = (" Проверь: Настройки → Telegram Business → Чат-боты → "
                     "ниже «Управление сообщениями» → включи "
                     "«Отвечать на сообщения», и повтори в чате"
                     " с собеседником.")
        await _tell(f"⚠️ Отправлено {sent} из {count}.{detail}{extra}")
    return True


async def _resolve_business_owner(bot, bc_id: str) -> int | None:
    """Точный владелец business-подключения.

    Сначала из базы (пишется в on_business_connection), иначе — живой запрос
    getBusinessConnection: база пуста, если подключение сделали до запуска
    бота или base пересоздали. Найденного владельца сразу сохраняем в базу.
    """
    if bc_id:
        try:
            owner = database.get_business_owner(bc_id)
        except Exception:
            owner = None
        if owner is not None:
            return owner
    if bc_id:
        try:
            conn = await bot.get_business_connection(bc_id)
            user = getattr(conn, "user", None)
            if user is not None and getattr(user, "id", None) is not None:
                try:
                    database.save_business_connection(bc_id, user.id)
                except Exception as e:
                    log.warning("save_business_connection: %s", e)
                return user.id
        except Exception as e:
            log.warning("getBusinessConnection %s: %r", bc_id, e)
    return None


async def _handle_business_spam_text(update: Update,
                                     context: ContextTypes.DEFAULT_TYPE,
                                     msg, chat, text: str) -> bool:
    """Перехват '/spam ...' текстом в business-чате.

    True = писал владелец: спам выполнен либо подсказка ушла ему в личку.
    False = писал собеседник: не трогаем, пусть сохраняется как обычный текст.
    """
    bc_id = getattr(msg, "business_connection_id", "") or ""
    sender_id = msg.from_user.id if getattr(msg, "from_user", None) else None
    owner_id = await _resolve_business_owner(context.bot, bc_id)
    if owner_id is None or sender_id != owner_id:
        log.info("spam в business-чате %s: sender=%s owner=%s — игнор, в архив",
                 getattr(chat, "id", "?"), sender_id, owner_id)
        return False
    owners = [owner_id]
    m = _SPAM_CMD_RE.match(text.strip())
    arg_text = (m.group("args") or "") if m else ""
    count, spam_text, err = _parse_spam(arg_text)
    if err:
        log.info("spam в business-чате %s: неверный формат (%s)", getattr(chat, "id", "?"), err)
        for owner in owners:
            try:
                await context.bot.send_message(owner, _SPAM_HINTS[err],
                                               parse_mode="HTML")
            except Exception as e:
                log.warning("spam hint to %s: %s", owner, e)
        return True
    # Отправка: юзербот в очереди, иначе — Bot API напрямую с паузами.
    await _submit_spam(context, chat.id, count, spam_text, owners[0],
                       True, msg,
                       {"business_connection_id": bc_id} if bc_id else {})
    return True

# TTL / одноразовые сообщения: запас ожидания штатного события удаления
# и потолок планирования (более долгие таймеры не планируем синтетику).
EXPIRY_GRACE_SECONDS = 15
MAX_SCHEDULABLE_TTL = 24 * 3600


# ---------- helpers ----------

def _chat_label(chat) -> str:
    if getattr(chat, "title", None):
        return chat.title
    name = f"{getattr(chat, 'first_name', '') or ''} " \
           f"{getattr(chat, 'last_name', '') or ''}".strip()
    return name or str(getattr(chat, "id", "?"))


def _media_candidates(msg) -> list[tuple[str, str, str]]:
    """Все кандидаты (kind, file_id, ext), крупные первыми.

    Включает платную (закрытую) медиу: у таких сообщений обычные поля
    photo/video могут быть пустыми, файлы лежат в paid_media.
    """
    out: list[tuple[str, str, str]] = []
    try:
        photo = getattr(msg, "photo", None)
        if photo:
            sizes = sorted(photo,
                           key=lambda p: (getattr(p, "file_size", 0) or 0),
                           reverse=True)
            for s in sizes:
                out.append(("photo", s.file_id, ".jpg"))
        if getattr(msg, "video", None):
            out.append(("video", msg.video.file_id, ".mp4"))
        if getattr(msg, "video_note", None):
            out.append(("video_note", msg.video_note.file_id, ".mp4"))
        if getattr(msg, "voice", None):
            out.append(("voice", msg.voice.file_id, ".ogg"))
        if getattr(msg, "audio", None):
            aud = msg.audio
            ext = ".mp3"
            if getattr(aud, "file_name", None):
                ext = os.path.splitext(aud.file_name)[1][:16] or ".mp3"
            out.append(("audio", aud.file_id, ext))
        if getattr(msg, "animation", None):
            out.append(("animation", msg.animation.file_id, ".mp4"))
        doc = getattr(msg, "document", None)
        if doc is not None:
            name = getattr(doc, "file_name", "") or ""
            ext = os.path.splitext(name)[1][:16] or ".bin"
            out.append(("doc", doc.file_id, ext))
        if getattr(msg, "sticker", None):
            stk = msg.sticker
            ext = ".webm" if getattr(stk, "is_video", False) else (".tgs" if getattr(stk, "is_animated", False) else ".webp")
            out.append(("sticker", stk.file_id, ext))
        paid = getattr(msg, "paid_media", None)
        if paid is not None:
            inner = getattr(paid, "paid_media", None)
            if isinstance(inner, (list, tuple)):
                items = list(inner)
            elif inner is not None:
                items = [inner]
            else:
                items = [paid]
            for it in items:
                ph = getattr(it, "photo", None)
                if ph:
                    sizes = (list(ph) if isinstance(ph, (list, tuple)) else [ph])
                    sizes = sorted(sizes,
                                   key=lambda p: (getattr(p, "file_size", 0) or 0),
                                   reverse=True)
                    for s in sizes:
                        out.append(("photo", s.file_id, ".jpg"))
                vd = getattr(it, "video", None)
                if vd is not None and getattr(vd, "file_id", ""):
                    out.append(("video", vd.file_id, ".mp4"))
    except Exception:
        pass
    seen, uniq = set(), []
    for c in out:
        if c[1] and c[1] not in seen:
            seen.add(c[1])
            uniq.append(c)
    return uniq


def _media_info(msg):
    """(kind, file_id, ext) — первый (крупнейший) кандидат, иначе None."""
    cands = _media_candidates(msg)
    return cands[0] if cands else None


def _detect_ephemeral(msg, chat_ttl: int = 0) -> tuple[bool, int, str]:
    """(истекает?, ttl_сек, причина).

    Явного флага 'view once' / ttl у входящего Message в Bot API нет, поэтому:
    - смотрим сырые поля ttl в api_kwargs (ttl_seconds / ttl_period / view_opts),
    - учитываем авто-таймер чата (message_auto_delete_timer_changed), который
      бот ведёт в базе: таймер > 0 значит каждое сообщение самоуничтожится.
    """
    raw = getattr(msg, "api_kwargs", None) or {}
    for key in ("ttl_seconds", "ttl_period", "self_destruct_ttl"):
        val = raw.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return True, int(val), f"таймер TTL {int(val)}с"
    if raw.get("view_once") or raw.get("is_view_once"):
        return True, 0, "одноразовый просмотр"
    if chat_ttl > 0:
        return True, int(chat_ttl), f"автоудаление чата через {int(chat_ttl)}с"
    return False, 0, ""


def _media_ref(msg) -> str:
    """Возвращает 'kind:file_id' для любого медиа, иначе ''."""
    info = _media_info(msg)
    return f"{info[0]}:{info[1]}" if info else ""


def _schedule_media_retry(context, msg, chat_id: int, media_ref: str,
                          owner_id: int, owners, is_eph: bool,
                          delays=(3.0, 15.0, 60.0)):
    """Докачать медиа позже, если при получении файл не отдался.

    Иногда Telegram отдаёт getFile не сразу (правки, платные, ссылка ещё
    не созрела). Пробует через 3с, 15с и минуту; при успехе записывает
    путь в базу и отвечает владельцу.
    """

    async def _later():
        try:
            for d in delays:
                await asyncio.sleep(d)
                try:
                    _, path = await _download_now(context, msg, chat_id)
                except Exception as e:
                    log.warning("медиаретрай %s/%s: %r", chat_id,
                                msg.message_id, e)
                    continue
                if path:
                    try:
                        database.set_media_file(msg.message_id, chat_id, path,
                                                is_ephemeral=1 if is_eph else None)
                    except Exception as e:
                        log.warning("медиаретрай set_media_file: %r", e)
                    log.info("медиаретрай %s/%s: докачано через %sс → %s",
                             chat_id, msg.message_id, int(d), path)
                    note = (f"✅ <b>Медиа досохранена</b> (с {int(d)}-й попытки): "
                            f"<code>{path}</code>")
                    targets = list(owners) if owners else (
                        [owner_id] if owner_id else [])
                    for owner in targets:
                        await _notify(context.bot, owner, note,
                                      media_ref, path)
                    return
            log.warning("медиаретрай %s/%s: так и не удалось скачать",
                        chat_id, msg.message_id)
        except Exception as e:
            log.exception("медиаретрей fall: %r", e)

    try:
        context.application.create_task(
            _later(), name=f"media-retry-{chat_id}-{msg.message_id}")
    except Exception as e:
        log.warning("schedule media retry: %r", e)


def _raw_dump(msg) -> str:
    """Краткий слепок сырого сообщения: что реально прислал Bot API.

    Нужен для диагноза одноразовых фото: если в слепке нет photo/video —
    Telegram не отдал бот медиа, спасти может только юзербот.
    """
    try:
        import json as _json
        d = msg.to_dict() if hasattr(msg, "to_dict") else None
        s = _json.dumps(d, ensure_ascii=False, default=str)
        return s[:800]
    except Exception as e:
        return f"<слепок не удался: {e!r}>"


async def _download_now(context: ContextTypes.DEFAULT_TYPE, msg,
                        chat_id: int) -> tuple[str, str]:
    """Мгновенно скачать медиа. Возвращает (media_ref, local_path).

    Пробует все размеры (крупный → мелкий) и делает второй заход через
    пару секунд, если файл ещё не готов на сервере. media_ref возвращается
    по первому кандидату, даже если скачать не вышло (по нему позже пробуем
    переслать file_id).
    """
    flags = (
        f"spoiler={bool(getattr(msg, 'has_media_spoiler', False))} "
        f"protected={bool(getattr(msg, 'has_protected_content', False))} "
        f"paid={getattr(msg, 'paid_media', None) is not None}"
    )
    cands = _media_candidates(msg)
    if not cands:
        raw = getattr(msg, "api_kwargs", None)
        if (getattr(msg, "has_media_spoiler", False)
                or getattr(msg, "paid_media", None) is not None
                or raw):
            # Похоже на одноразовое/спецсообщение, но Bot API НЕ дал медиа-полей.
            log.warning("медиа %s/%s: медиа-полей у бота нет (%s). Слепок: %s",
                        chat_id, getattr(msg, "message_id", 0), flags,
                        _raw_dump(msg))
        return "", ""
    first_ref = f"{cands[0][0]}:{cands[0][1]}"
    mid = getattr(msg, "message_id", 0)
    for attempt in (1, 2):
        for kind, fid, ext in cands:
            try:
                tg_file = await context.bot.get_file(fid)
            except Exception as e:
                log.warning("getFile %s/%s %s (%s, заход %s): %r",
                            chat_id, mid, fid, flags, attempt, e)
                continue
            try:
                folder = os.path.join(MEDIA_PATH, str(chat_id))
                os.makedirs(folder, exist_ok=True)
                path = os.path.join(folder, f"{mid}_{kind}{ext}")
                await tg_file.download_to_drive(path)
                log.info("медиа сохранена: %s (%s bytes)",
                         path, os.path.getsize(path))
                return f"{kind}:{fid}", path
            except Exception as e:
                log.warning("download %s/%s %s (%s, заход %s): %r",
                            chat_id, mid, fid, flags, attempt, e)
                continue
        if attempt == 1:
            await asyncio.sleep(2)
    log.warning("медиа %s/%s НЕ сохранена (%s): кандидатов %s. Слепок: %s",
                chat_id, mid, flags, len(cands), _raw_dump(msg))
    return first_ref, ""


def _short(text: str, limit: int = 400) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…(обрезано, всего {len(text)})"


def _esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


async def _owners_for_bc_live(bot, bc_id: str) -> list[int]:
    """Только точный владелец подключения (база, иначе живой запрос).

    Широковещательной рассылки всем известным больше нет: ботом пользуются
    разные люди со своей Premium-подпиской, чужие переписки им слать нельзя.
    Неизвестный владелец = пустой список (тихо в лог, без уведомлений).
    """
    if not bc_id:
        return []
    try:
        owner = database.get_business_owner(bc_id)
    except Exception:
        owner = None
    if owner is not None:
        return [owner]
    live = await _resolve_business_owner(bot, bc_id)
    return [live] if live is not None else []


async def _send_safe(bot_func, *args, **kwargs):
    """Вызов метода отправки с защитой от ошибок парсинга HTML."""
    try:
        return await bot_func(*args, **kwargs)
    except Exception as e:
        err_msg = str(e).lower()
        if "parse entities" in err_msg and "parse_mode" in kwargs:
            kwargs_no_html = dict(kwargs)
            kwargs_no_html.pop("parse_mode", None)
            return await bot_func(*args, **kwargs_no_html)
        raise


async def _notify(bot, owner_id: int,
                  text: str, media_ref: str = "", local_path: str = "",
                  is_ephemeral: bool = False):
    """Надёжный отчёт владельцу. Локальный файл — высший приоритет (переживёт
    удаление одноразового медиа на сервере), иначе повтор по file_id, иначе текст."""
    kind = media_ref.split(":", 1)[0] if ":" in media_ref else "doc"
    cap = text[:1000]

    # Попытка 1: отправить локальный сохранённый файл с диска
    if local_path and os.path.exists(local_path):
        try:
            with open(local_path, "rb") as fh:
                if kind == "photo":
                    await _send_safe(bot.send_photo, owner_id, photo=fh, caption=cap, parse_mode="HTML")
                    return
                elif kind == "video":
                    await _send_safe(bot.send_video, owner_id, video=fh, caption=cap, parse_mode="HTML")
                    return
                elif kind == "voice":
                    await _send_safe(bot.send_voice, owner_id, voice=fh, caption=cap, parse_mode="HTML")
                    return
                elif kind == "video_note":
                    await bot.send_video_note(owner_id, video_note=fh)
                    await _send_safe(bot.send_message, owner_id, text=cap, parse_mode="HTML")
                    return
                elif kind == "animation":
                    await _send_safe(bot.send_animation, owner_id, animation=fh, caption=cap, parse_mode="HTML")
                    return
                elif kind == "audio":
                    await _send_safe(bot.send_audio, owner_id, audio=fh, caption=cap, parse_mode="HTML")
                    return
                elif kind == "sticker":
                    await bot.send_sticker(owner_id, sticker=fh)
                    await _send_safe(bot.send_message, owner_id, text=cap, parse_mode="HTML")
                    return
                else:
                    await _send_safe(bot.send_document, owner_id, document=fh, caption=cap, parse_mode="HTML")
                    return
        except Exception as e:
            log.warning("Отправка файла %s владельцу %s не удалась: %s. Пробуем fallback send_document...", local_path, owner_id, e)
            try:
                with open(local_path, "rb") as fh:
                    await bot.send_document(owner_id, document=fh, caption=cap[:900])
                    return
            except Exception as e2:
                log.warning("Fallback send_document %s не удался: %s", local_path, e2)

    # Попытка 2: отправить по file_id (если локального файла не было)
    if media_ref and ":" in media_ref:
        _, fid = media_ref.split(":", 1)
        try:
            if kind == "photo":
                await _send_safe(bot.send_photo, owner_id, photo=fid, caption=cap, parse_mode="HTML")
                return
            elif kind == "video":
                await _send_safe(bot.send_video, owner_id, video=fid, caption=cap, parse_mode="HTML")
                return
            elif kind == "voice":
                await _send_safe(bot.send_voice, owner_id, voice=fid, caption=cap, parse_mode="HTML")
                return
            elif kind == "video_note":
                await bot.send_video_note(owner_id, video_note=fid)
                await _send_safe(bot.send_message, owner_id, text=cap, parse_mode="HTML")
                return
            elif kind == "animation":
                await _send_safe(bot.send_animation, owner_id, animation=fid, caption=cap, parse_mode="HTML")
                return
            elif kind == "audio":
                await _send_safe(bot.send_audio, owner_id, audio=fid, caption=cap, parse_mode="HTML")
                return
            else:
                await _send_safe(bot.send_document, owner_id, document=fid, caption=cap, parse_mode="HTML")
                return
        except Exception as e:
            log.warning("Повтор по file_id %s не удался (файл истёк или удалён): %s", fid, e)

    # Попытка 3: Текстовый отчёт (гарантированная доставка, если медиа не отправилось)
    extra_note = ""
    if local_path and os.path.exists(local_path):
        extra_note = f"\n\n📁 <i>Файл сохранён на диске:</i> <code>{_esc(local_path)}</code>"
    try:
        await _send_safe(bot.send_message, owner_id, text=text + extra_note, parse_mode="HTML")
    except Exception as e:
        log.exception("Критическая ошибка: не удалось отправить текстовый отчёт %s: %s", owner_id, e)
        try:
            await bot.send_message(owner_id, text=f"{text}\n\n[Медиа: {local_path or media_ref}]")
        except Exception:
            pass


def _deleted_report(rec: dict, title: str | None = None) -> str:
    text = _esc(_short(rec.get("text") or "(без текста — только медиа)", 3500))
    has_media = bool(rec.get("media_path") or rec.get("media_file"))
    if title is None:
        if has_media and rec.get("is_ephemeral"):
            title = "🔥 <b>Исчезающее фото / медиа удалено!</b>"
        else:
            title = ("🗑 <b>Удалено сообщение (с медиа)</b>" if has_media
                     else "🗑 <b>Удалено сообщение</b>")
    return (
        f"{title}\n"
        f"👤 Чат: {_esc(rec.get('chat_name', '?'))} (<code>{rec.get('chat_id')}</code>)\n"
        f"✍️ Автор: {_esc(rec.get('sender_name', '?'))} (<code>{rec.get('sender_id')}</code>)\n"
        f"🕒 Отправлено: {_esc(rec.get('date', '?'))}\n\n"
        f"📝 Текст:\n{text}"
    )


def _edited_report(chat_name: str, chat_id: int, sender_name: str,
                   old_text: str, new_text: str) -> str:
    return (
        "✏️ <b>Изменено сообщение</b>\n"
        f"👤 Чат: {_esc(chat_name)} (<code>{chat_id}</code>)\n"
        f"✍️ {_esc(sender_name)}\n\n"
        f"📝 Было:\n{_esc(_short(old_text or '(пусто)', 1500))}\n\n"
        f"📝 Стало:\n{_esc(_short(new_text or '(пусто)', 1500))}"
    )


def _event_line(ev: dict) -> str:
    is_eph = bool(ev.get("is_ephemeral"))
    has_media = bool(ev.get("has_media"))
    if ev["kind"] == "deleted":
        icon = "🔥" if is_eph else "🗑"
        body = f"Было: {_short(ev.get('old_text') or '(пусто)', 300)}"
    elif ev["kind"] == "expired":
        icon = "⏳"
        body = f"Истекло. Было: {_short(ev.get('old_text') or '(пусто)', 300)}"
    else:
        icon = "✏️"
        body = (f"Было: {_short(ev.get('old_text') or '(пусто)', 200)}\n"
                f"Стало: {_short(ev.get('new_text') or '(пусто)', 200)}")

    if is_eph:
        mark = " 🔥📎 [Исчезающее]"
    elif has_media:
        mark = " 📎 [Медиа]"
    else:
        mark = ""
    return (
        f"{icon} <code>{ev['chat_id']}</code> {_esc(ev.get('chat_name') or '?')} | "
        f"{_esc(ev.get('sender_name') or '?')}{mark}\n{_esc(body)}"
    )


# ---------- TTL / одноразовые: контроль истечения ----------

def _schedule_expiry_check(context: ContextTypes.DEFAULT_TYPE,
                           msg_id: int, chat_id: int, ttl_seconds: int,
                           reason: str, owners: list[int],
                           media_ref: str = "", local_path: str = "",
                           owner_id: int = 0):
    """Через ttl секунд проверяет: если штатное событие удаления не пришло
    (Telegram для TTL таковое обычно шлёт, но не гарантированно), записывает
    в журнал событие 'expired' и повторно отправляет владельцу локальную копию.

    Живёт только в памяти процесса — после рестарта бота проверки не выживают.
    """
    if ttl_seconds <= 0 or not owners:
        return
    ttl = min(int(ttl_seconds), MAX_SCHEDULABLE_TTL)

    async def _on_expire():
        await asyncio.sleep(ttl + EXPIRY_GRACE_SECONDS)
        try:
            if database.get_event_kinds(msg_id, chat_id) & {"deleted", "expired"}:
                return  # удаление уже зафиксировано штатным путём — дубли не нужны
            rec = database.get_message(msg_id, chat_id) or {}
            has_media = 1 if (rec.get("media_path") or rec.get("media_file")) else 0
            database.log_event(
                "expired", msg_id, chat_id,
                rec.get("chat_name") or "", rec.get("sender_id"),
                rec.get("sender_name") or "", old_text=rec.get("text") or "",
                date=rec.get("date") or "", has_media=has_media,
                is_ephemeral=1, owner_id=owner_id or rec.get("owner_id") or 0)
            text = _deleted_report(
                rec,
                f"⏳ <b>Сообщение истекло ({_esc(reason)}) — копия уже у тебя</b>")
            for owner in owners:
                await _notify(context.bot, owner, text, media_ref, local_path)
        except Exception as e:
            log.exception("_on_expire %s/%s: %s", chat_id, msg_id, e)

    asyncio.get_running_loop().create_task(_on_expire())


# ---------- Business lifecycle ----------

async def on_business_connection(update: Update,
                                 context: ContextTypes.DEFAULT_TYPE):
    try:
        bc = update.business_connection
        if bc is None:
            return
        database.save_business_connection(bc.id, bc.user.id)
        state = "подключён ✅" if bc.is_enabled else "отключён ❌"
        log.info("business %s: %s (%s)", state, bc.user.id, bc.id)
        if bc.is_enabled:
            await _notify(
                context.bot, bc.user.id,
                "✅ <b>АРХИВ подключён!</b>\n\n"
                "Сохраняю удалённые и изменённые сообщения, фото и видео "
                "из твоих переписок.\n"
                "Удалённое и изменённое будет прилетать сюда.\n\n"
                "/history — история удалений и правок\n"
                "/stats — статистика\n"
                "/spam — отправка нескольких сообщений",
                )
    except Exception as e:
        log.exception("on_business_connection: %s", e)


async def on_deleted_business(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    """Правильное поле — deleted_business_messages (мн.ч.)."""
    try:
        d = update.deleted_business_messages
        if d is None:
            return
        owners = await _owners_for_bc_live(context.bot,
                                             d.business_connection_id)
        if not owners:
            log.warning("удаление без известного owner (bc=%s) — "
                        "отчёт только по владельцу из архива, если есть",
                        d.business_connection_id)
        for mid in d.message_ids or []:
            try:
                rec = database.get_message(mid, d.chat.id)
            except Exception as e:
                log.warning("get_message %s/%s: %s", d.chat.id, mid, e)
                continue
            if rec is None:
                continue
            try:
                database.log_event(
                    "deleted", mid, d.chat.id,
                    rec.get("chat_name") or "", rec.get("sender_id"),
                    rec.get("sender_name") or "",
                    old_text=rec.get("text") or "",
                    date=rec.get("date") or "",
                    has_media=1 if (rec.get("media_path")
                                    or rec.get("media_file")) else 0,
                    owner_id=rec.get("owner_id") or 0)
            except Exception as e:
                log.warning("log_event deleted: %s", e)
            text = _deleted_report(rec)
            media = rec.get("media_path") or ""
            local = rec.get("media_file") or ""
            msg_owners = owners or ([rec["owner_id"]]
                                    if rec.get("owner_id") else [])
            for owner in msg_owners:
                await _notify(context.bot, owner, text, media, local)
    except Exception as e:
        log.exception("on_deleted_business: %s", e)


# ---------- новые сообщения: только сохранить ----------

async def on_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = update.effective_message
        if msg is None:
            return
        # Команды боту в личке — не сохраняем как переписку.
        # (текст клиента в бизнес-чате, даже с '/', сохраняем всегда)
        is_business = bool(getattr(msg, "business_connection_id", None))
        text = msg.text or msg.caption or ""
        if not is_business and text.startswith("/"):
            return
        if getattr(msg, "from_user", None) and msg.from_user.is_bot:
            # свои отчёты/команды не копим
            return
        chat = msg.chat
        if chat is None:
            return
        # /spam текстом в business-чате: клиент не ставит сущность bot_command
        # (в чате с человеком бота нет), поэтому CommandHandler такое не видит —
        # перехватываем здесь, иначе команда молча уйдёт в архив без отправки.
        if is_business and _SPAM_CMD_RE.match(text.strip()):
            log.info("перехват /spam текстом в business-чате %s", chat.id)
            if await _handle_business_spam_text(update, context, msg, chat, text):
                return
            # чужой /spam — сохраняем ниже как обычный текст
        chat_id = chat.id
        user = getattr(msg, "from_user", None)
        sender_id = user.id if user else None
        try:
            sender_name = user.full_name if user else _chat_label(chat)
        except Exception:
            sender_name = str(sender_id)
        chat_name = _chat_label(chat)
        date = msg.date.isoformat(timespec="seconds") if msg.date else ""

        # Точный владелец подключения для тегирования архива и отчётов.
        # Без рассылки всем: ботом пользуются разные люди со своей подпиской.
        owners: list[int] = []
        owner_id = 0
        if is_business:
            owners = await _owners_for_bc_live(
                context.bot, getattr(msg, "business_connection_id", "") or "")
            owner_id = owners[0] if owners else 0

        # Служебное сообщение об изменении авто-таймера чата: обновляем таймер
        # в базе, уведомляем владельца — и не сохраняем как обычное сообщение.
        timer = getattr(msg, "message_auto_delete_timer_changed", None)
        if timer is not None:
            ttl = int(getattr(timer, "message_auto_delete_time", 0) or 0)
            database.set_chat_ttl(chat_id, ttl)
            if is_business:
                note = (f"⏳ <b>Автоудаление в чате {_esc(chat_name)}:</b> таймер "
                        f"<b>{ttl}с</b>. Каждое сообщение качаю при получении — "
                        "истёкшее личным удалением и не дошедшее до меня — попадёт "
                        "в отчёт с копией."
                        if ttl > 0 else
                        f"⏳ <b>Автоудаление в чате {_esc(chat_name)} выключено.</b>")
                for owner in owners:
                    await _notify(context.bot, owner, note)
            return

        # Исчезающее/одноразовое: у Message в Bot API нет явного флага, поэтому
        # сырые ttl-поля + ведённый нами авто-таймер чата.
        # owners/owner_id уже resolved выше.
        is_eph, ttl, reason = _detect_ephemeral(msg, database.get_chat_ttl(chat_id))
        # Мгновенный архив медиа: одноразовое живёт до просмотра, качаем сразу.
        media_ref, local_path = await _download_now(context, msg, chat_id)
        database.save_message(msg.message_id, chat_id, sender_id,
                              sender_name, chat_name, text,
                              media_ref, date, is_ephemeral=1 if is_eph else 0,
                              owner_id=owner_id)
        if local_path:
            try:
                database.set_media_file(msg.message_id, chat_id, local_path,
                                        is_ephemeral=1 if is_eph else None)
            except Exception as e:
                log.warning("set_media_file: %s", e)
        elif media_ref:
            # Файл есть (file_id получен), но скачать сразу не вышло —
            # докачиваем в фоне: 3с / 15с / 1 мин. Особенно ценно для
            # одноразовых: шанс отхватить копию до просмотра/истечения.
            _schedule_media_retry(context, msg, chat_id, media_ref,
                                  owner_id, owners, is_eph)
        # Копию медиа сразу владельцу: для одноразового это может быть
        # единственное событие (удаления/правки может не прийти вовсе).
        if is_business and (media_ref or local_path):
            if is_eph:
                head = (f"🔥 <b>Исчезающее медиа — {_esc(reason)}!</b>\n"
                        if reason else "🔥 <b>Исчезающее медиа!</b>\n")
                tail = ("\n⚡️ Копия сохранена мгновенно — переживёт просмотр "
                        "или самоуничтожение.")
            else:
                head = "📥 <b>Медиа из чата</b>\n"
                tail = "\nСохранено при получении (так ловятся и одноразовые)."
            cap = (
                head
                + f"👤 {_esc(chat_name)} (<code>{chat_id}</code>)\n"
                + f"✍️ {_esc(sender_name)}\n"
                + f"🕒 {_esc(date)}\n"
                + f"📝 {_esc(_short(text or '(без подписи)', 500))}"
                + tail)
            for owner in owners:
                await _notify(context.bot, owner, cap, media_ref, local_path)
        # Контроль истечения: если ttl известен, планируем проверку, что
        # событие удаления реально пришло; иначе — записываем 'expired' сами.
        if is_business and is_eph and ttl:
            _schedule_expiry_check(context, msg.message_id, chat_id, ttl,
                                   reason, owners, media_ref, local_path,
                                   owner_id=owner_id)
    except Exception as e:
        log.exception("on_new: %s", e)


# ---------- правки: сохранить + в журнал + уведомить ----------

async def on_edited(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = update.effective_message
        if msg is None:
            return
        is_business = bool(getattr(msg, "business_connection_id", None))
        text = msg.text or msg.caption or ""
        if not is_business and text.startswith("/"):
            return
        if getattr(msg, "from_user", None) and msg.from_user.is_bot:
            # свои сообщения (меню, отчёты) не архивируем и не отслеживаем
            return
        chat = msg.chat
        if chat is None:
            return
        chat_id = chat.id
        user = getattr(msg, "from_user", None)
        sender_id = user.id if user else None
        try:
            sender_name = user.full_name if user else _chat_label(chat)
        except Exception:
            sender_name = str(sender_id)
        chat_name = _chat_label(chat)
        date = msg.date.isoformat(timespec="seconds") if msg.date else ""

        owners: list[int] = []
        owner_id = 0
        if is_business:
            owners = await _owners_for_bc_live(
                context.bot, getattr(msg, "business_connection_id", "") or "")
            owner_id = owners[0] if owners else 0

        old = database.get_message(msg.message_id, chat_id)
        if old is None:
            # пришло до запуска — «было» неизвестно
            media_ref, local_path = await _download_now(context, msg, chat_id)
            database.save_message(msg.message_id, chat_id, sender_id,
                                  sender_name, chat_name, text,
                                  media_ref, date, owner_id=owner_id)
            if local_path:
                try:
                    database.set_media_file(msg.message_id, chat_id, local_path)
                except Exception as e:
                    log.warning("set_media_file: %s", e)
            database.log_event(
                "edited", msg.message_id, chat_id, chat_name, sender_id,
                sender_name, old_text="(не сохранено — бот был офлайн)",
                new_text=text, date=date,
                has_media=1 if (media_ref or local_path) else 0,
                owner_id=owner_id)
            if is_business:
                for owner in owners:
                    await _notify(
                        context.bot, owner,
                        _edited_report(chat_name, chat_id, sender_name,
                                       "(не сохранено — бот был офлайн)", text))
            return

        if (old.get("text") or "") == (text or ""):
            return  # поменялось только медиа — молчим
        database.update_message_text(msg.message_id, chat_id, text)
        has_media = 1 if (old.get("media_path") or old.get("media_file")
                          or _media_info(msg)) else 0
        if _media_info(msg) and not old.get("media_file"):
            # к правке приложили медиа — докачиваем сразу (вдруг одноразовое)
            _, local_path = await _download_now(context, msg, chat_id)
            if local_path:
                try:
                    database.set_media_file(msg.message_id, chat_id, local_path)
                except Exception as e:
                    log.warning("set_media_file: %s", e)
                has_media = 1
        database.log_event(
            "edited", msg.message_id, chat_id,
            old.get("chat_name") or chat_name, old.get("sender_id"),
            old.get("sender_name") or sender_name,
            old_text=old.get("text") or "", new_text=text,
            date=old.get("date") or date, has_media=has_media,
            owner_id=owner_id or old.get("owner_id") or 0)
        if is_business:
            for owner in owners:
                await _notify(
                    context.bot, owner,
                    _edited_report(old.get("chat_name") or chat_name, chat_id,
                                   old.get("sender_name") or sender_name,
                                   old.get("text") or "", text))
    except Exception as e:
        log.exception("on_edited: %s", e)


# ---------- команды: только history + stats (+ служебный start) ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Текст клиента в бизнес-чате (даже '/start') — не команда боту.
    if update.business_message or update.edited_business_message:
        return
    user_id = update.effective_user.id if update.effective_user else None
    connected = False
    try:
        if user_id is not None:
            connected = user_id in database.get_all_business_owners()
    except Exception:
        connected = False
    text = WELCOME_TEXT + ("" if connected else NOT_CONNECTED_NOTE)
    await update.message.reply_text(text, parse_mode="HTML",
                                    reply_markup=_kb_welcome())


async def cmd_spam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ТЗ: /spam 10 hello — отправить N сообщений от имени пользователя.

    Отправка идёт через юзербота (очередь spam_jobs): Bot API рассылку
    с бота режет лимитами. В личке с самим ботом отключена.
    Текст без сущности bot_command из business-чата перехватывает on_new
    и зовёт _handle_business_spam_text.
    """
    if update.edited_business_message:
        return
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return

    bc_id = getattr(msg, "business_connection_id", "") or ""
    is_business = bool(bc_id)

    # В business-чате спамить может только владелец аккаунта.
    requester_id = msg.from_user.id if msg.from_user else 0
    if is_business:
        owner_id = await _resolve_business_owner(context.bot, bc_id)
        if owner_id is None or requester_id != owner_id:
            # Написал собеседник (или владелец неизвестен) — не спамим,
            # просто сохраняем в архив.
            await on_new(update, context)
            return
        requester_id = owner_id
    elif getattr(chat, "type", None) == "private":
        # Личка с самим ботом: спам здесь отключён — только в чате
        # с собеседником (business) или в группе.
        try:
            await msg.reply_text(
                "⛔ <b>Спам здесь недоступен.</b>\n\n"
                "Команда работает в чате с собеседником "
                "или в группе, где есть бот.\n"
                "Пример: <code>/spam 10 hello</code>",
                parse_mode="HTML")
        except Exception as e:
            log.warning("spam refuse: %s", e)
        return

    async def _tell_owner(text: str):
        """Подсказки: в business-чате — владельцу в лички с ботом
        (чтобы не светить их собеседнику), иначе — ответом в тот же чат."""
        if is_business:
            try:
                await context.bot.send_message(requester_id, text,
                                               parse_mode="HTML")
            except Exception as e:
                log.warning("spam hint to %s: %s", requester_id, e)
        else:
            try:
                await msg.reply_text(text, parse_mode="HTML")
            except Exception as e:
                log.warning("spam hint: %s", e)

    arg_text = " ".join(context.args) if context.args else ""
    count, spam_text, err = _parse_spam(arg_text)
    if err:
        await _tell_owner(_SPAM_HINTS[err])
        return
    send_kwargs = {"business_connection_id": bc_id} if is_business else {}
    await _submit_spam(context, chat.id, count, spam_text, requester_id,
                       is_business, msg, send_kwargs)


async def on_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer()
    except Exception:
        pass
    data = (query.data or "").strip()
    try:
        if data == "menu:main":
            await query.edit_message_text(MAIN_MENU_TEXT, parse_mode="HTML",
                                          reply_markup=_kb_main())
        elif data == "menu:instruction":
            await query.edit_message_text(INSTRUCTION_TEXT, parse_mode="HTML",
                                          reply_markup=_kb_instruction())
        elif data == "menu:commands":
            await query.edit_message_text(COMMANDS_TEXT, parse_mode="HTML",
                                          reply_markup=_kb_commands())
        elif data in ("menu:start", "menu:back"):
            user_id = query.from_user.id if query.from_user else None
            connected = False
            try:
                if user_id is not None:
                    connected = user_id in database.get_all_business_owners()
            except Exception:
                connected = False
            text = WELCOME_TEXT + ("" if connected else NOT_CONNECTED_NOTE)
            await query.edit_message_text(text, parse_mode="HTML",
                                          reply_markup=_kb_welcome())
    except Exception as e:
        log.warning("on_menu_button %s: %s", data, e)


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.business_message or update.edited_business_message:
        return
    try:
        limit = 10
        if context.args:
            for a in context.args:
                if a.isdigit():
                    limit = max(1, min(int(a), 30))
        chat = update.effective_chat
        # В группе — история этого чата, в личке — только своя
        # (ботом пользуются разные люди, общий котёл им видеть нельзя).
        chat_id = None
        owner_id = None
        if chat is not None and chat.type != "private":
            chat_id = chat.id
        else:
            user = update.effective_user
            owner_id = user.id if user else -1
        rows = database.get_recent_events(limit, chat_id, owner_id)
        if not rows:
            await update.message.reply_text(
                "📭 Пока пусто — удалений и правок не зафиксировано.")
            return
        lines = [_event_line(ev) for ev in rows]
        head = (f"📜 <b>Последние {len(rows)}"
                + (f" для чата <code>{chat_id}</code>" if chat_id else "") + ":</b>\n\n")
        text = head + "\n\n".join(lines)
        if len(text) > 4000:
            text = text[:4000] + "\n…"
        await update.message.reply_text(text, parse_mode="HTML")
    except Exception as e:
        log.exception("cmd_history: %s", e)
        await update.message.reply_text("⚠️ Не смог прочитать историю.")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.business_message or update.edited_business_message:
        return
    try:
        chat = update.effective_chat
        # В группе — цифры этого чата, в личке — только свои.
        scope: dict = {}
        scope_note = ""
        if chat is not None and chat.type != "private":
            scope = {"chat_id": chat.id}
            scope_note = f" чата <code>{chat.id}</code>"
        else:
            user = update.effective_user
            scope = {"owner_id": user.id if user else -1}
        total = database.count_messages(**scope)
        n_del = database.count_events("deleted", **scope)
        n_edit = database.count_events("edited", **scope)
        n_exp = database.count_events("expired", **scope)
        chats = database.list_known_chats(**scope)
        top = ""
        if chats:
            top_lines = [f"• {_esc(r['name'])} — <code>{r['chat_id']}</code> ({r['count']})"
                         for r in chats[:5]]
            top = "\n" + "\n".join(top_lines)
        await update.message.reply_text(
            f"📊 <b>Статистика{scope_note}</b>\n"
            f"💾 Сообщений в базе: <b>{total}</b>\n"
            f"👥 Чатов: <b>{len(chats)}</b>\n"
            f"🗑 Удалено: <b>{n_del}</b>\n"
            f"✏️ Изменено: <b>{n_edit}</b>\n"
            f"⏳ Истекло (TTL/одноразовые): <b>{n_exp}</b>{top}",
            parse_mode="HTML")
    except Exception as e:
        log.exception("cmd_stats: %s", e)
        await update.message.reply_text("⚠️ Не смог посчитать статистику.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("update %r caused: %s", update, context.error)


async def _post_init(app: Application):
    try:
        await app.bot.set_my_commands([
            BotCommand("spam", "Отправить N сообщений (/spam 100 привет)"),
            BotCommand("history", "История удалений и правок"),
            BotCommand("stats", "Статистика"),
        ])
        await app.bot.set_my_description(
            "АРХИВ — сохраняет удалённые и изменённые сообщения, фото и видео.")
        await app.bot.set_my_short_description(
            "АРХИВ — сохраняет удалённые и изменённые сообщения, фото и видео.")
    except Exception as e:
        log.warning("set_my_commands: %s", e)


def main():
    if not BOT_TOKEN:
        print("ОШИБКА: нет BOT_TOKEN в файле .env")
        raise SystemExit(1)

    database.init_db(DB_PATH)
    os.makedirs(MEDIA_PATH, exist_ok=True)
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()

    # Команды — первыми, чтобы '/history' в личке не сохранялся как сообщение.
    app.add_handler(CommandHandler(("start", "help"), cmd_start))
    app.add_handler(CommandHandler(SPAM_COMMANDS, cmd_spam))
    app.add_handler(CommandHandler(HISTORY_COMMANDS, cmd_history))
    app.add_handler(CommandHandler(STATS_COMMANDS, cmd_stats))
    app.add_handler(CallbackQueryHandler(on_menu_button, pattern=r"^menu:"))

    app.add_handler(BusinessConnectionHandler(on_business_connection))
    # Важно: именно Deleted (мн.ч.) — в старом коде было ед.ч. и всё падало.
    app.add_handler(BusinessMessagesDeletedHandler(on_deleted_business))

    app.add_handler(MessageHandler(
        filters.UpdateType.MESSAGE
        | filters.UpdateType.CHANNEL_POST
        | filters.UpdateType.BUSINESS_MESSAGE,
        on_new))
    app.add_handler(MessageHandler(
        filters.UpdateType.EDITED_MESSAGE
        | filters.UpdateType.EDITED_CHANNEL_POST
        | filters.UpdateType.EDITED_BUSINESS_MESSAGE,
        on_edited))

    app.add_error_handler(on_error)

    log.info("Бот запущен (polling). Ctrl+C — стоп.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
