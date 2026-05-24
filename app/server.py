"""FastAPI app: serves the board UI and the SSE endpoints."""
import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
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
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class BoardRequest(BaseModel):
    case: str = Field(..., min_length=20)
    max_rounds: int = Field(default=MAX_ROUNDS, ge=1, le=6)


class BoardResponse(BaseModel):
    session_id: str


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(STATIC_DIR / "board.html")


@app.post("/api/board", response_model=BoardResponse)
async def start_board(req: BoardRequest) -> BoardResponse:
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
            emit("error", {"message": f"{type(e).__name__}: {e}"})
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

    async def event_generator():
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
