import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter

from notification_mcp.config import Destination
from notification_mcp.models import EnqueueResult, Notification, NotificationStatus

CHANNEL_ADAPTER = TypeAdapter(Destination)


def timestamp(value: float | None) -> str | None:
    return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else None


def canonical_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Delivery:
    id: str
    notification: Notification
    destination: Destination
    channel_key: str
    attempts: int


class Store:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError(f"Unsupported queue schema version: {version}")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE,
                    payload TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    channel_key TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued','sending','sent','failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    next_attempt_at REAL,
                    sent_at REAL,
                    last_error_code TEXT,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS notifications_due
                    ON notifications(status, next_attempt_at);
                CREATE TABLE IF NOT EXISTS channel_cooldowns (
                    channel_key TEXT PRIMARY KEY,
                    until_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS telegram_bindings (
                    bot_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    update_id INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS telegram_offsets (
                    bot_id TEXT PRIMARY KEY,
                    next_offset INTEGER NOT NULL
                );
                PRAGMA user_version=2;
            """)

    @staticmethod
    def _status(row: sqlite3.Row) -> NotificationStatus:
        return NotificationStatus(
            id=row["id"],
            status=row["status"],
            event=json.loads(row["payload"])["event"],
            channel=row["channel"],
            attempts=row["attempts"],
            created_at=timestamp(row["created_at"]),
            updated_at=timestamp(row["updated_at"]),
            next_attempt_at=timestamp(row["next_attempt_at"]),
            sent_at=timestamp(row["sent_at"]),
            last_error_code=row["last_error_code"],
            last_error=row["last_error"],
        )

    def enqueue(
        self,
        notification: Notification,
        channel: str,
        destination: Destination,
        idempotency_key: str | None,
        now: float,
    ) -> EnqueueResult:
        payload = canonical_json(notification.model_dump(mode="json"))
        destination_json = canonical_json(destination.model_dump(mode="json"))
        channel_key = hashlib.sha256(destination_json.encode()).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                existing = db.execute(
                    "SELECT * FROM notifications WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                if existing:
                    if existing["payload"] != payload:
                        raise ValueError(
                            "Idempotency key already belongs to a different notification"
                        )
                    return EnqueueResult(notification=self._status(existing), deduplicated=True)
            notification_id = uuid4().hex
            db.execute(
                """INSERT INTO notifications
                (id,idempotency_key,payload,channel,destination,channel_key,status,
                 created_at,updated_at,next_attempt_at)
                VALUES (?,?,?,?,?,?,'queued',?,?,?)""",
                (
                    notification_id,
                    idempotency_key,
                    payload,
                    channel,
                    destination_json,
                    channel_key,
                    now,
                    now,
                    now,
                ),
            )
            row = db.execute(
                "SELECT * FROM notifications WHERE id=?", (notification_id,)
            ).fetchone()
            return EnqueueResult(notification=self._status(row), deduplicated=False)

    def telegram_chat(self, bot_id: str) -> str | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT chat_id FROM telegram_bindings WHERE bot_id=?", (bot_id,)
            ).fetchone()
            return row[0] if row else None

    def bind_telegram(
        self, bot_id: str, user_id: str, chat_id: str, *, private: bool, update_id: int
    ) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM telegram_bindings WHERE bot_id=?", (bot_id,)).fetchone()
            if row is None:
                if not private or user_id != chat_id:
                    return False
                db.execute(
                    "INSERT INTO telegram_bindings VALUES (?,?,?,?)",
                    (bot_id, user_id, chat_id, update_id),
                )
                return True
            if row["owner_id"] != user_id or update_id < row["update_id"]:
                return False
            db.execute(
                "UPDATE telegram_bindings SET chat_id=?,update_id=? WHERE bot_id=?",
                (chat_id, update_id, bot_id),
            )
            return True

    def telegram_offset(self, bot_id: str) -> int | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT next_offset FROM telegram_offsets WHERE bot_id=?", (bot_id,)
            ).fetchone()
            return row[0] if row else None

    def save_telegram_offset(self, bot_id: str, offset: int) -> None:
        with self.connection() as db:
            db.execute(
                """INSERT INTO telegram_offsets VALUES (?,?) ON CONFLICT(bot_id)
                DO UPDATE SET next_offset=MAX(next_offset,excluded.next_offset)""",
                (bot_id, offset),
            )

    def status(self, notification_id: str) -> NotificationStatus:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM notifications WHERE id=?", (notification_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Notification not found")
            return self._status(row)

    def recover_interrupted(self, now: float, max_attempts: int) -> None:
        # Only the process holding WorkerLock may recover or consume the queue.
        with self.connection() as db:
            db.execute(
                """UPDATE notifications SET
                status=CASE WHEN attempts < ? THEN 'queued' ELSE 'failed' END,
                next_attempt_at=CASE WHEN attempts < ? THEN ? ELSE NULL END,
                updated_at=?, last_error_code='delivery_interrupted',
                last_error='Delivery was interrupted; the previous outcome is unknown'
                WHERE status='sending'""",
                (max_attempts, max_attempts, now, now),
            )

    def claim(self, now: float) -> Delivery | None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT n.* FROM notifications n
                LEFT JOIN channel_cooldowns c ON c.channel_key=n.channel_key
                WHERE n.status='queued' AND n.next_attempt_at<=?
                  AND COALESCE(c.until_at,0)<=?
                ORDER BY n.next_attempt_at,n.created_at,n.id LIMIT 1""",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                """UPDATE notifications SET status='sending', attempts=attempts+1,
                updated_at=?, next_attempt_at=NULL WHERE id=?""",
                (now, row["id"]),
            )
            return Delivery(
                id=row["id"],
                notification=Notification.model_validate_json(row["payload"]),
                destination=CHANNEL_ADAPTER.validate_json(row["destination"]),
                channel_key=row["channel_key"],
                attempts=row["attempts"] + 1,
            )

    @staticmethod
    def _cooldown(db: sqlite3.Connection, channel_key: str, until: float) -> None:
        db.execute(
            """INSERT INTO channel_cooldowns VALUES (?,?)
            ON CONFLICT(channel_key) DO UPDATE SET until_at=MAX(until_at,excluded.until_at)""",
            (channel_key, until),
        )

    def sent(self, delivery: Delivery, now: float, cooldown: float = 0) -> None:
        with self.connection() as db:
            db.execute(
                """UPDATE notifications SET status='sent', sent_at=?, updated_at=?,
                last_error_code=NULL,last_error=NULL WHERE id=? AND status='sending'""",
                (now, now, delivery.id),
            )
            self._cooldown(db, delivery.channel_key, now + cooldown)

    def failed_attempt(
        self,
        delivery: Delivery,
        now: float,
        code: str,
        message: str,
        next_attempt_at: float | None,
        cooldown_until: float = 0,
    ) -> None:
        with self.connection() as db:
            db.execute(
                """UPDATE notifications SET status=?, updated_at=?,next_attempt_at=?,
                last_error_code=?,last_error=? WHERE id=? AND status='sending'""",
                (
                    "queued" if next_attempt_at is not None else "failed",
                    now,
                    next_attempt_at,
                    code,
                    message,
                    delivery.id,
                ),
            )
            self._cooldown(db, delivery.channel_key, cooldown_until)
