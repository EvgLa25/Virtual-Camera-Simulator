"""Phase 4: address allocation -- port-per-camera or IP-alias-per-camera."""
from __future__ import annotations

import ctypes
import ipaddress
import subprocess
import sys

from .config import AddressingCfg, local_ip

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def is_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _netsh(args: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(["netsh"] + args, capture_output=True,
                           creationflags=_NO_WINDOW)
        return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")
    except FileNotFoundError:
        return 1, "netsh not found"


class AddressAllocator:
    """Assigns cfg.ip / cfg.onvif_port / cfg.rtsp_port to every camera."""

    def __init__(self, acfg: AddressingCfg, log=print):
        self.cfg = acfg
        self.log = log
        self.added_ips: list[str] = []
        self.alias_active = False

    # ------------------------------------------------------------------
    def allocate(self, cameras) -> str:
        """Returns the bind host to use for the listening sockets."""
        if self.cfg.mode == "alias":
            return self._alloc_alias(cameras)
        return self._alloc_port(cameras)

    def _alloc_port(self, cameras) -> str:
        ip = self.cfg.advertise_ip or local_ip()
        for i, c in enumerate(cameras):
            c.ip = ip
            c.onvif_port = c.pin_onvif_port or (self.cfg.onvif_base_port + i)
            c.rtsp_port = c.pin_rtsp_port or (self.cfg.rtsp_base_port + i)
        self.log(f"addressing: port mode on {ip} "
                 f"({self.cfg.onvif_base_port}+, {self.cfg.rtsp_base_port}+)")
        return self.cfg.bind_host

    def _alloc_alias(self, cameras) -> str:
        if not is_admin():
            self.log("addressing: alias mode needs Administrator -- "
                     "falling back to port mode")
            return self._alloc_port(cameras)
        base = ipaddress.IPv4Address(self.cfg.alias_base_ip)
        self.alias_active = True
        for i, c in enumerate(cameras):
            ip = str(base + i)
            c.ip = ip
            c.onvif_port = c.pin_onvif_port or self.cfg.alias_onvif_port
            c.rtsp_port = c.pin_rtsp_port or self.cfg.alias_rtsp_port
            rc, out = _netsh(["interface", "ipv4", "add", "address",
                              f'name={self.cfg.alias_interface}',
                              f"address={ip}", f"mask={self.cfg.alias_netmask}"])
            if rc == 0:
                self.added_ips.append(ip)
            elif "already exists" in out.lower() or "כבר" in out:
                self.log(f"addressing: using existing {ip}; it will not be removed")
            else:
                raise RuntimeError(f"failed to add alias {ip}: {out.strip()[:120]}")
        self.log(f"addressing: alias mode, {len(self.added_ips)} IPs on "
                 f"{self.cfg.alias_interface}")
        return "0.0.0.0"

    # ------------------------------------------------------------------
    def release(self):
        for ip in self.added_ips:
            _netsh(["interface", "ipv4", "delete", "address",
                    f'name={self.cfg.alias_interface}', f"address={ip}"])
        if self.added_ips:
            self.log(f"addressing: removed {len(self.added_ips)} alias IPs")
        self.added_ips = []
