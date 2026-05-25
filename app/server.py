"""FastAPI app: serves the board UI and the SSE endpoints."""
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()  # must run before importing modules that read OPENAI_API_KEY at import time

from app import board, sessions  # noqa: E402
from app.config import MAX_ROUNDS  # noqa: E402

log = logging.getLogger("uvicorn.error")

STATIC_DIR = Path(__file__).parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    cleanup_task = asyncio.create_task(sessions.cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()


app = FastAPI(title="AI Tumor Board", lifespan=lifespan)

# CORS: same-origin only by default. Override with MEDBOARD_ALLOWED_ORIGINS
# (comma-separated). Use '*' to allow all origins for public demos.
_origins = os.getenv("MEDBOARD_ALLOWED_ORIGINS", "").strip()
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in _origins.split(",") if o.strip()],
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
    )

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class BoardRequest(BaseModel):
    # max_length caps the case at ~10k chars (~2.5k tokens). A typical
    # vignette is 200-1000 chars; this prevents an attacker from forcing the
    # LLM to chew through a 10MB payload and burn quota.
    case: str = Field(..., min_length=20, max_length=10000)
    max_rounds: int = Field(default=MAX_ROUNDS, ge=1, le=6)


class BoardResponse(BaseModel):
    session_id: str


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(STATIC_DIR / "board.html")


@app.get("/about")
async def about() -> FileResponse:
    return FileResponse(STATIC_DIR / "about.html")


@app.get("/privacy")
async def privacy() -> FileResponse:
    return FileResponse(STATIC_DIR / "privacy.html")


_MAX_ACTIVE_SESSIONS = int(os.getenv("MEDBOARD_MAX_ACTIVE_SESSIONS", "20"))


@app.post("/api/board", response_model=BoardResponse)
async def start_board(req: BoardRequest) -> BoardResponse:
    # Bound the in-memory session count to prevent trivial DoS where an attacker
    # POSTs repeatedly. Count active (not yet finished) sessions only.
    active = sum(1 for s in sessions.SESSIONS.values() if s.finished_at is None)
    if active >= _MAX_ACTIVE_SESSIONS:
        raise HTTPException(
            status_code=503,
            detail=f"Server busy ({active} active sessions). Try again in a few minutes.",
        )
    session = sessions.new_session()
    emit = sessions.emit_factory(session)

    async def _runner() -> None:
        try:
            result = await board.run_board(req.case, emit, max_rounds=req.max_rounds)
            session.final_result = result
        except asyncio.CancelledError:
            emit("error", {"message": "Session cancelled."})
            raise
        except Exception as e:
            log.exception("Board run failed")
            msg = f"{type(e).__name__}: {e}"
            if len(msg) > 240:
                msg = msg[:237] + "..."
            emit("error", {"message": msg})
            session.error = str(e)
        finally:
            session.finished_at = time.time()
            # Sentinel so the stream knows the queue is fully drained.
            try:
                session.queue.put_nowait({"type": "__end__", "payload": {}})
            except asyncio.QueueFull:
                pass

    session.task = asyncio.create_task(_runner())
    return BoardResponse(session_id=session.sid)


@app.get("/api/board/{sid}/stream")
async def stream_board(sid: str) -> StreamingResponse:
    session = sessions.get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown session")
    if session.is_streaming:
        raise HTTPException(
            status_code=409,
            detail="This session is already being streamed by another client.",
        )

    async def event_generator():
        session.is_streaming = True
        try:
            last_ping = time.time()
            while True:
                try:
                    ev = await asyncio.wait_for(session.queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # Heartbeat to keep the connection open through proxies.
                    last_ping = time.time()
                    yield f"event: ping\ndata: {{\"ts\": {last_ping}}}\n\n"
                    continue

                if ev.get("type") == "__end__":
                    break

                payload = json.dumps(ev)
                yield f"data: {payload}\n\n"

                if ev.get("type") in ("final", "error"):
                    # Give the client a moment, then close.
                    break
        finally:
            session.is_streaming = False

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",      # disable nginx buffering if behind one
        },
    )


@app.delete("/api/board/{sid}")
async def cancel_board(sid: str) -> dict:
    session = sessions.get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown session")
    if session.task and not session.task.done():
        session.task.cancel()
    return {"cancelled": True, "session_id": sid}


@app.get("/api/board/{sid}")
async def board_state(sid: str) -> dict:
    session = sessions.get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="Unknown session")
    return {
        "session_id": sid,
        "finished": session.finished_at is not None,
        "final_result": session.final_result,
        "error": session.error,
    }
