"""SQLite хранилище: сообщения + журнал событий + Business-подключения + TTL-таймеры чатов."""
import sqlite3
import threading
from datetime import datetime

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()
_db_path = "data.db"

# Допустимые kind в журнале: удалено штатно / изменено / истекло (TTL, view-once)
EVENT_KINDS = ("deleted", "edited", "expired")


def init_db(path: str = "data.db"):
    global _conn, _db_path
    _db_path = path
    # timeout + WAL: базу одновременно пишут два процесса (bot.py и userbot.py)
    _conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
    _conn.execute("PRAGMA journal_mode=WAL;")
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            msg_id    INTEGER NOT NULL,
            chat_id   INTEGER NOT NULL,
            sender_id INTEGER,
            sender_name TEXT DEFAULT '',
            chat_name   TEXT DEFAULT '',
            text        TEXT DEFAULT '',
            media_path  TEXT DEFAULT '',
            date        TEXT DEFAULT '',
            media_file  TEXT DEFAULT '',
            is_ephemeral INTEGER DEFAULT 0,
            owner_id  INTEGER DEFAULT 0,
            PRIMARY KEY (msg_id, chat_id)
        )
    """)
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            kind        TEXT NOT NULL,
            msg_id      INTEGER NOT NULL,
            chat_id     INTEGER NOT NULL,
            chat_name   TEXT DEFAULT '',
            sender_id   INTEGER,
            sender_name TEXT DEFAULT '',
            old_text    TEXT DEFAULT '',
            new_text    TEXT DEFAULT '',
            date        TEXT DEFAULT '',
            created_at  TEXT DEFAULT '',
            has_media   INTEGER DEFAULT 0,
            is_ephemeral INTEGER DEFAULT 0,
            owner_id  INTEGER DEFAULT 0
        )
    """)
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_chat ON events(chat_id, id DESC)")
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind)")
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS business_connections (
            bc_id   TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            date    TEXT DEFAULT ''
        )
    """)
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_ttls (
            chat_id     INTEGER PRIMARY KEY,
            ttl_seconds INTEGER DEFAULT 0,
            updated     TEXT DEFAULT ''
        )
    """)
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            k TEXT PRIMARY KEY,
            v TEXT DEFAULT ''
        )
    """)
    _conn.execute("""
        CREATE TABLE IF NOT EXISTS spam_jobs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    INTEGER NOT NULL,
            msg_count  INTEGER NOT NULL,
            text       TEXT DEFAULT '',
            owner_id   INTEGER DEFAULT 0,
            status     TEXT DEFAULT 'pending',
            sent       INTEGER DEFAULT 0,
            error      TEXT DEFAULT '',
            created_at TEXT DEFAULT '',
            claimed_at TEXT DEFAULT '',
            done_at    TEXT DEFAULT ''
        )
    """)
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_spam_jobs_status ON spam_jobs(status, id)")
    _migrate()
    _conn.commit()


def _migrate():
    """Старые базы: добавить недостающие колонки медиа до их агрессивного использования."""
    cols_msg = [r[1] for r in _conn.execute("PRAGMA table_info(messages)").fetchall()]
    if "media_file" not in cols_msg:
        _conn.execute("ALTER TABLE messages ADD COLUMN media_file TEXT DEFAULT ''")
    if "is_ephemeral" not in cols_msg:
        _conn.execute("ALTER TABLE messages ADD COLUMN is_ephemeral INTEGER DEFAULT 0")
    if "owner_id" not in cols_msg:
        _conn.execute("ALTER TABLE messages ADD COLUMN owner_id INTEGER DEFAULT 0")
    cols_ev = [r[1] for r in _conn.execute("PRAGMA table_info(events)").fetchall()]
    if "has_media" not in cols_ev:
        _conn.execute("ALTER TABLE events ADD COLUMN has_media INTEGER DEFAULT 0")
    if "is_ephemeral" not in cols_ev:
        _conn.execute("ALTER TABLE events ADD COLUMN is_ephemeral INTEGER DEFAULT 0")
    if "owner_id" not in cols_ev:
        _conn.execute("ALTER TABLE events ADD COLUMN owner_id INTEGER DEFAULT 0")


def _ensure():
    if _conn is None:
        init_db(_db_path)


# --- сообщения ---

def save_message(msg_id: int, chat_id: int, sender_id: int | None,
                 sender_name: str, chat_name: str, text: str,
                 media_path: str, date: str, media_file: str = "",
                 is_ephemeral: int = 0, owner_id: int = 0):
    _ensure()
    with _lock:
        _conn.execute("""
            INSERT OR REPLACE INTO messages
            (msg_id, chat_id, sender_id, sender_name, chat_name, text, media_path, date, media_file, is_ephemeral, owner_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (msg_id, chat_id, sender_id, sender_name or "", chat_name or "",
              text or "", media_path or "", date or "", media_file or "",
              1 if is_ephemeral else 0, int(owner_id or 0)))
        _conn.commit()


def set_media_file(msg_id: int, chat_id: int, path: str, is_ephemeral: int | None = None):
    """Локальный путь к скачанному медиа (мгновенный архив, виден после удаления)."""
    _ensure()
    with _lock:
        if is_ephemeral is not None:
            _conn.execute(
                "UPDATE messages SET media_file=?, is_ephemeral=? WHERE msg_id=? AND chat_id=?",
                (path or "", 1 if is_ephemeral else 0, msg_id, chat_id))
        else:
            _conn.execute(
                "UPDATE messages SET media_file=? WHERE msg_id=? AND chat_id=?",
                (path or "", msg_id, chat_id))
        _conn.commit()


def update_message_text(msg_id: int, chat_id: int, new_text: str):
    _ensure()
    with _lock:
        _conn.execute("UPDATE messages SET text=? WHERE msg_id=? AND chat_id=?",
                      (new_text or "", msg_id, chat_id))
        _conn.commit()


def get_message(msg_id: int, chat_id: int) -> dict | None:
    _ensure()
    with _lock:
        cur = _conn.execute(
            "SELECT msg_id, chat_id, sender_id, sender_name, chat_name, text,"
            " media_path, date, media_file, is_ephemeral, owner_id"
            " FROM messages WHERE msg_id=? AND chat_id=?", (msg_id, chat_id))
        row = cur.fetchone()
    if not row:
        return None
    keys = ["msg_id", "chat_id", "sender_id", "sender_name", "chat_name",
            "text", "media_path", "date", "media_file", "is_ephemeral",
            "owner_id"]
    return dict(zip(keys, row))


def count_messages(owner_id: int | None = None,
                  chat_id: int | None = None) -> int:
    _ensure()
    conds, args = [], []
    if owner_id is not None:
        conds.append("owner_id=?")
        args.append(int(owner_id))
    if chat_id is not None:
        conds.append("chat_id=?")
        args.append(chat_id)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    with _lock:
        cur = _conn.execute("SELECT COUNT(*) FROM messages" + where, args)
        return cur.fetchone()[0]


def list_known_chats(owner_id: int | None = None,
                     chat_id: int | None = None) -> list[dict]:
    _ensure()
    conds, args = [], []
    if owner_id is not None:
        conds.append("owner_id=?")
        args.append(int(owner_id))
    if chat_id is not None:
        conds.append("chat_id=?")
        args.append(chat_id)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    with _lock:
        cur = _conn.execute(
            "SELECT chat_id, chat_name, COUNT(*) "
            "FROM messages" + where +
            " GROUP BY chat_id ORDER BY MAX(date) DESC LIMIT 50", args)
        rows = cur.fetchall()
    return [{"chat_id": r[0], "name": r[1] or "?", "count": r[2]} for r in rows]


# --- журнал событий: deleted / edited / expired ---

def log_event(kind: str, msg_id: int, chat_id: int, chat_name: str = "",
              sender_id: int | None = None, sender_name: str = "",
              old_text: str = "", new_text: str = "", date: str = "",
              has_media: int = 0, is_ephemeral: int = 0,
              owner_id: int = 0) -> int:
    """kind: 'deleted' (сообщение удалено) / 'edited' (правка) / 'expired'
    (истекло по TTL или одноразовому просмотру). Возвращает id записи."""
    assert kind in EVENT_KINDS, kind
    _ensure()
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        cur = _conn.execute(
            "INSERT INTO events (kind, msg_id, chat_id, chat_name, sender_id,"
            " sender_name, old_text, new_text, date, created_at, has_media, is_ephemeral, owner_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, msg_id, chat_id, chat_name or "", sender_id,
             sender_name or "", old_text or "", new_text or "", date or "",
             now, 1 if has_media else 0, 1 if is_ephemeral else 0,
             int(owner_id or 0)))
        _conn.commit()
        return cur.lastrowid


def get_recent_events(limit: int = 10, chat_id: int | None = None,
                      owner_id: int | None = None) -> list[dict]:
    limit = max(1, min(int(limit or 10), 30))
    _ensure()
    conds, args = [], []
    if chat_id is not None:
        conds.append("chat_id=?")
        args.append(chat_id)
    if owner_id is not None:
        conds.append("owner_id=?")
        args.append(int(owner_id))
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    with _lock:
        cur = _conn.execute(
            "SELECT id, kind, msg_id, chat_id, chat_name, sender_id,"
            " sender_name, old_text, new_text, date, created_at, has_media, is_ephemeral, owner_id"
            " FROM events" + where + " ORDER BY id DESC LIMIT ?", (*args, limit))
        rows = cur.fetchall()
    keys = ["id", "kind", "msg_id", "chat_id", "chat_name", "sender_id",
            "sender_name", "old_text", "new_text", "date", "created_at",
            "has_media", "is_ephemeral", "owner_id"]
    return [dict(zip(keys, r)) for r in rows]


def get_event_kinds(msg_id: int, chat_id: int) -> set[str]:
    """Какие события уже записаны для сообщения (дедупликация при истечении TTL)."""
    _ensure()
    with _lock:
        cur = _conn.execute(
            "SELECT kind FROM events WHERE msg_id=? AND chat_id=?", (msg_id, chat_id))
        return {r[0] for r in cur.fetchall()}


def count_events(kind: str | None = None,
                 owner_id: int | None = None,
                 chat_id: int | None = None) -> int:
    _ensure()
    conds, args = [], []
    if kind:
        conds.append("kind=?")
        args.append(kind)
    if owner_id is not None:
        conds.append("owner_id=?")
        args.append(int(owner_id))
    if chat_id is not None:
        conds.append("chat_id=?")
        args.append(chat_id)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    with _lock:
        cur = _conn.execute("SELECT COUNT(*) FROM events" + where, args)
        return cur.fetchone()[0]


# --- очередь спама для юзербота (отправка от имени пользователя) ---

def enqueue_spam_job(chat_id: int, count: int, text: str,
                     owner_id: int = 0) -> int:
    """Положить задачу на отправку. Возвращает id задачи."""
    _ensure()
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        cur = _conn.execute(
            "INSERT INTO spam_jobs (chat_id, msg_count, text, owner_id,"
            " status, sent, created_at)"
            " VALUES (?, ?, ?, ?, 'pending', 0, ?)",
            (chat_id, int(count), text or "", int(owner_id or 0), now))
        _conn.commit()
        return cur.lastrowid


def claim_spam_job() -> dict | None:
    """Забрать одну задачу в работу (pending -> sending). Только для демона."""
    _ensure()
    with _lock:
        cur = _conn.execute(
            "SELECT id, chat_id, msg_count, text, owner_id, sent"
            " FROM spam_jobs WHERE status='pending' ORDER BY id LIMIT 1")
        row = cur.fetchone()
        if not row:
            return None
        now = datetime.now().isoformat(timespec="seconds")
        _conn.execute(
            "UPDATE spam_jobs SET status='sending', claimed_at=? WHERE id=?",
            (now, row[0]))
        _conn.commit()
    keys = ["id", "chat_id", "msg_count", "text", "owner_id", "sent"]
    return dict(zip(keys, row))


def update_spam_progress(job_id: int, sent: int):
    _ensure()
    with _lock:
        _conn.execute("UPDATE spam_jobs SET sent=? WHERE id=?",
                      (int(sent), job_id))
        _conn.commit()


def finish_spam_job(job_id: int, status: str, error: str = ""):
    """status: done / failed / expired."""
    assert status in ("done", "failed", "expired"), status
    _ensure()
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        _conn.execute(
            "UPDATE spam_jobs SET status=?, error=?, done_at=? WHERE id=?",
            (status, error or "", now, job_id))
        _conn.commit()


def get_spam_job(job_id: int) -> dict | None:
    _ensure()
    with _lock:
        cur = _conn.execute(
            "SELECT id, chat_id, msg_count, text, owner_id, status, sent,"
            " error, created_at, claimed_at, done_at"
            " FROM spam_jobs WHERE id=?", (job_id,))
        row = cur.fetchone()
    if not row:
        return None
    keys = ["id", "chat_id", "msg_count", "text", "owner_id", "status",
            "sent", "error", "created_at", "claimed_at", "done_at"]
    return dict(zip(keys, row))


def expire_stale_spam_jobs(max_age_minutes: int = 60) -> int:
    """Просрочить висящие задачи (демон был офлайн). Возвращает количество."""
    _ensure()
    with _lock:
        cur = _conn.execute(
            "UPDATE spam_jobs SET status='expired',"
            " error='юзербот был офлайн слишком долго'"
            " WHERE status IN ('pending','sending')"
            " AND datetime(created_at) < datetime('now', ?)",
            (f"-{int(max_age_minutes)} minutes",))
        _conn.commit()
        return cur.rowcount


# --- heartbeat / служебные метки ---

def meta_set(k: str, v: str):
    _ensure()
    with _lock:
        _conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)",
                      (k, v or ""))
        _conn.commit()


def meta_get(k: str) -> str | None:
    _ensure()
    with _lock:
        cur = _conn.execute("SELECT v FROM meta WHERE k=?", (k,))
        row = cur.fetchone()
    return row[0] if row else None


# --- TTL-таймеры чатов (автоудаление: 6с, 10с, 1мин, ...) ---

def set_chat_ttl(chat_id: int, ttl_seconds: int):
    """Обновить авто-таймер чата (приходит в сервисном сообщении об изменении таймера)."""
    _ensure()
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO chat_ttls (chat_id, ttl_seconds, updated)"
            " VALUES (?, ?, ?)",
            (chat_id, int(ttl_seconds) if ttl_seconds > 0 else 0,
             datetime.now().isoformat(timespec="seconds")))
        _conn.commit()


def get_chat_ttl(chat_id: int) -> int:
    """Действующий таймер автоудаления чата в секундах (0 — не задан)."""
    _ensure()
    with _lock:
        cur = _conn.execute(
            "SELECT ttl_seconds FROM chat_ttls WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()
    return int(row[0]) if row else 0


# --- Telegram Business подключения ---

def save_business_connection(bc_id: str, user_id: int):
    _ensure()
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO business_connections (bc_id, user_id, date)"
            " VALUES (?, ?, ?)",
            (bc_id, user_id, datetime.now().isoformat(timespec="seconds")))
        _conn.commit()


def get_business_owner(bc_id: str) -> int | None:
    if not bc_id:
        return None
    _ensure()
    with _lock:
        cur = _conn.execute(
            "SELECT user_id FROM business_connections WHERE bc_id=?", (bc_id,))
        row = cur.fetchone()
    return row[0] if row else None


def get_all_business_owners() -> list[int]:
    """Fallback для удалений: если bc_id протух, шлём всем известным владельцам."""
    _ensure()
    with _lock:
        cur = _conn.execute(
            "SELECT DISTINCT user_id FROM business_connections")
        rows = cur.fetchall()
    return [r[0] for r in rows if r[0] is not None]
