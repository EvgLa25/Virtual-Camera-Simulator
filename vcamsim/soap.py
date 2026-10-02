"""Phase 3: SOAP envelope handling + WS-Security UsernameToken (digest)."""
from __future__ import annotations

import base64
import hashlib
import re
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "wsse": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd",
    "wsu": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tr2": "http://www.onvif.org/ver20/media/wsdl",
    "tev": "http://www.onvif.org/ver10/events/wsdl",
    "timg": "http://www.onvif.org/ver20/imaging/wsdl",
    "tptz": "http://www.onvif.org/ver20/ptz/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "wsa": "http://www.w3.org/2005/08/addressing",
    "wsnt": "http://docs.oasis-open.org/wsn/b-2",
    "wstop": "http://docs.oasis-open.org/wsn/t-1",
    "tns1": "http://www.onvif.org/ver10/topics",
    # event property descriptions reference xs:boolean / xs:string by QName;
    # without the declaration a strict client cannot resolve the prefix
    "xs": "http://www.w3.org/2001/XMLSchema",
}

ENV_NS = " ".join(f'xmlns:{k}="{v}"' for k, v in NS.items())

PWD_DIGEST_TYPE = "PasswordDigest"


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def esc(v) -> str:
    """XML-escape any value going into a hand-built response.

    Camera names, models and passwords are user input; an unescaped '&' or '<'
    produces an envelope the client cannot parse at all."""
    return (str(v).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def parse(body: bytes):
    """-> (action, body_element, security_dict). Raises ET.ParseError."""
    root = ET.fromstring(body)
    action, body_el = "", None
    sec = {}
    for child in root:
        ln = local(child.tag)
        if ln == "Body":
            body_el = child
            for c in child:
                action = local(c.tag)
                body_el = c
                break
        elif ln == "Header":
            for h in child.iter():
                if local(h.tag) == "UsernameToken":
                    for f in h:
                        n = local(f.tag)
                        if n == "Username":
                            sec["user"] = (f.text or "").strip()
                        elif n == "Password":
                            sec["pass"] = (f.text or "").strip()
                            sec["type"] = f.get("Type", "")
                        elif n == "Nonce":
                            sec["nonce"] = (f.text or "").strip()
                        elif n == "Created":
                            sec["created"] = (f.text or "").strip()
    return action, body_el, sec


def check_wsse(sec: dict, user: str, password: str, now: datetime,
               skew: int = 300) -> bool:
    if not sec or sec.get("user") != user:
        return False
    pwd = sec.get("pass", "")
    created = sec.get("created", "")
    if created:
        c = parse_created(created)
        if c is not None and abs((now - c).total_seconds()) > skew:
            return False
    if "Digest" in (sec.get("type") or "") and sec.get("nonce"):
        try:
            nonce = base64.b64decode(sec["nonce"])
        except Exception:
            return False
        want = base64.b64encode(
            hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
        return want == pwd
    return pwd == password           # PasswordText


def parse_created(s: str) -> datetime | None:
    """wsu:Created as an aware UTC datetime, or None if unparseable.

    Clients send every flavour: a trailing Z, an explicit offset, no zone at
    all (treated as UTC), or 7-digit fractional seconds that fromisoformat()
    on older Pythons rejects."""
    txt = (s or "").strip()
    if not txt:
        return None
    if txt.endswith(("Z", "z")):
        txt = txt[:-1] + "+00:00"
    m = re.match(r"^(.*?\d\d:\d\d:\d\d)(\.\d+)?(.*)$", txt)
    if m:
        frac = (m.group(2) or "")[:7]          # at most microseconds
        txt = m.group(1) + frac + m.group(3)
    try:
        c = datetime.fromisoformat(txt)
    except ValueError:
        return None
    if c.tzinfo is None:
        c = c.replace(tzinfo=timezone.utc)
    return c


def envelope(inner: str, extra_header: str = "") -> bytes:
    hdr = f"<s:Header>{extra_header}</s:Header>" if extra_header else ""
    xml = (f'<?xml version="1.0" encoding="UTF-8"?>'
           f'<s:Envelope {ENV_NS}>{hdr}<s:Body>{inner}</s:Body></s:Envelope>')
    return xml.encode("utf-8")


def fault(reason: str, subcode: str = "ter:Action", code: str = "s:Receiver") -> bytes:
    inner = (f'<s:Fault><s:Code><s:Value>{code}</s:Value>'
             f'<s:Subcode><s:Value xmlns:ter="http://www.onvif.org/ver10/error">'
             f'{subcode}</s:Value></s:Subcode></s:Code>'
             f'<s:Reason><s:Text xml:lang="en">{esc(reason)}</s:Text></s:Reason></s:Fault>')
    return envelope(inner)


def auth_fault() -> bytes:
    return fault("Sender not authorized", "ter:NotAuthorized", "s:Sender")


def text_of(el, name: str, default: str = "") -> str:
    if el is None:
        return default
    for c in el.iter():
        if local(c.tag) == name and c.text:
            return c.text.strip()
    return default


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def duration_seconds(s: str, default: int = 60) -> int:
    m = re.match(r"^P(?:T)?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?$", (s or "").strip())
    if not m:
        return default
    h, mi, se = m.groups()
    v = int(h or 0) * 3600 + int(mi or 0) * 60 + int(float(se or 0))
    return v or default
