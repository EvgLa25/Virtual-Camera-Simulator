"""One interface the GUI uses for both a local engine and the remote service.

`LocalEngine` wraps the in-process `Engine` (running from source, or with no
service installed). `RemoteEngine` speaks the control channel to the service.
The GUI only ever sees this surface, so the two modes stay interchangeable.
"""
from __future__ import annotations

from .config import AppCfg, from_dict, to_dict
from .control import ControlClient, ControlError
from .engine import Engine


class RemoteCamera:
    """Stand-in for CameraRuntime, filled from one /status entry."""

    def __init__(self, data: dict, cfg, client: ControlClient):
        self.data = data
        self.cfg = cfg
        self._client = client
        self.state = data.get("state", "stopped")
        self.error = data.get("error", "")
        self.clients = data.get("clients", 0)

    @property
    def base_url(self) -> str:
        return self.data.get("base_url", "")

    def rtsp_url(self, profile, credentials: bool | None = None) -> str:
        return self.data.get("urls", {}).get(profile.token, "")

    @property
    def snaps(self):
        return self.state == "running"

    def snapshot(self) -> bytes:
        return self._client.snapshot(self.cfg.name)


class LocalEngine:
    """The engine in this process -- the behaviour VCamSim has always had."""

    mode = "local"

    def __init__(self, cfg):
        self.cfg = cfg
        self.engine = Engine(from_dict(AppCfg, to_dict(cfg)))

    @property
    def running(self) -> bool:
        return self.engine.running

    def start(self):
        self.engine.start()

    def stop(self):
        self.engine.stop()

    def status(self):
        return self.engine.status()

    def stats(self):
        return self.engine.stats()

    def drain_logs(self, limit: int = 300):
        return self.engine.drain_logs(limit)

    def take_dirty(self) -> bool:
        return self.engine.take_dirty()

    def camera(self, name):
        return self.engine.camera(name)

    def trigger(self, name: str, kind: str, on: bool):
        fn = getattr(self.engine, f"trigger_{kind}", None)
        if fn:
            fn(name, on)

    def push_faults(self, name: str, faults) -> bool:
        cam = self.engine.camera(name)
        if cam is None:
            return False
        for key, value in to_dict(faults).items():
            setattr(cam.cfg.faults, key, value)
        return True

    def push_config(self) -> bool:
        running = self.engine.running
        self.engine.stop()
        self.engine = Engine(from_dict(AppCfg, to_dict(self.cfg)))
        if running:
            self.engine.start()
        return True


class RemoteEngine:
    """The service, driven over the loopback control channel."""

    mode = "service"

    def __init__(self, cfg, port: int):
        self.cfg = cfg
        self.client = ControlClient(port)
        self._status = {}
        self._cams = {}
        self._dirty = True
        self.last_error = ""

    # ------------------------------------------------------------------
    def refresh(self) -> bool:
        try:
            self._status = self.client.status()
            self.last_error = ""
        except ControlError as e:
            self._status = {}
            self.last_error = str(e)
            return False
        self._cams = {c["name"]: c for c in self._status.get("cameras", [])}
        return True

    @property
    def running(self) -> bool:
        return bool(self._status.get("running_engine"))

    def start(self):
        return self.client.engine("start")

    def stop(self):
        return self.client.engine("stop")

    def status(self):
        return [{k: c.get(k) for k in
                 ("name", "ip", "onvif", "rtsp", "state", "clients", "subs",
                  "faults", "error")}
                for c in self._status.get("cameras", [])]

    def stats(self):
        s = dict(self._status)
        s.pop("cameras", None)
        s.setdefault("phase", "idle")
        s.setdefault("progress", (0, 0))
        s.setdefault("item_progress", (0.0, ""))
        for k in ("total", "running", "clients", "subs", "faults"):
            s.setdefault(k, 0)
        s.setdefault("mbps", None)
        return s

    def drain_logs(self, limit: int = 300):
        try:
            return self.client.logs()
        except ControlError:
            return []

    def take_dirty(self) -> bool:
        d = self._dirty
        self._dirty = False
        return d

    def camera(self, name):
        data = self._cams.get(name)
        if data is None:
            return None
        cfg = next((c for c in self.cfg.cameras if c.name == name), None)
        if cfg is None:
            return None
        return RemoteCamera(data, cfg, self.client)

    def trigger(self, name: str, kind: str, on: bool):
        try:
            self.client.trigger(name, kind, on)
        except ControlError:
            pass

    def push_faults(self, name: str, faults) -> bool:
        try:
            return self.client.set_faults(name, to_dict(faults))
        except ControlError:
            return False

    def push_config(self) -> bool:
        try:
            return self.client.put_config(to_dict(self.cfg))
        except ControlError:
            return False
