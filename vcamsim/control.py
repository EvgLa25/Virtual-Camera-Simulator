"""Loopback control channel between the GUI and the Windows service.

A Windows service has no desktop, so the service owns the engine and the GUI
drives it from outside. This is the wire: a tiny JSON-over-HTTP server bound to
127.0.0.1 only, hosted on the engine's own asyncio loop.

Requests carry a token from `control.token` next to the config, generated on
first run. Loopback binding already keeps other machines out; the token keeps
other *local* processes (and drive-by requests from a browser) out too.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

from .config import resolve_path, to_dict

TOKEN_FILE = "control.token"
HEADER = "X-VCam-Token"


def token_path() -> Path:
    return resolve_path(TOKEN_FILE)


def read_token() -> str:
    try:
        return token_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def ensure_token() -> str:
    """Return the shared token, creating it on first run."""
    tok = read_token()
    if tok:
        return tok
    tok = secrets.token_urlsafe(24)
    p = token_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(tok, encoding="utf-8")
        if os.name == "nt":
            os.chmod(p, 0o600)
    except OSError:
        pass
    return tok


# ============================================================ server side
class ControlServer:
    """Serves engine status and accepts live commands. Loopback only."""

    def __init__(self, runner, port: int, host: str = "127.0.0.1"):
        self.runner = runner
        self.host = host
        self.port = port
        self.server = None
        self._clients = set()
        self._writers = set()
        self.token = ensure_token()

    async def start(self):
        self.server = await asyncio.start_server(self._handle, self.host, self.port)
        self.runner.log(f"control channel on {self.host}:{self.port}")

    async def stop(self):
        if self.server:
            self.server.close()
            for writer in list(self._writers):
                writer.close()
            clients = list(self._clients)
            for task in clients:
                task.cancel()
            if clients:
                await asyncio.gather(*clients, return_exceptions=True)
            try:
                await self.server.wait_closed()
            except Exception:
                pass
            self.server = None

    # ------------------------------------------------------------------
    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        self._clients.add(task)
        self._writers.add(writer)
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            if not line:
                return
            parts = line.decode("latin-1").split()
            if len(parts) < 2:
                return
            method, target = parts[0].upper(), parts[1]
            headers = {}
            for _ in range(40):
                hl = await asyncio.wait_for(reader.readline(), 10)
                if not hl or hl in (b"\r\n", b"\n"):
                    break
                k, _, v = hl.decode("latin-1").partition(":")
                headers[k.strip().lower()] = v.strip()
            body = b""
            try:
                n = int(headers.get("content-length", 0) or 0)
            except ValueError:
                n = 0
            if n > 8 * 1024 * 1024:
                return await self._send(writer, 400, b'{"error":"body too large"}')
            if n > 0:
                body = await asyncio.wait_for(reader.readexactly(n), 20)

            if not secrets.compare_digest(headers.get(HEADER.lower(), ""), self.token):
                return await self._send(writer, 403, b'{"error":"bad token"}')

            path, _, query = target.partition("?")
            args = {}
            for kv in query.split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    args[k] = urlrequest.unquote(v)
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                payload = {}

            status, ctype, data = await self._route(method, path, args, payload)
            await self._send(writer, status, data, ctype)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError,
                ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            self.runner.log(f"control error: {e}")
        finally:
            self._clients.discard(task)
            self._writers.discard(writer)
            try:
                writer.close()
            except Exception:
                pass

    async def _send(self, writer, status: int, data: bytes,
                    ctype: str = "application/json"):
        reason = {200: "OK", 403: "Forbidden", 404: "Not Found",
                  400: "Bad Request"}.get(status, "OK")
        head = (f"HTTP/1.1 {status} {reason}\r\n"
                f"Content-Type: {ctype}\r\n"
                f"Content-Length: {len(data)}\r\n"
                f"Cache-Control: no-store\r\n"
                f"Connection: close\r\n\r\n")
        writer.write(head.encode("latin-1") + data)
        await writer.drain()

    async def _route(self, method, path, args, payload):
        j = lambda o: (200, "application/json", json.dumps(o).encode())  # noqa: E731
        r = self.runner

        if path == "/ping":
            return j({"ok": True, "version": 1, "pid": os.getpid()})
        if path == "/status":
            return j(r.snapshot_status())
        if path == "/logs":
            return j({"lines": r.drain_logs(400)})
        if path == "/config":
            if method == "POST":
                # off the loop: it stops and restarts the engine, which can
                # take many seconds, and the GUI keeps polling meanwhile
                loop = asyncio.get_event_loop()
                ok = await loop.run_in_executor(None, r.replace_config, payload)
                return j({"ok": bool(ok)})
            return j(to_dict(r.cfg))
        if path == "/snapshot":
            data = r.snapshot(args.get("name", ""))
            if not data:
                return 404, "text/plain", b"no snapshot"
            return 200, "image/jpeg", data
        if path == "/faults" and method == "POST":
            ok = r.set_faults(payload.get("name", ""), payload.get("faults", {}))
            return j({"ok": ok})
        if path == "/trigger" and method == "POST":
            ok = r.trigger(payload.get("name", ""), payload.get("kind", "motion"),
                           bool(payload.get("on", True)))
            return j({"ok": ok})
        if path == "/engine" and method == "POST":
            act = payload.get("action", "")
            loop = asyncio.get_event_loop()
            ok = await loop.run_in_executor(None, r.engine_action, act)
            return j({"ok": ok, "action": act})
        return 404, "application/json", b'{"error":"no such endpoint"}'


# ============================================================ client side
class ControlError(Exception):
    pass


class ControlClient:
    """Talks to a running service. Every call is short and fails fast, because
    the GUI polls it on a timer and must never block on a dead service."""

    def __init__(self, port: int, host: str = "127.0.0.1", timeout: float = 2.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.token = read_token()

    @property
    def base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _call(self, path: str, payload=None, raw: bool = False,
              timeout: float | None = None):
        if not self.token:
            self.token = read_token()
        data = json.dumps(payload).encode() if payload is not None else None
        req = urlrequest.Request(self.base + path, data=data, method=
                                 "POST" if data is not None else "GET")
        req.add_header(HEADER, self.token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urlrequest.urlopen(req, timeout=timeout or self.timeout) as r:
                body = r.read()
        except urlerror.HTTPError as e:
            raise ControlError(f"{e.code} {e.reason}") from e
        except Exception as e:
            raise ControlError(str(e)) from e
        if raw:
            return body
        try:
            return json.loads(body or b"{}")
        except json.JSONDecodeError as e:
            raise ControlError("bad response") from e

    # -- convenience ---------------------------------------------------
    def alive(self) -> bool:
        try:
            return bool(self._call("/ping", timeout=0.7).get("ok"))
        except ControlError:
            return False

    def status(self) -> dict:
        return self._call("/status")

    def logs(self) -> list[str]:
        return self._call("/logs").get("lines", [])

    def snapshot(self, name: str) -> bytes:
        try:
            return self._call(f"/snapshot?name={urlrequest.quote(name)}", raw=True)
        except ControlError:
            return b""

    def set_faults(self, name: str, faults: dict) -> bool:
        return bool(self._call("/faults", {"name": name, "faults": faults}).get("ok"))

    def trigger(self, name: str, kind: str, on: bool) -> bool:
        return bool(self._call("/trigger",
                               {"name": name, "kind": kind, "on": on}).get("ok"))

    def engine(self, action: str) -> bool:
        return bool(self._call("/engine", {"action": action},
                               timeout=30).get("ok"))

    def get_config(self) -> dict:
        return self._call("/config")

    def put_config(self, cfg_dict: dict) -> bool:
        # the service stops and restarts its engine to apply a new config,
        # which can take longer than the default 2 s poll timeout
        return bool(self._call("/config", cfg_dict, timeout=60).get("ok"))

    def wait_alive(self, seconds: float = 20.0) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            if self.alive():
                return True
            time.sleep(0.4)
        return False
