"""Per-camera stable identity derived from the camera name. Phase 0."""
from __future__ import annotations

import hashlib
import uuid as _uuid


def derive(name: str) -> tuple[str, str, str]:
    """-> (uuid, mac, serial); stable for a given name."""
    h = hashlib.md5(name.encode("utf-8")).digest()
    u = str(_uuid.UUID(bytes=h))
    mac = ":".join("%02X" % b for b in (0x00, 0x1A, 0x07, h[0], h[1], h[2]))
    serial = "VCS" + h.hex()[:11].upper()
    return u, mac, serial


def fill(cfg):
    u, mac, ser = derive(cfg.name)
    if not cfg.uuid:
        cfg.uuid = u
    if not cfg.mac:
        cfg.mac = mac
    if not cfg.serial:
        cfg.serial = ser
    return cfg
