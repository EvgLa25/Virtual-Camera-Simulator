"""Phase 3: minimal async HTTP/1.1 server for ONVIF SOAP + snapshot JPEG."""
from __future__ import annotations

import asyncio
import time

from . import auth
from .soap import esc

MAX_HEADERS = 64
MAX_BODY = 2 * 1024 * 1024        # ONVIF requests are tiny; cap them anyway


class HttpServer:
    def __init__(self, cam, bind_host: str, port: int):
        self.cam = cam
        self.bind_host = bind_host
        self.port = port
        self.server = None
        self.nonces = auth.NonceStore()
        self.seen_peers: set[str] = set()
        self._clients = set()
        self._writers = set()

    async def start(self):
        self.server = await asyncio.start_server(self._handle, self.bind_host, self.port)
        self.cam.log(f"HTTP/ONVIF listening on {self.bind_host}:{self.port}")

    async def stop(self):
        if self.server:
            self.server.close()
            for writer in list(self._writers):
                writer.close()
            for task in list(self._clients):
                task.cancel()
            if self._clients:
                await asyncio.gather(*list(self._clients), return_exceptions=True)
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
        peer = (writer.get_extra_info("peername") or ("?", 0))[0]
        if peer not in self.seen_peers:
            self.seen_peers.add(peer)
            self.cam.log(f"HTTP first connection from {peer}")
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                # a request-URI may legally contain spaces when a client is
                # sloppy, so split off the method and the version, not on every
                # space -- 'GET /a b HTTP/1.1' used to drop the connection.
                try:
                    txt = line.decode("latin-1").rstrip("\r\n")
                    if not txt.strip():            # stray CRLF between requests
                        continue
                    method, rest = txt.split(" ", 1)
                    if " " in rest:
                        path, version = rest.rsplit(" ", 1)
                        path = path.strip()
                    else:
                        path, version = rest, "HTTP/1.0"
                except ValueError:
                    return
                method = method.upper()
                # an absolute-form request line (proxies, some SDKs) carries
                # the scheme and host; route on the path part only
                if path.lower().startswith("http://"):
                    tail = path[7:]
                    path = "/" + tail.split("/", 1)[1] if "/" in tail else "/"
                headers = {}
                count = 0
                while True:
                    hl = await reader.readline()
                    if not hl or hl in (b"\r\n", b"\n"):
                        break
                    count += 1
                    if count > MAX_HEADERS:
                        continue                   # drain, but stop keeping them
                    k, _, v = hl.decode("latin-1").partition(":")
                    headers[k.strip().lower()] = v.strip()
                if count > MAX_HEADERS:
                    await self._send(writer, 400, "text/plain",
                                     b"Too many headers", False)
                    return
                if headers.get("expect", "").lower() == "100-continue":
                    # gSOAP and curl wait for this before sending the body
                    writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                    await writer.drain()
                body = b""
                if "chunked" in headers.get("transfer-encoding", "").lower():
                    body = await self._read_chunked(reader)
                    if body is None:
                        await self._send(writer, 400, "text/plain",
                                         b"Request body too large", False)
                        return
                else:
                    try:
                        n = int(headers.get("content-length", 0) or 0)
                    except ValueError:
                        n = 0
                    if n > MAX_BODY:
                        await self._send(writer, 400, "text/plain",
                                         b"Request body too large", False)
                        return
                    if n > 0:
                        body = await reader.readexactly(n)

                conn = headers.get("connection", "").lower()
                if version.upper().startswith("HTTP/1.0"):
                    keep = "keep-alive" in conn   # 1.0 closes unless asked
                else:
                    keep = "close" not in conn
                out = self._head_only(
                    method, await self._route(method, path, body, peer, headers))
                if out is None:
                    return                                    # fault: drop
                status, ctype, data, extra = out
                await self._send(writer, status, ctype, data, keep, extra)
                if not keep:
                    return
        except (asyncio.IncompleteReadError, ConnectionResetError,
                BrokenPipeError, asyncio.CancelledError):
            pass
        except Exception as e:
            self.cam.log(f"HTTP error {peer}: {e}")
        finally:
            self._clients.discard(task)
            self._writers.discard(writer)
            try:
                writer.close()
            except Exception:
                pass

    async def _read_chunked(self, reader):
        """Decode a chunked request body; None if it exceeds MAX_BODY."""
        out = bytearray()
        while True:
            line = await reader.readline()
            if not line:
                raise asyncio.IncompleteReadError(b"", None)
            try:
                size = int(line.split(b";", 1)[0].strip() or b"0", 16)
            except ValueError:
                return None
            if size == 0:
                while True:                        # trailers up to the blank line
                    tl = await reader.readline()
                    if not tl or tl in (b"\r\n", b"\n"):
                        break
                return bytes(out)
            if len(out) + size > MAX_BODY:
                return None
            out += await reader.readexactly(size)
            await reader.readline()                # CRLF after the chunk

    async def _send(self, writer, status: int, ctype: str, data: bytes,
                    keep: bool, extra: dict | None = None):
        reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized",
                  404: "Not Found", 405: "Method Not Allowed",
                  500: "Internal Server Error"}.get(status, "OK")
        h = {"Content-Type": ctype, "Content-Length": str(len(data)),
             "Server": "VCamSim/1.0",
             "Date": time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()),
             "Connection": "keep-alive" if keep else "close"}
        h.update(extra or {})
        head = f"HTTP/1.1 {status} {reason}\r\n" + \
               "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n"
        writer.write(head.encode("latin-1") + data)
        await writer.drain()

    @staticmethod
    def _head_only(method: str, out):
        """HEAD gets the headers of the GET without the body."""
        if method == "HEAD" and out is not None:
            status, ctype, data, extra = out
            extra = dict(extra or {})
            extra["Content-Length"] = str(len(data))
            return status, ctype, b"", extra
        return out

    # ------------------------------------------------------------------
    async def _route(self, method: str, path: str, body: bytes, peer: str,
                     headers: dict):
        f = self.cam.cfg.faults
        if f.offline or f.onvif_offline:
            return None
        p = path.split("?")[0]

        if p == "/snapshot" or p.endswith("/snapshot.jpg"):
            # digest HA2 is computed over the request method; a HEAD probe
            # (some VMS check the URI before using it) must verify as HEAD
            ok = (not f.reject_auth) and auth.check(
                headers.get("authorization"), method,
                self.cam.cfg.username, self.cam.cfg.password, self.nonces)
            if not ok:
                return (401, "text/plain", b"Unauthorized",
                        {"WWW-Authenticate": auth.challenge(self.nonces.issue())})
            jpg = self.cam.snapshot()
            if not jpg:
                return 404, "text/plain", b"no snapshot", None
            self.cam.log(f"HTTP snapshot <- {peer}")
            return 200, "image/jpeg", jpg, {"Cache-Control": "no-store"}

        if p.startswith("/onvif"):
            if method in ("GET", "HEAD"):
                return 200, "text/plain", b"VCamSim ONVIF", None
            if method != "POST":
                return 405, "text/plain", b"Method Not Allowed", {"Allow": "POST, GET"}
            res = await self.cam.onvif.handle(
                path, body, peer, headers.get("authorization"), self.nonces)
            if res is None:
                return None
            return res

        if p == "/":
            c = self.cam.cfg
            e = esc
            rtsp = self.cam.rtsp_url(c.profiles[0]) if c.profiles else "-"
            html = (f"<html><head><meta charset='utf-8'><title>{e(c.name)}</title>"
                    f"</head><body style='font:14px system-ui;margin:2rem'>"
                    f"<h3>{e(c.name)}</h3>"
                    f"<p>{e(c.manufacturer)} {e(c.model)} / {e(c.serial)}</p>"
                    f"<p>ONVIF: {e(self.cam.base_url)}/onvif/device_service</p>"
                    f"<p>RTSP: {e(rtsp)}</p>"
                    f"</body></html>").encode("utf-8")
            return 200, "text/html; charset=utf-8", html, None

        return 404, "text/plain", b"Not Found", None
