"""Phase 4: WS-Discovery responder shared by every camera."""
from __future__ import annotations

import asyncio
import re
import socket
import time
import uuid
from xml.etree import ElementTree as ET

from .soap import esc

MCAST_ADDR = "239.255.255.250"
MCAST_PORT = 3702

NSD = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
NSA = "http://schemas.xmlsoap.org/ws/2004/08/addressing"

_ENV = ('xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
        f'xmlns:w="{NSA}" xmlns:d="{NSD}" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl" '
        'xmlns:tds="http://www.onvif.org/ver10/device/wsdl"')

# Types a Probe may ask for that we answer to, as (namespace, local name). A
# Probe with no Types gets matches; a probe for printers or WSD hosts does not.
# "wsdp:Device" and "tds:Device" share a local name, so the prefix has to be
# resolved through the message's own xmlns declarations.
_OUR_TYPES = {
    ("http://www.onvif.org/ver10/network/wsdl", "networkvideotransmitter"),
    ("http://www.onvif.org/ver10/device/wsdl", "device"),
}
_XMLNS_RE = re.compile(rb'xmlns(?::([A-Za-z_][\w.-]*))?\s*=\s*["\']([^"\']*)["\']')


def _types_match(raw: bytes, types: str) -> bool:
    if not types.strip():
        return True
    nsmap = {m.group(1).decode() if m.group(1) else "": m.group(2).decode()
             for m in _XMLNS_RE.finditer(raw)}
    for tok in types.split():
        prefix, _, local = tok.rpartition(":")
        ns = nsmap.get(prefix)
        if ns is None:
            # undeclared prefix: be lenient and go by the local name
            if local.lower() in {ln for _, ln in _OUR_TYPES}:
                return True
        elif (ns, local.lower()) in _OUR_TYPES:
            return True
    return False
# one log line per probing host per minute -- a VMS probes every few seconds
_LOG_EVERY = 60.0


def _scopes(cam) -> str:
    c = cam.cfg
    return esc(" ".join([
        "onvif://www.onvif.org/type/video_encoder",
        "onvif://www.onvif.org/type/Network_Video_Transmitter",
        "onvif://www.onvif.org/Profile/Streaming",
        f"onvif://www.onvif.org/name/{c.name.replace(' ', '_')}",
        f"onvif://www.onvif.org/hardware/{c.model}",
        "onvif://www.onvif.org/location/any",
    ]))


def _match(cam) -> str:
    return (f'<d:ProbeMatch><w:EndpointReference>'
            f'<w:Address>urn:uuid:{esc(cam.cfg.uuid)}</w:Address></w:EndpointReference>'
            f'<d:Types>dn:NetworkVideoTransmitter tds:Device</d:Types>'
            f'<d:Scopes>{_scopes(cam)}</d:Scopes>'
            f'<d:XAddrs>{cam.base_url}/onvif/device_service</d:XAddrs>'
            f'<d:MetadataVersion>1</d:MetadataVersion></d:ProbeMatch>')


def _probe_matches(relates_to: str, cams) -> bytes:
    body = "".join(_match(c) for c in cams)
    xml = (f'<?xml version="1.0" encoding="utf-8"?><e:Envelope {_ENV}>'
           f'<e:Header><w:MessageID>urn:uuid:{uuid.uuid4()}</w:MessageID>'
           f'<w:RelatesTo>{esc(relates_to)}</w:RelatesTo>'
           f'<w:To>{NSA}/role/anonymous</w:To>'
           f'<w:Action>{NSD}/ProbeMatches</w:Action>'
           f'<d:AppSequence InstanceId="1" MessageNumber="1"/></e:Header>'
           f'<e:Body><d:ProbeMatches>{body}</d:ProbeMatches></e:Body></e:Envelope>')
    return xml.encode("utf-8")


def _hello_bye(cam, kind: str) -> bytes:
    inner = (f'<d:{kind}><w:EndpointReference>'
             f'<w:Address>urn:uuid:{esc(cam.cfg.uuid)}</w:Address></w:EndpointReference>'
             f'<d:Types>dn:NetworkVideoTransmitter tds:Device</d:Types>'
             f'<d:Scopes>{_scopes(cam)}</d:Scopes>'
             f'<d:XAddrs>{cam.base_url}/onvif/device_service</d:XAddrs>'
             f'<d:MetadataVersion>1</d:MetadataVersion></d:{kind}>')
    xml = (f'<?xml version="1.0" encoding="utf-8"?><e:Envelope {_ENV}>'
           f'<e:Header><w:MessageID>urn:uuid:{uuid.uuid4()}</w:MessageID>'
           f'<w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
           f'<w:Action>{NSD}/{kind}</w:Action>'
           f'<d:AppSequence InstanceId="1" MessageNumber="1"/></e:Header>'
           f'<e:Body>{inner}</e:Body></e:Envelope>')
    return xml.encode("utf-8")


def local_addresses() -> list[str]:
    out = {"0.0.0.0"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            out.add(info[4][0])
    except Exception:
        pass
    return list(out)


class DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, engine):
        self.engine = engine
        self.transport = None
        self._last_log: dict[str, float] = {}

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            return
        action = msgid = types = ""
        is_probe = False
        for el in root.iter():
            tag = el.tag.rsplit("}", 1)[-1]
            if tag == "Action":
                action = (el.text or "").strip()
            elif tag == "MessageID":
                msgid = (el.text or "").strip()
            elif tag == "Probe":
                is_probe = True
            elif tag == "Types" and is_probe:
                types = (el.text or "").strip()
        if not is_probe and not action.endswith("/Probe"):
            return
        # honour the Types filter: a probe for some other device class
        # (printers, WSD hosts) must not get camera matches back
        if not _types_match(data, types):
            return
        cams = [c for c in self.engine.running_cameras()
                if not c.cfg.faults.hide_from_discovery
                and not c.cfg.faults.offline]
        if not cams:
            return
        try:
            self.transport.sendto(_probe_matches(msgid, cams), addr)
        except OSError:
            pass
        now = time.monotonic()
        if now - self._last_log.get(addr[0], 0.0) >= _LOG_EVERY:
            self._last_log[addr[0]] = now
            if len(self._last_log) > 256:
                self._last_log = {k: v for k, v in self._last_log.items()
                                  if now - v < _LOG_EVERY}
            self.engine.log(f"WS-Discovery Probe from {addr[0]} -> "
                            f"{len(cams)} matches")


class DiscoveryResponder:
    def __init__(self, engine):
        self.engine = engine
        self.transport = None
        self.sock = None

    async def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass
        s.bind(("", MCAST_PORT))
        grp = socket.inet_aton(MCAST_ADDR)
        for ip in local_addresses():
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                             grp + socket.inet_aton(ip))
            except OSError:
                pass
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        # send Hello/Bye out of the interface the cameras advertise on, not
        # whichever NIC the OS picks for multicast (often a VPN or virtual one)
        adv = (self.engine.cfg.addressing.advertise_ip or "").strip()
        cams = self.engine.cams
        if not adv and cams and cams[0].cfg.ip not in ("", "0.0.0.0"):
            adv = cams[0].cfg.ip
        if adv and not adv.startswith("127."):
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                             socket.inet_aton(adv))
            except OSError:
                pass
        s.setblocking(False)
        self.sock = s
        loop = asyncio.get_event_loop()
        self.transport, _ = await loop.create_datagram_endpoint(
            lambda: DiscoveryProtocol(self.engine), sock=s)
        self.engine.log(f"WS-Discovery listening on {MCAST_ADDR}:{MCAST_PORT}")

    def announce(self, cam, kind: str = "Hello"):
        if not self.transport:
            return
        try:
            self.transport.sendto(_hello_bye(cam, kind), (MCAST_ADDR, MCAST_PORT))
        except OSError:
            pass

    async def stop(self):
        if self.transport:
            self.transport.close()
            self.transport = None
        self.sock = None
