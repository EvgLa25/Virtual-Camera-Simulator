"""RTSP server (OPTIONS/DESCRIBE/SETUP/PLAY/PAUSE/TEARDOWN/GET_/SET_PARAMETER).

Behaves like a real IP camera:
  * one continuous camera timeline per camera -- the video loops forever and
    every client joins the stream at the position it is at *now*, aligned back
    to the previous keyframe, exactly as a real encoder behaves.
  * RTP sequence numbers and timestamps stay monotonic across the loop wrap.
  * parameter sets (VPS/SPS/PPS) are re-sent in front of every keyframe.
  * RTP over UDP and TCP-interleaved, Digest auth, RTCP sender reports,
    session keepalive and idle-session expiry.
"""
from __future__ import annotations

import asyncio
import base64
import random
import socket
import struct
import time

from . import auth
from .rtp import CLOCK, packetize, rtcp_sr, rtp_packet

PT = 96


def _alloc_udp_pair(host: str = "0.0.0.0"):
    for _ in range(200):
        s1 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s1.bind((host, 0))
        except OSError:
            s1.close()
            continue
        p = s1.getsockname()[1]
        if p % 2:
            s1.close()
            continue
        s2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s2.bind((host, p + 1))
        except OSError:
            s1.close()
            s2.close()
            continue
        s1.setblocking(False)
        s2.setblocking(False)
        return s1, s2, p
    raise RuntimeError("no free UDP port pair")


def _pick_transport(header: str) -> str | None:
    """A client may offer several transports separated by commas, in order
    of preference ("RTP/AVP/UDP;..., RTP/AVP/TCP;..."). Take the first one we
    can serve; None if every alternative is multicast or unknown."""
    for alt in header.split(","):
        alt = alt.strip()
        if not alt:
            continue
        spec = alt.split(";", 1)[0].strip().upper()
        if not spec.startswith("RTP/AVP"):
            continue
        if "multicast" in alt.lower():
            continue
        return alt
    return None


class _RtcpProtocol(asyncio.DatagramProtocol):
    """Receives the client's RTCP receiver reports; counts as session activity."""

    def __init__(self, conn):
        self.conn = conn

    def datagram_received(self, data, addr):
        self.conn.touch()

    def error_received(self, exc):
        pass


class Session:
    def __init__(self, conn, profile, variant):
        self.id = f"{random.getrandbits(31):08X}"
        self.conn = conn
        self.profile = profile
        self.variant = variant
        self.pt = PT
        self.ssrc = random.getrandbits(31)
        self.seq = random.getrandbits(15)
        self.ts = random.getrandbits(31)
        self.playing = False
        self.task = None
        self.freeze_at = None
        self.start_frame = 0
        # transport
        self.tcp = True
        self.ch_rtp = 0
        self.ch_rtcp = 1
        self.udp_rtp = None          # raw socket (send only)
        self.rtcp_tr = None          # datagram transport (send + receive)
        self.client_addr = None
        self.client_ports = (0, 0)
        self.server_port = 0

    async def send_rtp(self, pkt: bytes):
        if self.tcp:
            await self.conn.write_interleaved(self.ch_rtp, pkt)
        elif self.udp_rtp and self.client_addr:
            try:
                self.udp_rtp.sendto(pkt, (self.client_addr, self.client_ports[0]))
            except OSError:
                pass

    async def send_rtcp(self, pkt: bytes):
        if self.tcp:
            await self.conn.write_interleaved(self.ch_rtcp, pkt)
        elif self.rtcp_tr and self.client_addr:
            try:
                self.rtcp_tr.sendto(pkt, (self.client_addr, self.client_ports[1]))
            except OSError:
                pass

    def close(self):
        self.playing = False
        if self.task:
            self.task.cancel()
            self.task = None
        if self.udp_rtp:
            try:
                self.udp_rtp.close()
            except OSError:
                pass
            self.udp_rtp = None
        if self.rtcp_tr:
            try:
                self.rtcp_tr.close()
            except Exception:
                pass
            self.rtcp_tr = None


class RtspConnection:
    def __init__(self, server, reader, writer):
        self.srv = server
        self.cam = server.cam
        self.r = reader
        self.w = writer
        self.wlock = asyncio.Lock()
        self.sessions: dict[str, Session] = {}
        self.peer = writer.get_extra_info("peername") or ("?", 0)
        self.last_active = time.time()
        self.closing = False

    def touch(self):
        self.last_active = time.time()

    # ------------------------------------------------------------- plumbing
    async def write_interleaved(self, ch: int, pkt: bytes):
        async with self.wlock:
            try:
                self.w.write(b"$" + bytes([ch]) + struct.pack("!H", len(pkt)) + pkt)
                await self.w.drain()
            except Exception:
                raise asyncio.CancelledError()

    async def _respond(self, code: int, cseq: str, headers: dict | None = None,
                       body: str = ""):
        reason = {200: "OK", 401: "Unauthorized", 404: "Not Found",
                  454: "Session Not Found", 461: "Unsupported Transport",
                  500: "Internal Server Error", 503: "Service Unavailable"}.get(code, "Error")
        h = {"CSeq": cseq, "Server": "VCamSim/1.0",
             "Date": time.strftime("%a, %b %d %Y %H:%M:%S GMT", time.gmtime())}
        h.update(headers or {})
        b = body.encode("utf-8")
        if b:
            h["Content-Length"] = str(len(b))
        out = f"RTSP/1.0 {code} {reason}\r\n"
        out += "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n"
        async with self.wlock:
            self.w.write(out.encode("utf-8") + b)
            await self.w.drain()

    # ---------------------------------------------------------------- loop
    async def run(self):
        wd = asyncio.ensure_future(self._watchdog())
        try:
            while True:
                first = await self.r.readexactly(1)
                self.touch()
                if first == b"$":                      # interleaved from client
                    hdr = await self.r.readexactly(3)
                    ln = struct.unpack("!H", hdr[1:3])[0]
                    await self.r.readexactly(ln)
                    continue
                line = (first + await self.r.readline()).decode("utf-8", "replace").strip()
                if not line:
                    continue
                headers = {}
                while True:
                    hl = (await self.r.readline()).decode("utf-8", "replace").strip()
                    if not hl:
                        break
                    if ":" in hl:
                        k, v = hl.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
                cl = int(headers.get("content-length", 0) or 0)
                if cl:
                    await self.r.readexactly(cl)
                await self._dispatch(line, headers)
        except (asyncio.IncompleteReadError, ConnectionResetError,
                asyncio.CancelledError, BrokenPipeError):
            pass
        except Exception as e:
            self.cam.log(f"RTSP error {self.peer[0]}: {e}")
        finally:
            wd.cancel()
            for s in list(self.sessions.values()):
                if s.playing:
                    self.cam.on_client_change(-1)
                s.close()
            self.sessions.clear()
            try:
                self.w.close()
            except Exception:
                pass

    async def _watchdog(self):
        """Real cameras drop sessions that stop sending keepalives."""
        timeout = self.srv.session_timeout
        if timeout <= 0:
            return
        try:
            while True:
                await asyncio.sleep(15)
                if time.time() - self.last_active > timeout:
                    self.cam.log(f"RTSP session idle {timeout}s, closing {self.peer[0]}")
                    try:
                        self.w.close()
                    except Exception:
                        pass
                    return
        except asyncio.CancelledError:
            pass

    def _profile_for(self, url: str):
        path = url
        if path.startswith("rtsp://"):
            rest = path[7:]
            path = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
        path = path.split("?")[0].rstrip("/")
        for tail in ("/trackID=0", "/trackid=0", "/track1", "/streamid=0"):
            if path.endswith(tail):
                path = path[: -len(tail)]
        low = path.lower()
        for p in self.cam.cfg.profiles:
            if low == p.stream_path.rstrip("/").lower():
                return p
        # tolerate anything else -> main stream (clients often guess the path)
        return self.cam.cfg.profiles[0] if self.cam.cfg.profiles else None

    async def _dispatch(self, line: str, headers: dict):
        parts = line.split()
        if len(parts) < 3:
            return
        method, url = parts[0].upper(), parts[1]
        cseq = headers.get("cseq", "0")
        f = self.cam.cfg.faults
        # keepalives repeat every few seconds per session; at 30 cameras they
        # bury everything else in the log.
        if method not in ("GET_PARAMETER", "SET_PARAMETER", "OPTIONS"):
            self.cam.log(f"RTSP {method} {url} <- {self.peer[0]}")

        # an unplugged camera answers nothing, OPTIONS included
        if f.offline or f.rtsp_offline:
            raise asyncio.CancelledError()

        if method == "OPTIONS":
            return await self._respond(200, cseq, {
                "Public": "OPTIONS, DESCRIBE, SETUP, PLAY, PAUSE, TEARDOWN, "
                          "GET_PARAMETER, SET_PARAMETER"})

        if method not in ("GET_PARAMETER", "SET_PARAMETER"):
            ok = (not f.reject_auth) and auth.check(
                headers.get("authorization"), method,
                self.cam.cfg.username, self.cam.cfg.password, self.srv.nonces)
            if not ok:
                return await self._respond(401, cseq, {
                    "WWW-Authenticate": auth.challenge(self.srv.nonces.issue())})

        if method == "DESCRIBE":
            if f.refuse_describe:
                return await self._respond(503, cseq)
            prof = self._profile_for(url)
            v = self.cam.variants.get(prof.token) if prof else None
            if v is None:
                return await self._respond(404, cseq)
            return await self._respond(200, cseq, {
                "Content-Type": "application/sdp",
                "Content-Base": url.rstrip("/") + "/",
                "Cache-Control": "no-cache"}, self._sdp(v))

        if method == "SETUP":
            prof = self._profile_for(url)
            v = self.cam.variants.get(prof.token) if prof else None
            if v is None:
                return await self._respond(404, cseq)
            tr = _pick_transport(headers.get("transport", ""))
            if tr is None:
                return await self._respond(461, cseq)
            sess = Session(self, prof, v)
            if "TCP" in tr.upper():
                ch = 0
                for tok in tr.split(";"):
                    if tok.strip().lower().startswith("interleaved="):
                        try:
                            ch = int(tok.split("=", 1)[1].split("-")[0])
                        except ValueError:
                            ch = 0
                sess.tcp = True
                sess.ch_rtp, sess.ch_rtcp = ch, ch + 1
                trh = (f"RTP/AVP/TCP;unicast;interleaved={ch}-{ch + 1};"
                       f"ssrc={sess.ssrc:08X};mode=\"PLAY\"")
            else:
                cp = (0, 0)
                for tok in tr.split(";"):
                    if tok.strip().lower().startswith("client_port="):
                        a, _, b = tok.split("=", 1)[1].partition("-")
                        try:
                            cp = (int(a), int(b or int(a) + 1))
                        except ValueError:
                            cp = (0, 0)
                if cp == (0, 0):
                    return await self._respond(461, cseq)
                try:
                    s1, s2, sp = _alloc_udp_pair(self.srv.bind_host)
                except RuntimeError:
                    return await self._respond(500, cseq)
                loop = asyncio.get_event_loop()
                rtcp_tr, _ = await loop.create_datagram_endpoint(
                    lambda: _RtcpProtocol(self), sock=s2)
                sess.tcp = False
                sess.udp_rtp = s1
                sess.rtcp_tr = rtcp_tr
                sess.client_addr = self.peer[0]
                sess.client_ports = cp
                sess.server_port = sp
                trh = (f"RTP/AVP;unicast;client_port={cp[0]}-{cp[1]};"
                       f"server_port={sp}-{sp + 1};ssrc={sess.ssrc:08X};mode=\"PLAY\"")
            self.sessions[sess.id] = sess
            return await self._respond(200, cseq, {
                "Transport": trh,
                "Session": f"{sess.id};timeout={self.srv.announce_timeout}"})

        sid = (headers.get("session", "").split(";")[0]).strip()
        sess = self.sessions.get(sid) or (next(iter(self.sessions.values()))
                                          if self.sessions else None)

        if method == "PLAY":
            if not sess:
                return await self._respond(454, cseq)
            if sess.playing:
                return await self._respond(200, cseq, {
                    "Session": f"{sess.id};timeout={self.srv.announce_timeout}"})
            # join the camera's live timeline at the previous keyframe
            sess.start_frame = self.cam.live_frame(sess.variant, align_keyframe=True)
            await self._respond(200, cseq, {
                "Session": f"{sess.id};timeout={self.srv.announce_timeout}",
                "Range": "npt=now-",
                "RTP-Info": f"url={url};seq={sess.seq};rtptime={sess.ts}"})
            sess.playing = True
            sess.task = asyncio.ensure_future(self._stream(sess))
            self.cam.on_client_change(1)
            return

        if method == "PAUSE":
            if sess:
                if sess.playing:
                    sess.playing = False
                    self.cam.on_client_change(-1)
                # Finish the old producer before a subsequent PLAY starts one.
                if sess.task:
                    sess.task.cancel()
                    await asyncio.gather(sess.task, return_exceptions=True)
                    sess.task = None
            return await self._respond(200, cseq, {"Session": sid} if sid else None)

        if method == "TEARDOWN":
            if sess:
                if sess.playing:
                    self.cam.on_client_change(-1)
                sess.close()
                self.sessions.pop(sess.id, None)
            await self._respond(200, cseq)
            # only drop the connection once its last session is gone: a client
            # that set up main+sub on one socket must keep streaming the other.
            if not self.sessions:
                raise asyncio.CancelledError()
            return

        if method in ("GET_PARAMETER", "SET_PARAMETER"):
            return await self._respond(200, cseq, {"Session": sid} if sid else None)

        return await self._respond(200, cseq)

    # ----------------------------------------------------------------- SDP
    def _sdp(self, v) -> str:
        ip = self.cam.cfg.ip or "0.0.0.0"
        lines = ["v=0", f"o=- {int(time.time())} 1 IN IP4 {ip}",
                 f"s={self.cam.cfg.name}", "i=VCamSim", f"c=IN IP4 {ip}",
                 "t=0 0", "a=tool:VCamSim", "a=control:*", "a=range:npt=now-",
                 f"m=video 0 RTP/AVP {PT}", f"b=AS:{v.bitrate}"]
        if v.codec == "H264":
            pli = v.sps[1:4].hex() if len(v.sps) >= 4 else "4d0028"
            sp = base64.b64encode(v.sps).decode()
            pp = base64.b64encode(v.pps).decode()
            lines += [f"a=rtpmap:{PT} H264/{CLOCK}",
                      f"a=fmtp:{PT} packetization-mode=1;profile-level-id={pli};"
                      f"sprop-parameter-sets={sp},{pp}"]
        else:
            b = base64.b64encode
            lines += [f"a=rtpmap:{PT} H265/{CLOCK}",
                      f"a=fmtp:{PT} sprop-vps={b(v.vps).decode()};"
                      f"sprop-sps={b(v.sps).decode()};sprop-pps={b(v.pps).decode()}"]
        lines += [f"a=framerate:{v.fps}",
                  f"a=x-dimensions:{v.width},{v.height}",
                  "a=recvonly", "a=control:trackID=0"]
        return "\r\n".join(lines) + "\r\n"

    # -------------------------------------------------------------- stream
    async def _stream(self, sess: Session):
        v = sess.variant
        codec = v.codec
        fps = max(1, v.fps)
        inc = CLOCK // fps
        loop = asyncio.get_event_loop()
        start = loop.time()
        n = sess.start_frame          # absolute, monotonic: loops via n % len
        i = 0
        pkts = octets = 0
        last_sr = 0.0
        t0 = time.time()
        f = self.cam.cfg.faults
        try:
            while sess.playing:
                delay = (start + i / fps) - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                elif delay < -1.0:                 # fell behind, resync
                    start = loop.time() - i / fps
                if not sess.playing:
                    break
                if f.offline or f.rtsp_offline:
                    break
                if f.stream_drop_after_sec and (time.time() - t0) > f.stream_drop_after_sec:
                    self.cam.log(f"fault: dropping RTSP stream {sess.id}")
                    break

                if f.freeze_video:
                    if sess.freeze_at is None:
                        sess.freeze_at = n
                    idx = sess.freeze_at
                else:
                    sess.freeze_at = None
                    idx = n

                kf, nals = v.nals(idx)
                if f.bitrate_collapse and not kf:
                    sess.ts = (sess.ts + inc) & 0xFFFFFFFF
                    n += 1
                    i += 1
                    continue

                out: list[bytes] = []
                if kf:                            # re-send parameter sets
                    for ps in (v.vps, v.sps, v.pps):
                        if ps:
                            out += packetize(ps, codec)
                for nal in nals:
                    if f.corrupt_nal_pct and random.random() * 100 < f.corrupt_nal_pct:
                        ba = bytearray(nal)
                        for _ in range(min(8, len(ba))):
                            ba[random.randrange(len(ba))] ^= 0xFF
                        nal = bytes(ba)
                    out += packetize(nal, codec)

                for k, payload in enumerate(out):
                    marker = k == len(out) - 1
                    if f.drop_rtp_pct and random.random() * 100 < f.drop_rtp_pct:
                        sess.seq = (sess.seq + 1) & 0xFFFF
                        continue
                    await sess.send_rtp(rtp_packet(payload, sess.pt, sess.seq,
                                                   sess.ts, sess.ssrc, marker))
                    sess.seq = (sess.seq + 1) & 0xFFFF
                    pkts += 1
                    octets += len(payload)
                    self.cam.tx_bytes += len(payload) + 12

                sess.ts = (sess.ts + inc) & 0xFFFFFFFF
                n += 1
                i += 1
                if time.time() - last_sr > 5:
                    last_sr = time.time()
                    await sess.send_rtcp(rtcp_sr(sess.ssrc, sess.ts, pkts, octets))
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            self.cam.log(f"stream error: {e}")
        finally:
            if sess.playing:
                sess.playing = False
                self.cam.on_client_change(-1)


class RtspServer:
    def __init__(self, cam, bind_host: str, port: int, session_timeout: int = 120):
        self.cam = cam
        self.bind_host = bind_host
        self.port = port
        self.server = None
        self._clients = set()
        self._writers = set()
        self.nonces = auth.NonceStore()
        self.session_timeout = session_timeout
        # what we tell clients in "Session: id;timeout=N" -- never more than
        # the real idle limit, or a client keeping alive every N-5 s would be
        # dropped for doing exactly what we asked
        self.announce_timeout = (min(60, session_timeout) if session_timeout > 0
                                 else 60)

    async def start(self):
        self.server = await asyncio.start_server(
            self._handle, self.bind_host, self.port)
        self.cam.log(f"RTSP listening on {self.bind_host}:{self.port}")

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        self._clients.add(task)
        self._writers.add(writer)
        peer = (writer.get_extra_info("peername") or ("?", 0))[0]
        self.cam.log(f"RTSP TCP connect from {peer}")
        try:
            await RtspConnection(self, reader, writer).run()
        finally:
            self._clients.discard(task)
            self._writers.discard(writer)
            self.cam.log(f"RTSP TCP disconnect {peer}")

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
