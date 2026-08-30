from __future__ import annotations

import subprocess
import sys

from dune_watch.models import Alert
from dune_watch.notify.base import NotificationSendError


class MacOSNotifyChannel:
    name = "macos_native"

    def send(self, alert: Alert) -> None:
        if sys.platform != "darwin":
            return  # no-op off macOS, e.g. when running on a cloud VM
        script = f"display notification {self._osa_str(alert.body)} with title {self._osa_str(alert.title)}"
        try:
            subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=10)
        except Exception as exc:
            raise NotificationSendError(f"osascript failed: {exc}") from exc

    @staticmethod
    def _osa_str(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
