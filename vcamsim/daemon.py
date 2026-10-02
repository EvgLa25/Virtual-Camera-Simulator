"""Headless runner: the engine plus its control channel.

Exactly what the Windows service executes, factored out so it can also be run
in the foreground (`python -m vcamsim daemon`) and tested without installing a
service or needing Administrator.
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from pathlib import Path

from .config import AppCfg, from_dict
from .control import ControlServer
from .engine import Engine


class Runner:
    def __init__(self, config_path: str, log_cb=None):
        self.config_path = str(config_path)
        self.engine: Engine | None = None
        self.control: ControlServer | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._own_logs: deque[str] = deque(maxlen=2000)
        self._log_cb = log_cb
        self._stop = threading.Event()
        self._ready = threading.Event()
        # serialises start/stop/replace: they run off the control loop so a
        # 15 s engine stop never blocks the GUI's status polls
        self._lock = threading.Lock()
        self.config_error = ""
        try:
            self.cfg = AppCfg.load(self.config_path)
        except RuntimeError as e:
            # a broken config.yaml must not take the service down with it:
            # keep serving the control channel so the GUI can show the error
            # and hand over a good config
            self.cfg = AppCfg()
            self.config_error = str(e)
            self.log(f"ERROR: {e} -- running with an empty config until a "
                     f"valid one is saved")

    # ------------------------------------------------------------------ log
    def log(self, msg: str, who: str = "service"):
        line = f"{time.strftime('%H:%M:%S')} [{who}] {msg}"
        self._own_logs.append(line)
        if self._log_cb:
            try:
                self._log_cb(line)
            except Exception:
                pass

    def drain_logs(self, limit: int = 400) -> list[str]:
        out = []
        if self.engine:
            out.extend(self.engine.drain_logs(limit))
        while self._own_logs and len(out) < limit:
            out.append(self._own_logs.popleft())
        return out

    # -------------------------------------------------------------- status
    def snapshot_status(self) -> dict:
        eng = self.engine
        if eng is None:
            return {"phase": "idle", "running": 0, "total": 0, "clients": 0,
                    "subs": 0, "faults": 0, "mbps": None, "progress": [0, 0],
                    "item_progress": [0.0, ""], "cameras": []}
        st = eng.stats()
        cams = []
        for c in eng.cams:
            urls = {}
            if c.state == "running":
                for p in c.cfg.profiles:
                    urls[p.token] = c.rtsp_url(p, credentials=True)
            cams.append({
                "name": c.cfg.name, "ip": c.cfg.ip,
                "onvif": c.cfg.onvif_port, "rtsp": c.cfg.rtsp_port,
                "state": c.state, "clients": c.clients,
                "subs": len(c.onvif.events.subs),
                "faults": c.cfg.faults.any_active(),
                "error": c.error,
                "base_url": c.base_url if c.state == "running" else "",
                "urls": urls,
            })
        st = dict(st)
        st["cameras"] = cams
        st["running_engine"] = bool(eng.running)
        if self.config_error:
            st["config_error"] = self.config_error
        return st

    def snapshot(self, name: str) -> bytes:
        if not self.engine:
            return b""
        c = self.engine.camera(name)
        return c.snapshot() if c else b""

    # ------------------------------------------------------------ commands
    def _camera_cfg(self, name: str):
        for c in self.cfg.cameras:
            if c.name == name:
                return c
        return None

    def set_faults(self, name: str, faults: dict) -> bool:
        target = None
        if self.engine:
            rt = self.engine.camera(name)
            target = rt.cfg if rt else None
        target = target or self._camera_cfg(name)
        if target is None:
            return False
        for k, v in (faults or {}).items():
            if hasattr(target.faults, k):
                cur = getattr(target.faults, k)
                try:
                    setattr(target.faults, k,
                            bool(v) if isinstance(cur, bool)
                            else float(v) if isinstance(cur, float) else int(v))
                except (TypeError, ValueError):
                    continue
        return True

    def trigger(self, name: str, kind: str, on: bool) -> bool:
        if not self.engine:
            return False
        fn = getattr(self.engine, f"trigger_{kind}", None)
        if fn is None:
            return False
        fn(name, on)
        return True

    def engine_action(self, action: str) -> bool:
        with self._lock:
            if action == "start":
                return self._start_engine()
            if action == "stop":
                return self._stop_engine()
            if action == "restart":
                if not self._stop_engine():
                    return False
                return self._start_engine()
            if action == "shutdown":
                self._stop.set()
                return True
            return False

    def replace_config(self, data: dict) -> bool:
        """Take a new config from the GUI, persist it and restart the engine."""
        # parse first: a bad payload must leave the running engine alone
        try:
            cfg = from_dict(AppCfg, data)
            cfg.normalize()
        except (TypeError, ValueError, AttributeError) as e:
            self.log(f"rejected config: {e}")
            return False
        with self._lock:
            if self.control and cfg.control_port != self.control.port:
                self.log("changing the control port requires restarting the service")
                return False
            was_running = bool(self.engine and self.engine.running)
            try:
                cfg.save(self.config_path)
            except OSError as e:
                self.log(f"could not save config: {e}")
                return False
            if not self._stop_engine():
                self.log("config saved, but the previous engine is still stopping")
                return False
            self.cfg = cfg
            self.config_error = ""
            self.log(f"config replaced ({len(self.cfg.cameras)} cameras)")
            if was_running:
                return self._start_engine()
        return True

    # ------------------------------------------------------------- engine
    def _start_engine(self) -> bool:
        if self.engine and self.engine.running:
            return True
        if self.engine and self.engine.thread and self.engine.thread.is_alive():
            self.log("previous engine is still stopping; start refused")
            return False
        # the engine's lines are queued for the GUI *and* mirrored to wherever
        # the runner logs (service.log for the Windows service, stdout for
        # `vcamsim daemon`), so a start-up failure is readable with no GUI
        self.engine = Engine(self.cfg, log_cb=self._log_cb)
        self.engine.in_service = True
        try:
            self.engine.start()
        except Exception as e:
            self.log(f"start failed: {e}")
            self.engine = None
            return False
        return True

    def _stop_engine(self) -> bool:
        if self.engine:
            try:
                self.engine.stop()
            except TimeoutError as e:
                self.log(str(e))
                return False
            self.engine = None
        return True

    # --------------------------------------------------------------- main
    def run(self, autostart: bool = True):
        """Blocks until stop() is called. Runs the control channel's loop."""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main(autostart))
        finally:
            try:
                self.loop.close()
            except Exception:
                pass

    async def _main(self, autostart: bool):
        self.log(f"config: {self.config_path}")
        self.control = ControlServer(self, self.cfg.control_port)
        try:
            await self.control.start()
        except OSError as e:
            self.log(f"control channel failed to bind: {e}")
            self.control = None
        if autostart and self.cfg.cameras:
            with self._lock:
                self._start_engine()
        self._ready.set()
        while not self._stop.is_set():
            await asyncio.sleep(0.25)
        self.log("shutting down")
        if self.control:
            await self.control.stop()
        with self._lock:
            self._stop_engine()

    def stop(self):
        self._stop.set()

    def wait_ready(self, seconds: float = 15.0) -> bool:
        return self._ready.wait(seconds)


def run_foreground(config_path: str, autostart: bool = True):
    r = Runner(config_path, log_cb=lambda l: print(l, flush=True))
    try:
        r.run(autostart=autostart)
    except KeyboardInterrupt:
        r.stop()
    return r
