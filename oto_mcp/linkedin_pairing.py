"""LinkedIn browser-session pairing via VNC.

Lance Xvfb + x11vnc + websockify + Patchright (headed) sur le serveur.
L'utilisateur se logue via un iframe noVNC, le script détecte le login
et persiste le profil browser.

Session lifecycle : pending → vnc_ready → paired → cleanup
                    pending → failed → cleanup
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("oto_mcp.linkedin_pairing")

_DATA_DIR = Path(os.environ.get("OTO_MCP_DATA_DIR", "/opt/oto-mcp/data"))
_NOVNC_WEB = Path("/usr/share/novnc")

DISPLAY_NUM = 98
VNC_PORT = 5998
WS_PORT = 6080
TIMEOUT_S = 300  # 5 min


def profile_dir_for(sub: str) -> Path:
    return _DATA_DIR / "browser-profiles" / f"linkedin-{sub}"


def has_profile(sub: str) -> bool:
    d = profile_dir_for(sub)
    return d.is_dir() and any(d.iterdir())


@dataclass
class LinkedInPairingSession:
    sub: str
    session_id: str
    queue: asyncio.Queue
    loop: asyncio.AbstractEventLoop
    status: str = "pending"
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    _procs: list = field(default_factory=list)
    _cancelled: threading.Event = field(default_factory=threading.Event)
    _thread: Optional[threading.Thread] = None

    def cancel(self) -> None:
        self._cancelled.set()
        _kill_procs(self._procs)


_SESSIONS: dict[str, LinkedInPairingSession] = {}
_BY_SUB: dict[str, str] = {}
_LOCK = threading.Lock()


def get_session(session_id: str) -> Optional[LinkedInPairingSession]:
    return _SESSIONS.get(session_id)


def get_active_for_sub(sub: str) -> Optional[LinkedInPairingSession]:
    sid = _BY_SUB.get(sub)
    return _SESSIONS.get(sid) if sid else None


def _drop(session: LinkedInPairingSession) -> None:
    with _LOCK:
        _SESSIONS.pop(session.session_id, None)
        if _BY_SUB.get(session.sub) == session.session_id:
            _BY_SUB.pop(session.sub, None)


def _push_threadsafe(session: LinkedInPairingSession, event) -> None:
    asyncio.run_coroutine_threadsafe(session.queue.put(event), session.loop)


def _kill_procs(procs: list) -> None:
    for p in reversed(procs):
        if p and p.poll() is None:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try:
                    p.terminate()
                except Exception:
                    pass


def _run_bridge(session: LinkedInPairingSession) -> None:
    """Worker thread: start VNC stack + Patchright, poll for login."""
    display = f":{DISPLAY_NUM}"
    profile = str(profile_dir_for(session.sub))
    Path(profile).mkdir(parents=True, exist_ok=True)
    procs = session._procs
    env = {**os.environ, "DISPLAY": display}

    try:
        # 1. Xvfb
        xvfb = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", "1280x800x24"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )
        procs.append(xvfb)
        time.sleep(1)

        # 2. x11vnc
        vnc = subprocess.Popen(
            ["x11vnc", "-display", display, "-nopw",
             "-listen", "127.0.0.1", "-rfbport", str(VNC_PORT),
             "-shared", "-forever", "-noxdamage"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )
        procs.append(vnc)
        time.sleep(0.5)

        # 3. websockify + noVNC
        ws = subprocess.Popen(
            ["websockify", "--web", str(_NOVNC_WEB),
             str(WS_PORT), f"127.0.0.1:{VNC_PORT}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )
        procs.append(ws)
        time.sleep(0.5)

        # 4. Patchright browser (headed)
        logger.info("Starting Patchright for %s on %s", session.sub, display)
        _run_browser_and_detect(session, profile, env)

    except Exception as e:
        logger.exception("Bridge error for %s", session.sub)
        if session.status not in ("paired", "failed"):
            session.status = "failed"
            _push_threadsafe(session, {"type": "failed", "error": "bridge_exception", "message": str(e)})
    finally:
        session.finished_at = time.time()
        _kill_procs(procs)
        _push_threadsafe(session, None)

        def _delayed_drop():
            time.sleep(60)
            _drop(session)
        threading.Thread(target=_delayed_drop, daemon=True).start()


def _run_browser_and_detect(
    session: LinkedInPairingSession, profile: str, env: dict,
) -> None:
    """Launch Patchright, navigate to LinkedIn login, push vnc_ready, poll for login."""
    import asyncio as _aio

    async def _inner():
        from o_browser import BrowserClient

        client = BrowserClient(
            headless=False,
            profile_path=profile,
            viewport=(1280, 800),
        )
        os.environ["DISPLAY"] = env["DISPLAY"]
        await client.start()

        try:
            await client.goto("https://www.linkedin.com/login")
            await _aio.sleep(2)

            session.status = "vnc_ready"
            _push_threadsafe(session, {"type": "vnc_ready", "ws_port": WS_PORT})

            deadline = time.time() + TIMEOUT_S
            while time.time() < deadline and not session._cancelled.is_set():
                await _aio.sleep(2)
                try:
                    url = client.page.url
                    if "/feed" in url or "/mynetwork" in url:
                        session.status = "paired"
                        _push_threadsafe(session, {"type": "paired"})
                        logger.info("LinkedIn login detected for %s (url)", session.sub)
                        return

                    logged_in = await client.page.evaluate(
                        "!!document.querySelector('.global-nav__me')"
                    )
                    if logged_in:
                        session.status = "paired"
                        _push_threadsafe(session, {"type": "paired"})
                        logger.info("LinkedIn login detected for %s (nav)", session.sub)
                        return
                except Exception:
                    pass

            if session.status not in ("paired", "failed"):
                reason = "cancelled" if session._cancelled.is_set() else "timeout"
                session.status = "failed"
                _push_threadsafe(session, {"type": "failed", "error": reason})
        finally:
            try:
                await client.close()
            except Exception:
                pass

    _aio.run(_inner())


def start(sub: str, loop: asyncio.AbstractEventLoop) -> LinkedInPairingSession:
    existing = get_active_for_sub(sub)
    if existing and existing.status in ("pending", "vnc_ready"):
        existing.cancel()
        time.sleep(1)

    session_id = secrets.token_urlsafe(16)
    session = LinkedInPairingSession(
        sub=sub,
        session_id=session_id,
        queue=asyncio.Queue(),
        loop=loop,
    )
    with _LOCK:
        _SESSIONS[session_id] = session
        _BY_SUB[sub] = session_id

    t = threading.Thread(target=_run_bridge, args=(session,), daemon=True)
    session._thread = t
    t.start()
    return session
