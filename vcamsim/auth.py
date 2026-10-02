"""HTTP/RTSP Digest + Basic auth (shared by the RTSP and HTTP servers)."""
from __future__ import annotations

import hashlib
import os
import time

REALM = "IPCamera"


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def make_nonce() -> str:
    return _md5(f"{time.time()}{os.urandom(8).hex()}")


def challenge(nonce: str, stale: bool = False) -> str:
    s = f'Digest realm="{REALM}", nonce="{nonce}", qop="auth", algorithm=MD5'
    if stale:
        s += ", stale=TRUE"
    return s


def _parse_kv(header: str) -> dict:
    out = {}
    part = header.split(" ", 1)[1] if " " in header else ""
    tok, buf, inq = [], "", False
    for ch in part:
        if ch == '"':
            inq = not inq
            continue
        if ch == "," and not inq:
            tok.append(buf)
            buf = ""
            continue
        buf += ch
    tok.append(buf)
    for tkn in tok:
        if "=" in tkn:
            k, v = tkn.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


class NonceStore:
    """Server-scoped pool of issued nonces (a client may reconnect between
    the 401 challenge and the authorized request)."""

    def __init__(self, keep: int = 64):
        self.keep = keep
        self._n: list[str] = []

    def issue(self) -> str:
        n = make_nonce()
        self._n.append(n)
        if len(self._n) > self.keep:
            del self._n[: -self.keep]
        return n

    def valid(self, n: str) -> bool:
        return n in self._n


def check(header: str | None, method: str, user: str, password: str,
          nonce) -> bool:
    """Validate an Authorization header (Digest preferred, Basic accepted)."""
    if not header:
        return False
    scheme = header.split(" ", 1)[0].lower()
    if scheme == "basic":
        import base64
        try:
            raw = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8", "replace")
            u, _, p = raw.partition(":")
            return u == user and p == password
        except Exception:
            return False
    if scheme != "digest":
        return False
    d = _parse_kv(header)
    if d.get("username") != user:
        return False
    uri = d.get("uri", "")
    ha1 = _md5(f"{user}:{d.get('realm', REALM)}:{password}")
    ha2 = _md5(f"{method}:{uri}")
    n = d.get("nonce", "")
    if d.get("qop"):
        expect = _md5(f"{ha1}:{n}:{d.get('nc','')}:{d.get('cnonce','')}:{d['qop']}:{ha2}")
    else:
        expect = _md5(f"{ha1}:{n}:{ha2}")
    # accept the client's nonce echo only if we issued it
    ok_nonce = nonce.valid(n) if isinstance(nonce, NonceStore) else (n == nonce)
    if not ok_nonce:
        return False
    return expect == d.get("response", "")
