import asyncio
import smtplib
import ssl
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Awaitable, Callable
from uuid import uuid4

from .config import Settings
from .database import Database
from .models import Severity


class AlertService:
    def __init__(self, db: Database, config: Settings, on_change: Callable[[], Awaitable[None]] | None = None):
        self.db = db
        self.config = config
        self._last_email: dict[str, datetime] = {}
        self.on_change = on_change

    async def raise_alarm(
        self, station_id: str, alarm_key: str, severity: Severity, message: str
    ) -> dict:
        now = datetime.now(UTC)
        existing = await self.db.fetch_one(
            "SELECT * FROM alarms WHERE station_id=? AND alarm_key=? AND status='active'",
            (station_id, alarm_key),
        )
        if existing:
            await self.db.execute(
                "UPDATE alarms SET last_at=?, severity=?, message=? WHERE id=?",
                (now.isoformat(), severity.value, message, existing["id"]),
            )
            alarm_id = existing["id"]
        else:
            alarm_id = str(uuid4())
            await self.db.execute(
                """INSERT INTO alarms(id,station_id,alarm_key,severity,message,status,first_at,last_at)
                VALUES(?,?,?,?,?,'active',?,?)""",
                (alarm_id, station_id, alarm_key, severity.value, message, now.isoformat(), now.isoformat()),
            )
        email_key = f"{station_id}:{alarm_key}"
        if now - self._last_email.get(email_key, datetime.min.replace(tzinfo=UTC)) >= timedelta(minutes=5):
            self._last_email[email_key] = now
            await self._send_email(f"[{severity.value.upper()}] 工站告警", f"工站: {station_id}\n{message}")
        await self._notify_change()
        return {"id": alarm_id, "station_id": station_id, "severity": severity, "message": message}

    async def recover(self, station_id: str, alarm_key: str) -> None:
        now = datetime.now(UTC)
        row = await self.db.fetch_one(
            "SELECT * FROM alarms WHERE station_id=? AND alarm_key=? AND status='active'",
            (station_id, alarm_key),
        )
        if not row:
            return
        await self.db.execute(
            "UPDATE alarms SET status='recovered', recovered_at=?, last_at=? WHERE id=?",
            (now.isoformat(), now.isoformat(), row["id"]),
        )
        await self._send_email("[RECOVERED] 工站恢复", f"工站: {station_id}\n已恢复: {row['message']}")
        await self._notify_change()

    async def acknowledge(self, alarm_id: str) -> None:
        await self.db.execute(
            "UPDATE alarms SET status='acknowledged', acknowledged_at=? WHERE id=? AND status='active'",
            (datetime.now(UTC).isoformat(), alarm_id),
        )
        await self._notify_change()

    async def acknowledge_all(self) -> int:
        row = await self.db.fetch_one("SELECT count(*) value FROM alarms WHERE status='active'")
        count = int(row["value"] if row else 0)
        if count:
            await self.db.execute(
                "UPDATE alarms SET status='acknowledged', acknowledged_at=? WHERE status='active'",
                (datetime.now(UTC).isoformat(),),
            )
            await self._notify_change()
        return count

    async def delete(self, alarm_id: str) -> None:
        await self.db.execute("DELETE FROM alarms WHERE id=?", (alarm_id,))
        await self._notify_change()

    async def delete_many(self, alarm_ids: list[str]) -> int:
        unique_ids = list(dict.fromkeys(alarm_ids))
        if not unique_ids:
            return 0
        placeholders = ",".join("?" for _ in unique_ids)
        async with self.db.connect() as conn:
            cursor = await conn.execute(
                f"DELETE FROM alarms WHERE id IN ({placeholders})", tuple(unique_ids)
            )
            await conn.commit()
            deleted = max(0, cursor.rowcount)
        if deleted:
            await self._notify_change()
        return deleted

    async def _notify_change(self) -> None:
        if self.on_change:
            await self.on_change()

    async def _send_email(self, subject: str, body: str) -> None:
        c = self.config
        if not all([c.smtp_host, c.smtp_from, c.alert_email]):
            return
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = c.smtp_from
        msg["To"] = c.alert_email
        msg.set_content(body)

        def send() -> None:
            with smtplib.SMTP_SSL(c.smtp_host, c.smtp_port, context=ssl.create_default_context()) as smtp:
                if c.smtp_username and c.smtp_password:
                    smtp.login(c.smtp_username, c.smtp_password.get_secret_value())
                smtp.send_message(msg)

        try:
            await asyncio.to_thread(send)
        except Exception:
            # Alert delivery must never stop monitoring; failure is visible in server logs.
            return
