"""Phase 2: RTP packetization (RFC 6184 H.264 / RFC 7798 H.265) + RTCP SR."""
from __future__ import annotations

import struct
import time

MTU = 1400
CLOCK = 90000


def rtp_packet(payload: bytes, pt: int, seq: int, ts: int, ssrc: int, marker: bool) -> bytes:
    b1 = (pt & 0x7F) | (0x80 if marker else 0)
    return struct.pack("!BBHII", 0x80, b1, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc) + payload


def pack_h264(nal: bytes, mtu: int = MTU) -> list[bytes]:
    if len(nal) <= mtu:
        return [nal]
    hdr = nal[0]
    nri = hdr & 0x60
    typ = hdr & 0x1F
    body = nal[1:]
    step = mtu - 2
    out = []
    for i in range(0, len(body), step):
        chunk = body[i:i + step]
        s = 0x80 if i == 0 else 0
        e = 0x40 if i + len(chunk) >= len(body) else 0
        out.append(bytes([nri | 28, s | e | typ]) + chunk)
    return out


def pack_h265(nal: bytes, mtu: int = MTU) -> list[bytes]:
    if len(nal) <= mtu:
        return [nal]
    typ = (nal[0] >> 1) & 0x3F
    b0 = (49 << 1) | (nal[0] & 0x01)       # PayloadHdr type 49 = FU
    b1 = nal[1]
    body = nal[2:]
    step = mtu - 3
    out = []
    for i in range(0, len(body), step):
        chunk = body[i:i + step]
        s = 0x80 if i == 0 else 0
        e = 0x40 if i + len(chunk) >= len(body) else 0
        out.append(bytes([b0, b1, s | e | typ]) + chunk)
    return out


def packetize(nal: bytes, codec: str) -> list[bytes]:
    return pack_h264(nal) if codec == "H264" else pack_h265(nal)


def rtcp_sr(ssrc: int, rtp_ts: int, packets: int, octets: int) -> bytes:
    ntp = int((time.time() + 2208988800) * (1 << 32))
    return struct.pack("!BBHIIIIII", 0x80, 200, 6, ssrc,
                       (ntp >> 32) & 0xFFFFFFFF, ntp & 0xFFFFFFFF,
                       rtp_ts & 0xFFFFFFFF, packets & 0xFFFFFFFF,
                       octets & 0xFFFFFFFF)
