"""Config model (dataclasses + YAML). Phase 0."""
from __future__ import annotations

import dataclasses as dc
import os
import socket
import sys
import typing as t
from dataclasses import dataclass, field
from pathlib import Path

import yaml


# ---------------------------------------------------------------- dataclass IO
def _is_dc(tp) -> bool:
    return dc.is_dataclass(tp) and isinstance(tp, type)


def _coerce(tp, v, default):
    """Bend a YAML scalar to the field's declared type.

    Hand-edited configs arrive with `port: "8000"`, `enabled: yes`,
    `drop_rtp_pct: 5` and so on; a str where an int is expected used to survive
    loading and blow up much later in a socket call or an f-string."""
    if v is None:
        return default
    try:
        if tp is bool:
            if isinstance(v, str):
                return v.strip().lower() in ("1", "true", "yes", "on")
            return bool(v)
        if tp is int:
            if isinstance(v, bool):
                return int(v)
            return int(float(v))
        if tp is float:
            return float(v)
        if tp is str:
            return "" if v is None else str(v)
    except (TypeError, ValueError):
        return default
    return v


def from_dict(cls, data):
    """Recursively build a dataclass from a plain dict."""
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise TypeError(f"{cls.__name__}: expected a mapping, got {type(data).__name__}")
    kw = {}
    hints = t.get_type_hints(cls)
    defaults = {f.name: (f.default if f.default is not dc.MISSING else None)
                for f in dc.fields(cls)}
    for k, v in data.items():
        if k not in defaults:
            continue
        tp = hints.get(k)
        origin = t.get_origin(tp)
        if _is_dc(tp):
            kw[k] = from_dict(tp, v or {})
        elif origin in (list, t.List):
            (arg,) = t.get_args(tp)
            if v is None:
                v = []
            if not isinstance(v, list):
                raise TypeError(f"{cls.__name__}.{k}: expected a list")
            kw[k] = [from_dict(arg, i) for i in v] if _is_dc(arg) else list(v)
        else:
            kw[k] = _coerce(tp, v, defaults[k])
    return cls(**kw)


def to_dict(obj):
    if dc.is_dataclass(obj):
        return {f.name: to_dict(getattr(obj, f.name)) for f in dc.fields(obj)}
    if isinstance(obj, list):
        return [to_dict(i) for i in obj]
    return obj


# ---------------------------------------------------------------------- models
@dataclass
class ProfileCfg:
    name: str = "MainStream"
    token: str = "profile_1"
    width: int = 1920
    height: int = 1080
    fps: int = 25
    bitrate: int = 4096           # kbps
    codec: str = "H264"           # H264 | H265
    gop: int = 0                  # 0 -> fps (1s GOP)
    stream_path: str = "/Streaming/Channels/101"

    def key(self) -> str:
        return (f"{self.codec}_{self.width}x{self.height}_{self.fps}_{self.bitrate}"
                f"_g{self.gop or self.fps}")


@dataclass
class FaultCfg:
    # reachability
    offline: bool = False
    onvif_offline: bool = False
    rtsp_offline: bool = False
    hide_from_discovery: bool = False
    # auth / time
    reject_auth: bool = False
    clock_drift_sec: int = 0
    # soap
    soap_latency_ms: int = 0
    soap_timeout: bool = False
    soap_fault: bool = False
    malformed_xml: bool = False
    truncate_response: bool = False
    # stream
    refuse_describe: bool = False
    stream_drop_after_sec: int = 0
    freeze_video: bool = False
    corrupt_nal_pct: float = 0.0
    drop_rtp_pct: float = 0.0
    bitrate_collapse: bool = False
    # events
    event_flood: bool = False
    event_stall: bool = False
    subscription_expire_sec: int = 0

    def any_active(self) -> bool:
        d = to_dict(self)
        return any(bool(v) for v in d.values())


@dataclass
class CameraCfg:
    name: str = "Camera"
    video: str = ""
    enabled: bool = True
    username: str = "admin"
    password: str = "admin"
    manufacturer: str = "VCamSim"
    model: str = "VCS-2000"
    firmware: str = "1.0.0"
    hardware: str = "VCS-HW1"
    serial: str = ""
    uuid: str = ""
    mac: str = ""
    # assigned by the address allocator at start
    ip: str = ""
    onvif_port: int = 0
    rtsp_port: int = 0
    profiles: list[ProfileCfg] = field(default_factory=list)
    faults: FaultCfg = field(default_factory=FaultCfg)
    motion_interval_sec: int = 0      # 0 = no automatic motion
    motion_duration_sec: int = 3
    # GetStreamUri returns rtsp://user:pass@host/... (some VMS clients need it)
    stream_uri_credentials: bool = False
    # 0 = allocate automatically; set to pin a camera to a specific port
    pin_onvif_port: int = 0
    pin_rtsp_port: int = 0
    # Only import the first N seconds of the source. The stream loops for ever,
    # so a long file buys nothing but a very long first-run transcode.
    # 0 = import the whole file.
    import_seconds: int = 60

    def ensure_profiles(self):
        if not self.profiles:
            self.profiles = [
                ProfileCfg("MainStream", "profile_1", 1920, 1080, 25, 4096,
                           "H264", 0, "/Streaming/Channels/101"),
                ProfileCfg("SubStream", "profile_2", 704, 576, 15, 512,
                           "H264", 0, "/Streaming/Channels/102"),
            ]
        return self.profiles


@dataclass
class AddressingCfg:
    mode: str = "port"                 # port | alias
    bind_host: str = "0.0.0.0"
    advertise_ip: str = ""             # blank -> auto detect
    onvif_base_port: int = 8000
    rtsp_base_port: int = 554
    # alias mode
    alias_interface: str = "Ethernet"
    alias_base_ip: str = "10.10.1.100"
    alias_netmask: str = "255.255.255.0"
    alias_onvif_port: int = 80
    alias_rtsp_port: int = 554


@dataclass
class AppCfg:
    media_dir: str = "media_cache"
    discovery_enabled: bool = True
    rtsp_session_timeout_sec: int = 120     # 0 = never drop idle RTSP sessions
    ui_theme: str = "dark"                  # dark | light
    # loopback-only control channel the GUI uses to drive the Windows service
    control_port: int = 9760
    addressing: AddressingCfg = field(default_factory=AddressingCfg)
    cameras: list[CameraCfg] = field(default_factory=list)

    # -- io
    @staticmethod
    def load(path: str | Path) -> "AppCfg":
        p = Path(path)
        if not p.exists():
            return AppCfg()
        try:
            with open(p, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
        except yaml.YAMLError as e:
            raise RuntimeError(f"{p.name} is not valid YAML: {e}") from e
        if not isinstance(raw, dict):
            raise RuntimeError(f"{p.name} must contain a YAML mapping")
        try:
            cfg = from_dict(AppCfg, raw)
        except (TypeError, ValueError, AttributeError) as e:
            raise RuntimeError(f"{p.name} has an unusable value: {e}") from e
        cfg.normalize()
        return cfg

    def normalize(self) -> list[str]:
        """Repair what can be repaired and report what was changed.

        Everything here is a value that loads fine but breaks later: an odd
        frame width makes ffmpeg refuse yuv420p, two cameras with one name
        share a UUID and a MAC so a VMS sees a single device, two profiles with
        one token make GetProfile ambiguous, a zero fps divides by zero."""
        notes: list[str] = []
        if self.ui_theme not in ("dark", "light"):
            self.ui_theme = "dark"
        if self.addressing.mode not in ("port", "alias"):
            notes.append(f"addressing.mode '{self.addressing.mode}' -> port")
            self.addressing.mode = "port"
        a = self.addressing
        a.onvif_base_port = min(65000, max(1, int(a.onvif_base_port or 8000)))
        a.rtsp_base_port = min(65000, max(1, int(a.rtsp_base_port or 554)))
        a.alias_onvif_port = min(65535, max(1, int(a.alias_onvif_port or 80)))
        a.alias_rtsp_port = min(65535, max(1, int(a.alias_rtsp_port or 554)))
        self.rtsp_session_timeout_sec = max(0, int(self.rtsp_session_timeout_sec))
        self.control_port = min(65535, max(1, int(self.control_port or 9760)))
        if not self.media_dir.strip():
            self.media_dir = "media_cache"

        seen: set[str] = set()
        for i, c in enumerate(self.cameras, 1):
            c.name = (c.name or "").strip() or f"Camera {i:02d}"
            base, n = c.name, 2
            while c.name in seen:
                c.name = f"{base} ({n})"
                n += 1
            if c.name != base:
                notes.append(f"duplicate camera name '{base}' -> '{c.name}'")
                c.serial = c.uuid = c.mac = ""        # identity follows the name
            seen.add(c.name)
            c.import_seconds = max(0, int(c.import_seconds))
            c.motion_interval_sec = max(0, int(c.motion_interval_sec))
            c.motion_duration_sec = max(1, int(c.motion_duration_sec))
            c.pin_onvif_port = min(65535, max(0, int(c.pin_onvif_port)))
            c.pin_rtsp_port = min(65535, max(0, int(c.pin_rtsp_port)))
            c.video = (c.video or "").strip()
            c.ensure_profiles()
            toks: set[str] = set()
            for j, p in enumerate(c.profiles, 1):
                p.codec = "H265" if str(p.codec).upper() in ("H265", "HEVC") else "H264"
                w, h = max(16, int(p.width)), max(16, int(p.height))
                w, h = w - w % 2, h - h % 2
                if (w, h) != (p.width, p.height):
                    notes.append(f"{c.name}/{p.name}: size {p.width}x{p.height} -> {w}x{h}")
                    p.width, p.height = w, h
                p.fps = min(120, max(1, int(p.fps)))
                p.bitrate = max(32, int(p.bitrate))
                p.gop = max(0, int(p.gop))
                p.name = (p.name or "").strip() or f"Stream{j}"
                p.token = (p.token or "").strip() or f"profile_{j}"
                tb, k = p.token, 2
                while p.token in toks:
                    p.token = f"{tb}_{k}"
                    k += 1
                toks.add(p.token)
                sp = (p.stream_path or "").strip() or f"/Streaming/Channels/10{j}"
                if not sp.startswith("/"):
                    sp = "/" + sp
                p.stream_path = sp
            f = c.faults
            f.corrupt_nal_pct = min(100.0, max(0.0, float(f.corrupt_nal_pct)))
            f.drop_rtp_pct = min(100.0, max(0.0, float(f.drop_rtp_pct)))
            for k in ("soap_latency_ms", "stream_drop_after_sec",
                      "subscription_expire_sec"):
                setattr(f, k, max(0, int(getattr(f, k))))
        return notes

    def save(self, path: str | Path):
        """Write atomically, and never persist runtime-assigned addresses.

        The allocator stamps ip/onvif_port/rtsp_port onto the live CameraCfg
        objects at start; saving those made the next run look pre-addressed and
        showed stale ports in the UI while stopped."""
        data = to_dict(self)
        for cam in data.get("cameras", []):
            cam["ip"] = ""
            cam["onvif_port"] = 0
            cam["rtsp_port"] = 0
        p = Path(path)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)


# -------------------------------------------------------------------- helpers
def app_dir() -> Path:
    """Folder the program lives in (exe folder when frozen)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def resolve_path(p: str | Path) -> Path:
    """Relative paths are resolved against the program folder, not the CWD,
    so double-clicking the exe from anywhere behaves the same."""
    q = Path(p)
    return q if q.is_absolute() else app_dir() / q


def local_ip() -> str:
    """This machine's LAN address. Needs no traffic: a UDP connect() only asks
    the routing table which source address it would use."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 53))
        ip = s.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    finally:
        s.close()
    # no default route (isolated lab network): take the first non-loopback
    # IPv4 the hostname resolves to, which is what a VMS on that LAN can reach
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except Exception:
        pass
    return "127.0.0.1"


def generate_cameras(template: CameraCfg, count: int, start_index: int = 1) -> list[CameraCfg]:
    """Stamp out N cameras from a template ('{n}' in name is substituted)."""
    out = []
    for i in range(start_index, start_index + count):
        c = from_dict(CameraCfg, to_dict(template))
        base = template.name if "{n}" in template.name else template.name + " {n}"
        c.name = base.replace("{n}", f"{i:02d}")
        c.serial = c.uuid = c.mac = ""     # re-derived per camera
        c.ip = ""
        c.onvif_port = c.rtsp_port = 0
        out.append(c)
    return out
