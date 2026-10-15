"""Global Edge HTTP 429 backoff and independent per-guild notice policy."""
from __future__ import annotations

import math
import time


class EdgeRateLimitPolicy:
    # Delays reflect the next probe time; only a successful Edge request resets escalation.
    NOTICE_INTERVAL = 3 * 60 * 60

    def __init__(self, *, first_delay: float = 90):
        first = min(1200, max(30, int(first_delay)))
        self.delays = tuple(min(1200, first * (2 ** index)) for index in range(5))
        self.level = 0
        self.retry_at = 0.0
        self.probing = False
        self.guild_notices = {}  # guild_id: (last_sent_monotonic, last_level)

    def failed_429(self, *, now=None):
        now = time.monotonic() if now is None else now
        # Concurrent connections already in flight may also return 429.
        # Only the first failure, or a failed post-cooldown probe, escalates.
        if self.level and now < self.retry_at and not self.probing:
            return self.delays[self.level - 1], False
        self.level = min(self.level + 1, len(self.delays))
        duration = self.delays[self.level - 1]
        self.retry_at = now + duration
        self.probing = False
        return duration, True

    def success(self):
        if self.level and self.probing:
            self.level = 0
            self.retry_at = 0.0
            self.probing = False
            # Notification cooldown is per guild, independent of an outage.
            # A brief recovery followed by another 429 must not spam Discord.

    def failed_other(self):
        # A failed recovery probe cannot leave the global guard locked forever.
        self.probing = False

    def should_bypass(self, *, now=None):
        if not self.level:
            return False
        now = time.monotonic() if now is None else now
        if now < self.retry_at or self.probing:
            return True
        self.probing = True
        return False

    def remaining_seconds(self, *, now=None):
        now = time.monotonic() if now is None else now
        return max(0, math.ceil(self.retry_at - now))

    def reserve_notice(self, guild_id: int, *, now=None, level=None):
        level = self.level if level is None else level
        if not level or guild_id <= 0:
            return None
        now = time.monotonic() if now is None else now
        previous = self.guild_notices.get(guild_id)
        if previous is not None:
            last_time, last_level = previous
            if level <= last_level and now - last_time < self.NOTICE_INTERVAL:
                return None
        self.guild_notices[guild_id] = (now, level)
        return 'initial' if previous is None else 'followup'

    def undo_notice(self, guild_id: int, *, reserved_at: float, level: int, previous=None):
        if self.guild_notices.get(guild_id) == (reserved_at, level):
            if previous is None:
                self.guild_notices.pop(guild_id, None)
            else:
                self.guild_notices[guild_id] = previous
