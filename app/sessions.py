"""In-memory session registry for streaming board events."""
import asyncio
import time
import uuid
from dataclasses import dataclass, field

SESSION_TTL_SECONDS = 30 * 60
CLEANUP_INTERVAL_SECONDS = 60


@dataclass
class Session:
    sid: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=512))
    task: asyncio.Task | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    final_result: dict | None = None
    error: str | None = None


SESSIONS: dict[str, Session] = {}


def new_session() -> Session:
    sid = f"tb_{uuid.uuid4().hex[:12]}"
    s = Session(sid=sid)
    SESSIONS[sid] = s
    return s


def get(sid: str) -> Session | None:
    return SESSIONS.get(sid)


def emit_factory(session: Session):
    """Return a closure suitable for board.run_board's `emit` parameter."""
    def _emit(event_type: str, payload: dict) -> None:
        try:
            session.queue.put_nowait({"type": event_type, "payload": payload})
        except asyncio.QueueFull:
            # Drop silently if the client isn't draining fast enough.
            pass
    return _emit


async def cleanup_loop() -> None:
    while True:
        try:
            now = time.time()
            stale = [
                sid
                for sid, s in SESSIONS.items()
                if (s.finished_at and now - s.finished_at > SESSION_TTL_SECONDS)
                or (now - s.started_at > 2 * SESSION_TTL_SECONDS)
            ]
            for sid in stale:
                s = SESSIONS.pop(sid, None)
                if s and s.task and not s.task.done():
                    s.task.cancel()
        except Exception:
            pass
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
