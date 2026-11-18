"""Best-effort notifications for unattended runs; failures are logged, never raised."""

from __future__ import annotations

import subprocess
import urllib.request

from baton.config import AutoConfig

TIMEOUT = 10.0


def notify(config: AutoConfig, title: str, message: str) -> None:
    """Send title and message (milestone, stop reason, PR URL only) via the configured channel."""
    try:
        if config.notify == "notify-send":
            subprocess.run(
                ["notify-send", "--", title, message],
                check=True,
                capture_output=True,
                timeout=TIMEOUT,
            )
        elif config.notify == "ntfy" and config.ntfy_url:
            request = urllib.request.Request(  # noqa: S310 - scheme validated in config
                config.ntfy_url,
                data=message.encode("utf-8"),
                headers={"Title": title.encode("ascii", "replace").decode("ascii")},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=TIMEOUT):  # noqa: S310
                pass
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Notification via {config.notify} failed: {exc}")
