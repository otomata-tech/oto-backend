"""WhatsApp QR pairing — bridge entre le subprocess Node.js (Baileys) et le browser.

Une session par `sub`. `start()` lance le subprocess et un thread bridge qui
pousse les events dans une `asyncio.Queue`. La route SSE `/api/whatsapp/pair/stream`
consomme cette queue et émet en `text/event-stream`.

Statut session :
- `pending` (subprocess démarré, en attente du QR)
- `qr` (au moins un QR émis)
- `paired` (Baileys a rendu connection.open)
- `failed` (timeout, error, ou cancel)

Cleanup auto sur paired/failed après 60s pour laisser le client récupérer le
dernier event si SSE reconnect.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _data_dir() -> Path:
    return Path(os.environ.get("OTO_MCP_DATA_DIR", "/opt/oto-mcp/data"))


def auth_dir_for(sub: str) -> Path:
    return _data_dir() / "whatsapp" / sub


def is_paired(sub: str) -> bool:
    d = auth_dir_for(sub)
    if not d.exists():
        return False
    # Baileys écrit `creds.json` dans le multi-file auth state ; sa présence
    # est un proxy fiable pour "session valide".
    return (d / "creds.json").exists()


@dataclass
class PairingSession:
    sub: str
    session_id: str
    queue: asyncio.Queue
    loop: asyncio.AbstractEventLoop
    status: str = "pending"
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    _thread: Optional[threading.Thread] = None
    _proc_ref: list = field(default_factory=list)  # holds Popen for cancel
    _cancelled: threading.Event = field(default_factory=threading.Event)

    def cancel(self) -> None:
        self._cancelled.set()
        if self._proc_ref:
            proc = self._proc_ref[0]
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass


# session_id → PairingSession ; sub → session_id (un seul actif par user)
_SESSIONS: dict[str, PairingSession] = {}
_BY_SUB: dict[str, str] = {}
_LOCK = threading.Lock()


def get_session(session_id: str) -> Optional[PairingSession]:
    return _SESSIONS.get(session_id)


def get_active_for_sub(sub: str) -> Optional[PairingSession]:
    sid = _BY_SUB.get(sub)
    return _SESSIONS.get(sid) if sid else None


def _drop(session: PairingSession) -> None:
    with _LOCK:
        _SESSIONS.pop(session.session_id, None)
        if _BY_SUB.get(session.sub) == session.session_id:
            _BY_SUB.pop(session.sub, None)


async def _push(session: PairingSession, event: dict) -> None:
    await session.queue.put(event)


def _push_threadsafe(session: PairingSession, event: dict) -> None:
    asyncio.run_coroutine_threadsafe(_push(session, event), session.loop)


def _bridge_subprocess(session: PairingSession) -> None:
    """Run in a worker thread: drives the auth_stream generator into the queue."""
    import subprocess
    from oto.tools.whatsapp.client import SCRIPT
    cmd = [
        "node", str(SCRIPT), "auth",
        "--auth-dir", str(auth_dir_for(session.sub)),
        "--json-events",
    ]
    auth_dir_for(session.sub).mkdir(parents=True, exist_ok=True)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        session._proc_ref.append(proc)

        import json as _json
        for line in proc.stdout or []:
            if session._cancelled.is_set():
                break
            line = line.strip()
            if not line:
                continue
            try:
                evt = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if evt.get("type") == "qr":
                session.status = "qr"
                _push_threadsafe(session, {"type": "qr", "value": evt.get("value")})
            elif evt.get("type") == "result":
                session.status = "paired"
                _push_threadsafe(session, {"type": "paired"})
            elif evt.get("type") == "error":
                session.status = "failed"
                _push_threadsafe(session, {
                    "type": "failed",
                    "error": evt.get("error", "unknown"),
                    "message": evt.get("message", ""),
                })

        # Subprocess finished — make sure we send a terminal event.
        if session.status not in ("paired", "failed"):
            session.status = "failed"
            reason = "cancelled" if session._cancelled.is_set() else "exited"
            _push_threadsafe(session, {"type": "failed", "error": reason})

    except Exception as e:
        session.status = "failed"
        _push_threadsafe(session, {"type": "failed", "error": "bridge_exception", "message": str(e)})
    finally:
        session.finished_at = time.time()
        # Sentinel for SSE consumers — None = stream end.
        _push_threadsafe(session, None)
        # Schedule cleanup after 60s so a reconnecting client can still drain.
        def _delayed_drop():
            time.sleep(60)
            _drop(session)
        threading.Thread(target=_delayed_drop, daemon=True).start()


def start(sub: str, loop: asyncio.AbstractEventLoop) -> PairingSession:
    """Start a fresh pairing session for `sub`. Cancels any existing one."""
    existing = get_active_for_sub(sub)
    if existing and existing.status in ("pending", "qr"):
        existing.cancel()

    session_id = secrets.token_urlsafe(16)
    session = PairingSession(
        sub=sub,
        session_id=session_id,
        queue=asyncio.Queue(),
        loop=loop,
    )
    with _LOCK:
        _SESSIONS[session_id] = session
        _BY_SUB[sub] = session_id

    t = threading.Thread(target=_bridge_subprocess, args=(session,), daemon=True)
    session._thread = t
    t.start()
    return session
