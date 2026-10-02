"""Phases 4/7: camera runtime + engine (asyncio loop on its own thread)."""
from __future__ import annotations

import asyncio
import queue
import socket
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from . import firewall, identity, media
from .addressing import AddressAllocator
from .config import AppCfg, CameraCfg, resolve_path
from .discovery import DiscoveryResponder
from .httpd import HttpServer
from .onvif_svc import OnvifService
from .rtsp import RtspServer


class CameraRuntime:
    def __init__(self, cfg: CameraCfg, engine: "Engine"):
        self.cfg = cfg
        self.engine = engine
        self.variants: dict[str, media.Variant] = {}
        self.snaps: media.SnapshotSet | None = None
        self.onvif = OnvifService(self)
        self.http: HttpServer | None = None
        self.rtsp: RtspServer | None = None
        self.motion_task = None
        self.clients = 0
        self.state = "stopped"
        self.error = ""
        # monotonic: an NTP step or a manual clock change must not make every
        # stream jump or run backwards
        self.t0 = time.monotonic()
        self.tx_bytes = 0          # RTP payload sent, for the live throughput meter

    # ------------------------------------------------------------- helpers
    def log(self, msg: str):
        self.engine.log(msg, self.cfg.name)

    def now(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=self.cfg.faults.clock_drift_sec)

    @property
    def base_url(self) -> str:
        p = self.cfg.onvif_port
        host = self.cfg.ip or "127.0.0.1"
        return f"http://{host}" + ("" if p == 80 else f":{p}")

    def rtsp_url(self, profile, credentials: bool | None = None) -> str:
        host = self.cfg.ip or "127.0.0.1"
        port = "" if self.cfg.rtsp_port == 554 else f":{self.cfg.rtsp_port}"
        want = self.cfg.stream_uri_credentials if credentials is None else credentials
        if want:
            u = quote(self.cfg.username, safe="")
            p = quote(self.cfg.password, safe="")
            host = f"{u}:{p}@{host}"
        return f"rtsp://{host}{port}{profile.stream_path}"

    def live_frame(self, variant, align_keyframe: bool = True) -> int:
        """Absolute frame index of the camera's continuous timeline right now.

        The counter never resets, so `index % len(frames)` loops the video for
        ever while RTP timestamps stay monotonic."""
        pos = int((time.monotonic() - self.t0) * variant.fps)
        if pos < 0:
            pos = 0
        if not align_keyframe:
            return pos
        n = len(variant.frames)
        return (pos // n) * n + variant.prev_keyframe(pos % n)

    def snapshot(self) -> bytes:
        if not self.snaps:
            return b""
        return self.snaps.get(int(time.monotonic() - self.t0))

    def on_client_change(self, delta: int):
        self.clients = max(0, self.clients + delta)
        self.engine.notify()

    # -------------------------------------------------------------- media
    def prepare_media(self, cache: Path, abort=None, on_progress=None):
        if not self.cfg.video:
            raise RuntimeError("no video file set for this camera")
        # A service starts with CWD=C:\Windows\System32, so a relative video
        # path has to resolve against the program folder like media_dir does.
        src = str(resolve_path(self.cfg.video))
        self.cfg.ensure_profiles()
        secs = max(0, self.cfg.import_seconds)
        n = len(self.cfg.profiles)
        for i, p in enumerate(self.cfg.profiles):
            # fold each profile's own 0..1 progress into this camera's share
            def step(f, _i=i):
                if on_progress:
                    on_progress((_i + f) / (n + 1), p.name)
            try:
                es, idx = media.import_variant(src, cache, p, log=self.log,
                                               abort=abort, seconds=secs,
                                               on_progress=step)
            except media.SourceMissing:
                hint = ""
                if self.engine.in_service and len(src) > 1 and src[1] == ":" \
                        and not Path(src[:3]).exists():
                    hint = (" -- a Windows service runs as LocalSystem and cannot "
                            "see mapped network drives; use a UNC path or copy "
                            "the file next to VCamSim")
                elif self.engine.in_service and "\\Users\\" in src:
                    hint = (" -- make sure the service account can read that "
                            "user folder, or copy the file next to VCamSim")
                raise RuntimeError(f"video not found: {self.cfg.video}{hint}")
            self.variants[p.token] = media.get_variant(es, idx)
        if on_progress:
            on_progress(n / (n + 1), "snapshots")
        p0 = self.cfg.profiles[0]
        w = min(p0.width, 1280)
        h = max(2, int(p0.height * (w / max(1, p0.width))) // 2 * 2)
        self.snaps = media.get_snapshots(
            media.import_snapshots(src, cache, w, h, log=self.log, abort=abort,
                                   seconds=secs))

    # ------------------------------------------------------------ servers
    async def start(self, bind_host: str, retries: int = 0):
        """Bind both listeners. `retries` > 0 re-tries a failed bind once a
        second: an IP alias netsh has just added sits in the *tentative* state
        for a few seconds and refuses binds until duplicate-address detection
        finishes."""
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                self.http = HttpServer(self, bind_host, self.cfg.onvif_port)
                await self.http.start()
                self.rtsp = RtspServer(self, bind_host, self.cfg.rtsp_port,
                                       self.engine.cfg.rtsp_session_timeout_sec)
                await self.rtsp.start()
                self.motion_task = asyncio.ensure_future(
                    self.onvif.events.auto_motion_loop())
                self.state = "running"
                self.error = ""
                self.t0 = time.monotonic()
                return
            except Exception as e:               # OSError, or anything odd
                last = e
                await self._close_servers()
                if attempt < retries and self.engine._stopping.is_set() is False:
                    if attempt == 0:
                        self.log(f"bind failed ({e}); retrying for up to "
                                 f"{retries}s")
                    await asyncio.sleep(1.0)
                    continue
                break
        self.state = "error"
        self.error = _explain_oserror(last)
        self.log(f"start failed: {self.error}")
        await self.stop()

    async def _close_servers(self):
        for s in (self.http, self.rtsp):
            if s:
                try:
                    await s.stop()
                except Exception:
                    pass
        self.http = self.rtsp = None

    async def stop(self):
        if self.motion_task:
            self.motion_task.cancel()
            self.motion_task = None
        await self._close_servers()
        self.onvif.events.subs.clear()
        if self.state != "error":
            self.state = "stopped"
        self.clients = 0


def _explain_oserror(e: Exception | None) -> str:
    """Turn the usual WinSock failures into something a user can act on."""
    if e is None:
        return "unknown error"
    err = getattr(e, "errno", None) or getattr(e, "winerror", None)
    txt = str(e)
    if err in (10048, 98) or "address already in use" in txt.lower():
        return f"port already in use ({txt}) -- change the base port in Settings"
    if err in (10013, 13):
        return (f"permission denied binding the port ({txt}) -- ports below "
                f"1024 (554, 80) need Administrator, or another program "
                f"reserved the port")
    if err in (10049, 99):
        return (f"the address is not available on this machine ({txt}) -- "
                f"check advertise_ip / the alias base IP and NIC name")
    return txt


class Engine:
    """Owns the asyncio loop, the cameras and the whole start/stop lifecycle.

    Everything expensive -- probing ffmpeg, transcoding, indexing -- happens on
    the engine thread, never on the caller's, so a GUI stays responsive while a
    first run transcodes.  Callers watch `phase` / `progress` for feedback.
    """

    def __init__(self, cfg: AppCfg, log_cb=None):
        self.cfg = cfg
        self.cams: list[CameraRuntime] = []
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.alloc: AddressAllocator | None = None
        self.discovery: DiscoveryResponder | None = None
        self.logs: queue.Queue = queue.Queue(maxsize=5000)
        self.log_cb = log_cb
        self.running = False
        self._dirty = threading.Event()
        # lifecycle
        self._stopping = threading.Event()     # "abort as soon as you can"
        self._loop_ready = threading.Event()   # the loop is up and reachable
        self._stop_evt: asyncio.Event | None = None
        self.phase = "idle"                    # idle|preparing|starting|running|stopping
        self.progress = (0, 0)                 # (done, total) during preparing
        self.item_progress = (0.0, "")         # (0..1, label) within one camera
        self._tx_mark = (time.time(), 0)
        self.in_service = False                # set by the daemon; shapes hints

    # ---------------------------------------------------------------- log
    def log(self, msg: str, who: str = "engine"):
        line = f"{time.strftime('%H:%M:%S')} [{who}] {msg}"
        try:
            self.logs.put_nowait(line)
        except queue.Full:
            pass
        if self.log_cb:
            try:
                self.log_cb(line)
            except Exception:
                pass

    def drain_logs(self, limit: int = 500) -> list[str]:
        out = []
        while len(out) < limit:
            try:
                out.append(self.logs.get_nowait())
            except queue.Empty:
                break
        return out

    def notify(self):
        self._dirty.set()

    def take_dirty(self) -> bool:
        d = self._dirty.is_set()
        self._dirty.clear()
        return d

    # -------------------------------------------------------------- state
    def running_cameras(self) -> list[CameraRuntime]:
        return [c for c in self.cams if c.state == "running"]

    def status(self) -> list[dict]:
        return [{
            "name": c.cfg.name, "ip": c.cfg.ip,
            "onvif": c.cfg.onvif_port, "rtsp": c.cfg.rtsp_port,
            "state": c.state, "clients": c.clients,
            "subs": len(c.onvif.events.subs),
            "faults": c.cfg.faults.any_active(),
            "error": c.error,
            "profiles": len(c.cfg.profiles),
        } for c in self.cams]

    def camera(self, name: str) -> CameraRuntime | None:
        for c in self.cams:
            if c.cfg.name == name:
                return c
        return None

    def stats(self) -> dict:
        """Aggregates for the dashboard, including live RTP throughput."""
        total = sum(c.tx_bytes for c in self.cams)
        now = time.time()
        t0, b0 = self._tx_mark
        dt = now - t0
        rate = (total - b0) / dt if dt >= 0.25 else None
        if rate is not None:
            self._tx_mark = (now, total)
        return {
            "total": len(self.cams),
            "running": sum(1 for c in self.cams if c.state == "running"),
            "error": sum(1 for c in self.cams if c.state == "error"),
            "clients": sum(c.clients for c in self.cams),
            "subs": sum(len(c.onvif.events.subs) for c in self.cams),
            "faults": sum(1 for c in self.cams if c.cfg.faults.any_active()),
            "mbps": (rate * 8 / 1e6) if rate is not None else None,
            "phase": self.phase,
            "progress": self.progress,
            "item_progress": self.item_progress,
        }

    # -------------------------------------------------------------- start
    def start(self):
        """Returns immediately -- all the slow work happens on the thread."""
        if self.running:
            return
        if self.thread and self.thread.is_alive():
            # the previous run is still tearing down (a slow transcode being
            # killed, sockets draining); starting a second loop now would
            # fight it for the ports and the mmaps
            raise RuntimeError("the engine is still stopping -- try again in a moment")
        for note in self.cfg.normalize():
            self.log(f"config repaired: {note}")
        cams = [c for c in self.cfg.cameras if c.enabled]
        if not cams:
            raise RuntimeError("no enabled cameras to start")
        names = [c.name for c in cams]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            # identity (UUID/MAC/serial) is derived from the name: two cameras
            # with one name would be one device to a VMS
            raise RuntimeError("duplicate camera names: " + ", ".join(dup))
        for c in cams:
            identity.fill(c)
            c.ensure_profiles()
        self.cams = [CameraRuntime(c, self) for c in cams]
        for rt in self.cams:
            rt.state = "pending"

        self._stopping.clear()
        self._loop_ready.clear()
        self.phase = "preparing"
        self.progress = (0, len(self.cams))
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="vcamsim-engine")
        self.thread.start()

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._stop_evt = asyncio.Event()
        self._loop_ready.set()
        crashed = False
        try:
            self.loop.run_until_complete(self._main())
        except Exception as e:
            crashed = True
            self.log(f"engine loop error: {e!r}")
        finally:
            self._shutdown_loop()
            if crashed:
                # stop() never ran, so nothing else will give the alias IPs
                # back or mark the cameras as down
                for c in self.cams:
                    if c.state == "running":
                        c.state = "error"
                        c.error = c.error or "engine loop crashed"
                if self.alloc:
                    try:
                        self.alloc.release()
                    except Exception:
                        pass
                    self.alloc = None
            self.phase = "idle"
            self.running = False
            self.notify()

    def _shutdown_loop(self):
        """Cancel anything still pending, then close the loop cleanly.

        Without this, `loop.close()` on a loop with live tasks leaves
        'Task was destroyed but it is pending' noise and can drop sockets
        without ever closing them."""
        try:
            pending = [t for t in asyncio.all_tasks(self.loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                self.loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        except Exception:
            pass
        finally:
            try:
                self.loop.close()
            except Exception:
                pass

    # ---------------------------------------------------------- prepare
    def _prepare(self):
        """Blocking: transcode + index every camera. Runs in an executor."""
        cache = resolve_path(self.cfg.media_dir)
        abort = self._stopping.is_set
        for i, rt in enumerate(self.cams):
            if self._stopping.is_set():
                return
            self.progress = (i, len(self.cams))
            self.notify()

            def step(frac, what, _i=i, _n=rt.cfg.name):
                self.item_progress = (frac, f"{_n} - {what}")

            try:
                rt.prepare_media(cache, abort=abort, on_progress=step)
                rt.state = "stopped"
            except media.Aborted:
                return
            except Exception as e:
                rt.state = "error"
                rt.error = str(e)
                self.log(f"{rt.cfg.name}: media prepare failed: {e}")
        self.progress = (len(self.cams), len(self.cams))

    async def _main(self):
        loop = asyncio.get_event_loop()

        ok, msg = await loop.run_in_executor(None, media.tools_available)
        self.log(("ffmpeg: " if ok else "ERROR: ") + msg)
        if not ok:
            self.log("NOTE: cameras whose media is already in the cache will "
                     "still start; anything new cannot be imported until "
                     "ffmpeg is available")

        self.alloc = AddressAllocator(self.cfg.addressing, log=self.log)
        bind_host = await loop.run_in_executor(
            None, self.alloc.allocate, [c.cfg for c in self.cams])
        alias = self.alloc.alias_active
        self._warn_dup_ports()
        self._warn_busy_ports(bind_host)

        await loop.run_in_executor(None, self._prepare)
        if self._stopping.is_set():
            self.log("start cancelled")
            return

        self.phase = "starting"
        self.notify()
        for c in self.cams:
            if self._stopping.is_set():
                break
            if c.state == "error":
                continue
            host = c.cfg.ip if alias else bind_host
            await c.start(host, retries=10 if alias else 0)

        if self.cfg.discovery_enabled and not self._stopping.is_set():
            self.discovery = DiscoveryResponder(self)
            try:
                await self.discovery.start()
                for c in self.running_cameras():
                    self.discovery.announce(c, "Hello")
            except OSError as e:
                self.log(f"discovery failed: {e}")
                self.discovery = None

        ok_n = len(self.running_cameras())
        self.log(f"started {ok_n}/{len(self.cams)} cameras")
        if sys.platform == "win32":
            has_rule = await loop.run_in_executor(None, firewall.rule_exists)
            if not has_rule:
                self.log("NOTE: no Windows Firewall rule found. Inbound "
                         "ONVIF/RTSP from OTHER machines will be blocked -- "
                         "use the Firewall button (or: python -m vcamsim "
                         "firewall) as Administrator.")
        self.phase = "running"
        self.notify()

        if not self._stopping.is_set():
            await self._stop_evt.wait()

        # ------------------------------------------------------- shutdown
        self.phase = "stopping"
        self.notify()
        if self.discovery:
            for c in self.running_cameras():
                self.discovery.announce(c, "Bye")
            await self.discovery.stop()
            self.discovery = None
        for c in self.cams:
            await c.stop()
        self.log("engine stopped")

    def _warn_dup_ports(self):
        """Two cameras on one ip:port (a pinned port colliding with the
        sequence, or two pins alike) -- the second would fail with a bare
        'address in use' that looks like an external conflict."""
        seen: dict[tuple, str] = {}
        for c in self.cams:
            for label, port in (("ONVIF", c.cfg.onvif_port),
                                ("RTSP", c.cfg.rtsp_port)):
                key = (c.cfg.ip, port)
                if key in seen:
                    self.log(f"WARNING: {c.cfg.name} {label} port {port} is also "
                             f"used by {seen[key]} -- check pin_onvif_port / "
                             f"pin_rtsp_port and the base ports")
                else:
                    seen[key] = f"{c.cfg.name} {label}"

    def _warn_busy_ports(self, bind_host: str):
        """A camera whose port is already taken fails with a bare OSError.
        Say so up front -- 554 in particular is very often already in use."""
        alias = bool(self.alloc and self.alloc.alias_active)
        busy = []
        for c in self.cams:
            for label, port in (("ONVIF", c.cfg.onvif_port),
                                ("RTSP", c.cfg.rtsp_port)):
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    # no SO_REUSEADDR: on Windows it lets this probe bind on
                    # top of a live listener and hides exactly the conflict
                    # we are looking for
                    s.bind((c.cfg.ip if alias else (bind_host or "0.0.0.0"), port))
                except OSError as e:
                    if alias and getattr(e, "winerror", None) == 10049:
                        continue              # tentative alias; retried at start
                    busy.append(f"{c.cfg.name} {label} :{port}")
                finally:
                    s.close()
        if busy:
            self.log("WARNING: port already in use -- " + ", ".join(busy[:8])
                     + (f" (+{len(busy) - 8} more)" if len(busy) > 8 else "")
                     + ". Change the base port in Settings"
                     + (", or stop the VCamSim service if it is running"
                        if sys.platform == "win32" else "") + ".")

    # --------------------------------------------------------------- stop
    def stop(self, timeout: float = 15.0):
        if not self.running and not self.thread:
            return
        self.phase = "stopping"
        self._stopping.set()               # aborts a transcode in flight
        # The loop may not exist yet if Stop lands during startup; wait for it
        # rather than leaving an orphan thread streaming from mmaps we are about
        # to close.
        self._loop_ready.wait(timeout=5)
        if self.loop and self._stop_evt is not None:
            try:
                self.loop.call_soon_threadsafe(self._stop_evt.set)
            except RuntimeError:
                pass
        alive = True
        if self.thread:
            self.thread.join(timeout=timeout)
            alive = self.thread.is_alive()
            if alive:
                self.log("WARNING: engine thread did not stop in time; "
                         "leaving media mapped to avoid a crash")
            else:
                self.thread = None
        if alive:
            self.notify()
            raise TimeoutError("Engine is still stopping; media and addresses remain reserved")
        self.running = False
        if self.alloc:
            self.alloc.release()
            self.alloc = None
        if not alive:
            # only safe once nothing can read them -- and only *our* mappings:
            # another engine in this process may still be streaming the same
            # cached file
            for c in self.cams:
                for v in c.variants.values():
                    media.release(v)
                c.variants.clear()
                c.snaps = None
        self.phase = "idle"
        self.notify()

    # ------------------------------------------------------- live controls
    def submit(self, coro):
        if self.loop and self.running:
            return asyncio.run_coroutine_threadsafe(coro, self.loop)
        return None

    def trigger_motion(self, name: str, on: bool = True):
        c = self.camera(name)
        if c:
            self.submit(_call(c.onvif.events.motion, on))

    def trigger_tamper(self, name: str, on: bool = True):
        c = self.camera(name)
        if c:
            self.submit(_call(c.onvif.events.tamper, on))

    def trigger_signal_loss(self, name: str, on: bool = True):
        c = self.camera(name)
        if c:
            self.submit(_call(c.onvif.events.signal_loss, on))


async def _call(fn, *a):
    fn(*a)
