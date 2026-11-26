"""Start the local HTTP balancer inside the owning MCP process on first use."""
import errno
import os
import socket
import threading
import time
from pathlib import Path

import httpx
import uvicorn

from balancer.app import create_app

PROJECT = Path(__file__).resolve().parents[1]
START_LOCK = threading.Lock()


def _ready(client):
    try:
        response = client.get("/openapi.json", timeout=1)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return False
    response.raise_for_status()
    if response.json().get("info", {}).get("title") != "A2A_Link balancer":
        raise ValueError("The configured address serves a different application")
    return True


def ensure_balancer(client, address):
    if _ready(client):
        return
    url = httpx.URL(address)
    if (url.scheme != "http" or url.host not in ("127.0.0.1", "localhost")
            or url.path != "/" or url.query):
        raise RuntimeError("Automatic startup requires a local HTTP balancer address")
    with START_LOCK:
        if _ready(client):
            return
        bind_error = None
        listener = socket.socket()
        try:
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("127.0.0.1", url.port or 80))
        except OSError as exc:
            listener.close()
            if exc.errno != errno.EADDRINUSE and getattr(exc, "winerror", None) != 10013:
                raise
            bind_error = exc
            # Another MCP process may already be starting the shared listener.
        else:
            try:
                db = PROJECT / os.environ.get("A2A_LINK_DB", "a2a_link.db")
                server = uvicorn.Server(uvicorn.Config(create_app(str(db)), access_log=False,
                                                       log_config=None))
                threading.Thread(target=server.run, kwargs={"sockets": [listener]},
                                 name="a2a-balancer", daemon=True).start()
            except Exception:
                listener.close()
                raise
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if _ready(client):
                return
            time.sleep(0.1)
        if bind_error is not None:
            raise bind_error
        raise RuntimeError("Balancer did not become ready within five seconds")
