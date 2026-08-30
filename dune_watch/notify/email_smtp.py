from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

from dune_watch.models import Alert
from dune_watch.notify.base import NotificationSendError


class EmailSmtpChannel:
    name = "email_smtp"

    def __init__(self, settings: dict):
        self.host = settings.get("smtp_host")
        self.port = int(settings.get("smtp_port", 587))
        self.use_tls = bool(settings.get("use_tls", True))
        self.from_addr = settings.get("from_addr")
        self.to_addrs = settings.get("to_addrs", [])
        username_env_var = settings.get("username_env_var")
        password_env_var = settings.get("password_env_var")
        self.username = os.environ.get(username_env_var) if username_env_var else None
        self.password = os.environ.get(password_env_var) if password_env_var else None

    def send(self, alert: Alert) -> None:
        if not self.host or not self.from_addr or not self.to_addrs:
            raise NotificationSendError("SMTP channel missing host/from_addr/to_addrs configuration")
        if not self.username or not self.password:
            raise NotificationSendError("SMTP credentials not configured (missing env vars)")

        msg = EmailMessage()
        msg["Subject"] = alert.title
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(self.to_addrs)
        msg.set_content(alert.body)

        try:
            with smtplib.SMTP(self.host, self.port, timeout=15) as server:
                if self.use_tls:
                    server.starttls()
                server.login(self.username, self.password)
                server.send_message(msg)
        except Exception as exc:
            raise NotificationSendError(f"SMTP send failed: {exc}") from exc
