import asyncio
import time
from dataclasses import dataclass, field


MAX_ACTIVE_GAME_USERS_PER_GUILD = 6


@dataclass(slots=True)
class GameSessionLease:
    session_id: str
    game_type: str
    guild_id: int
    reserved_user_ids: set[int] = field(default_factory=set)
    active_user_ids: set[int] = field(default_factory=set)
    state: str = "pending"
    expires_at: float | None = None


@dataclass(frozen=True, slots=True)
class SessionReservationResult:
    ok: bool
    code: str = "ok"
    busy_user_ids: tuple[int, ...] = ()
    active_users: int = 0


class GameSessionRegistry:
    """Process-wide reservation registry for interactive game sessions.

    The registry keeps user reservations global across guilds and limits the
    number of active participants in each guild. Its lock protects only
    in-memory index mutations; Discord and database I/O must stay outside it.
    """

    def __init__(
        self,
        *,
        max_active_users_per_guild: int = MAX_ACTIVE_GAME_USERS_PER_GUILD,
    ):
        self.max_active_users_per_guild = max(1, int(max_active_users_per_guild))
        self._sessions: dict[str, GameSessionLease] = {}
        self._user_sessions: dict[int, str] = {}
        self._guild_active_users: dict[int, set[int]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _deadline(ttl: float | None) -> float | None:
        if ttl is None:
            return None
        return time.monotonic() + max(1.0, float(ttl))

    def _release_locked(self, session_id: str) -> GameSessionLease | None:
        lease = self._sessions.pop(str(session_id), None)
        if lease is None:
            return None

        for user_id in tuple(lease.reserved_user_ids):
            if self._user_sessions.get(int(user_id)) == lease.session_id:
                self._user_sessions.pop(int(user_id), None)

        guild_users = self._guild_active_users.get(lease.guild_id)
        if guild_users is not None:
            guild_users.difference_update(lease.active_user_ids)
            if not guild_users:
                self._guild_active_users.pop(lease.guild_id, None)
        return lease

    async def create_pending(
        self,
        *,
        session_id: str,
        game_type: str,
        guild_id: int,
        owner_id: int,
        ttl: float | None,
        required_free_user_ids: set[int] | tuple[int, ...] | list[int] = (),
    ) -> SessionReservationResult:
        sid = str(session_id)
        gid = int(guild_id)
        uid = int(owner_id)
        required_free = {int(user_id) for user_id in required_free_user_ids}
        async with self._lock:
            if sid in self._sessions:
                return SessionReservationResult(False, "session_exists")

            busy = tuple(sorted(
                user_id
                for user_id in ({uid} | required_free)
                if self._user_sessions.get(user_id) is not None
            ))
            if busy:
                return SessionReservationResult(False, "user_busy", busy)

            active_count = len(self._guild_active_users.get(gid, set()))
            if active_count >= self.max_active_users_per_guild:
                return SessionReservationResult(False, "guild_full", active_users=active_count)

            lease = GameSessionLease(
                session_id=sid,
                game_type=str(game_type),
                guild_id=gid,
                reserved_user_ids={uid},
                state="pending",
                expires_at=self._deadline(ttl),
            )
            self._sessions[sid] = lease
            self._user_sessions[uid] = sid
            return SessionReservationResult(True, active_users=active_count)

    async def activate(
        self,
        *,
        session_id: str,
        user_ids: set[int] | tuple[int, ...] | list[int],
        ttl: float | None = None,
    ) -> SessionReservationResult:
        sid = str(session_id)
        requested = {int(user_id) for user_id in user_ids}
        async with self._lock:
            lease = self._sessions.get(sid)
            if lease is None:
                return SessionReservationResult(False, "session_missing")
            if lease.state == "closed":
                return SessionReservationResult(False, "session_closed")

            busy = tuple(sorted(
                user_id
                for user_id in requested
                if self._user_sessions.get(user_id) not in (None, sid)
            ))
            if busy:
                return SessionReservationResult(False, "user_busy", busy)

            guild_users = self._guild_active_users.setdefault(lease.guild_id, set())
            new_active = requested - guild_users
            projected = len(guild_users) + len(new_active)
            if projected > self.max_active_users_per_guild:
                if not guild_users:
                    self._guild_active_users.pop(lease.guild_id, None)
                return SessionReservationResult(False, "guild_full", active_users=len(guild_users))

            for user_id in requested:
                self._user_sessions[user_id] = sid
            lease.reserved_user_ids.update(requested)
            lease.active_user_ids = set(requested)
            lease.state = "active"
            lease.expires_at = self._deadline(ttl)
            guild_users.update(requested)
            return SessionReservationResult(True, active_users=len(guild_users))

    async def touch(self, session_id: str, *, ttl: float | None) -> bool:
        async with self._lock:
            lease = self._sessions.get(str(session_id))
            if lease is None:
                return False
            lease.expires_at = self._deadline(ttl)
            return True

    async def release(self, session_id: str) -> bool:
        async with self._lock:
            return self._release_locked(str(session_id)) is not None

    async def is_user_busy(self, user_id: int, *, except_session_id: str | None = None) -> bool:
        async with self._lock:
            current = self._user_sessions.get(int(user_id))
            return current is not None and current != except_session_id

    async def guild_active_count(self, guild_id: int) -> int:
        async with self._lock:
            return len(self._guild_active_users.get(int(guild_id), set()))

    async def owns_user(self, session_id: str, user_id: int) -> bool:
        async with self._lock:
            return self._user_sessions.get(int(user_id)) == str(session_id)


    async def find_expired(self) -> tuple[str, ...]:
        now = time.monotonic()
        async with self._lock:
            return tuple(
                session_id
                for session_id, lease in self._sessions.items()
                if lease.expires_at is not None and lease.expires_at <= now
            )

    async def cleanup_expired(self) -> tuple[str, ...]:
        now = time.monotonic()
        async with self._lock:
            expired = tuple(
                session_id
                for session_id, lease in self._sessions.items()
                if lease.expires_at is not None and lease.expires_at <= now
            )
            for session_id in expired:
                self._release_locked(session_id)
            return expired
