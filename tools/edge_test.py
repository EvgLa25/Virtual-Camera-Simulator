"""Edge-case regression test: the scenarios a VMS or an operator hits that the
smoke test's happy path does not.

  python tools/edge_test.py
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vcamsim import media                                           # noqa: E402
from vcamsim.config import AddressingCfg, AppCfg, CameraCfg, ProfileCfg, from_dict  # noqa: E402
from vcamsim.engine import Engine                                   # noqa: E402
from tools.smoke_test import (check, make_test_video, soap_call,    # noqa: E402
                              soap_call_status, OK, FAIL)

ONVIF, RTSP = 18600, 18650
DEV_NS = "http://www.onvif.org/ver10/device/wsdl"
MED_NS = "http://www.onvif.org/ver10/media/wsdl"
MED2_NS = "http://www.onvif.org/ver20/media/wsdl"
EVT_NS = "http://www.onvif.org/ver10/events/wsdl"


def raw_http(host, port, payload: bytes, timeout=6) -> bytes:
    s = socket.create_connection((host, port), timeout=timeout)
    try:
        s.sendall(payload)
        out = b""
        while True:
            try:
                b = s.recv(65536)
            except socket.timeout:
                break
            if not b:
                break
            out += b
            if b"\r\n\r\n" in out:
                head, _, body = out.partition(b"\r\n\r\n")
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        n = int(line.split(b":")[1])
                        if len(body) >= n:
                            return out
        return out
    finally:
        s.close()


def wsse_naive(user, pwd) -> str:
    """UsernameToken whose Created has no time zone at all."""
    nonce = os.urandom(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    dig = base64.b64encode(
        hashlib.sha1(nonce + created.encode() + pwd.encode()).digest()).decode()
    return (
        '<s:Header><Security xmlns="http://docs.oasis-open.org/wss/2004/01/'
        'oasis-200401-wss-wssecurity-secext-1.0.xsd"><UsernameToken>'
        f'<Username>{user}</Username><Password Type="http://docs.oasis-open.org/'
        f'wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{dig}'
        f'</Password><Nonce>{base64.b64encode(nonce).decode()}</Nonce>'
        '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/'
        f'oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>'
        '</UsernameToken></Security></s:Header>')


def rtsp_exchange(host, port, lines: list[str], timeout=5) -> str:
    s = socket.create_connection((host, port), timeout=timeout)
    try:
        s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        out = b""
        end = time.time() + timeout
        while time.time() < end:
            try:
                b = s.recv(65536)
            except socket.timeout:
                break
            if not b:
                break
            out += b
            if b"\r\n\r\n" in out:
                break
        return out.decode("utf-8", "replace")
    finally:
        s.close()


def main():
    src = make_test_video(ROOT / "media_cache" / "_testsrc.mp4")

    # ------------------------------------------------------------ config
    raw = {"cameras": [
        {"name": "Dup", "video": str(src), "enabled": "yes",
         "profiles": [{"width": "641", "height": 361, "fps": "0", "token": "p"},
                      {"width": 640, "height": 360, "token": "p"}]},
        {"name": "Dup", "video": str(src)},
    ], "addressing": {"onvif_base_port": "18600"}}
    cfg = from_dict(AppCfg, raw)
    notes = cfg.normalize()
    c0 = cfg.cameras[0]
    check("config: strings coerce to ints/bools",
          c0.enabled is True and cfg.addressing.onvif_base_port == 18600)
    check("config: odd frame sizes are made even",
          (c0.profiles[0].width, c0.profiles[0].height) == (640, 360),
          f"{c0.profiles[0].width}x{c0.profiles[0].height}")
    check("config: zero fps is clamped", c0.profiles[0].fps >= 1)
    check("config: duplicate profile tokens are renamed",
          c0.profiles[0].token != c0.profiles[1].token)
    check("config: duplicate camera names are renamed",
          cfg.cameras[1].name != "Dup" and notes, cfg.cameras[1].name)
    try:
        from_dict(AppCfg, {"cameras": "nope"})
        bad = False
    except TypeError:
        bad = True
    check("config: a non-list camera section is a clean error", bad)

    # --------------------------------------------------- multi-slice index
    sliced = ROOT / "media_cache" / "_sliced.h264"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-t", "2",
                    "-an", "-c:v", "libx264", "-preset", "veryfast",
                    "-x264-params", "keyint=25:min-keyint=25:scenecut=0:"
                    "bframes=0:slices=4:repeat-headers=1",
                    "-f", "h264", str(sliced)], check=True)
    idx = media.build_index(sliced, "H264", 25, 1280, 720, 2048)
    nf = len(idx["frames"])
    check("index: a 4-slice encode still yields one access unit per frame",
          45 <= nf <= 55 and all(len(f[1]) >= 4 for f in idx["frames"]),
          f"{nf} frames, first AU has {len(idx['frames'][0][1])} NALs")
    check("index: keyframes land every GOP", idx["frames"][0][0] == 1
          and idx["frames"][25][0] == 1 and idx["frames"][1][0] == 0)

    # ------------------------------------------------------------ engine
    cams = []
    for i in (1, 2):
        c = CameraCfg(name=f"EdgeCam {i:02d}", video=str(src), import_seconds=4)
        c.ensure_profiles()
        for p in c.profiles:
            p.width, p.height, p.bitrate = 640, 360, 400
        cams.append(c)
    cfg = AppCfg(media_dir=str(ROOT / "media_cache"),
                 addressing=AddressingCfg(mode="port", advertise_ip="127.0.0.1",
                                          onvif_base_port=ONVIF, rtsp_base_port=RTSP),
                 cameras=cams)
    eng = Engine(cfg, log_cb=lambda l: print("   |", l))
    eng.start()
    for _ in range(240):
        if eng.phase == "running":
            break
        time.sleep(0.5)
    try:
        check("engine: both cameras running", len(eng.running_cameras()) == 2,
              str([c.state for c in eng.cams]))
        if len(eng.running_cameras()) != 2:
            return
        cam = eng.running_cameras()[0]
        u, p = cam.cfg.username, cam.cfg.password
        dev = f"{cam.base_url}/onvif/device_service"
        med = f"{cam.base_url}/onvif/media_service"
        med2 = f"{cam.base_url}/onvif/media2_service"
        evt = f"{cam.base_url}/onvif/event_service"
        host, port = "127.0.0.1", cam.cfg.onvif_port

        # duplicate names never reach the allocator: start repairs and says so
        notes = []
        e2 = Engine(AppCfg(cameras=[CameraCfg(name="X", video="a"),
                                    CameraCfg(name="X", video="b")]),
                    log_cb=notes.append)
        e2.cfg.addressing.onvif_base_port, e2.cfg.addressing.rtsp_base_port = 18690, 18695
        e2.cfg.discovery_enabled = False
        e2.start()
        time.sleep(0.5)
        e2.stop()
        names = [c.cfg.name for c in e2.cams]
        check("engine: duplicate camera names are repaired and logged",
              len(set(names)) == 2 and any("config repaired" in n for n in notes),
              str(names))

        # ---- WS-Security with a naive Created
        xml = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
               f'{wsse_naive(u, p)}<s:Body><GetDeviceInformation xmlns="{DEV_NS}"/>'
               '</s:Body></s:Envelope>').encode()
        import urllib.request
        r = urllib.request.urlopen(urllib.request.Request(
            dev, data=xml, headers={"Content-Type": "application/soap+xml"}), timeout=10)
        check("wsse: a Created without a time zone is accepted as UTC",
              cam.cfg.serial in r.read().decode())

        # ---- HTTP: chunked body, Expect: 100-continue, HTTP/1.0 closes
        body = (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/'
                f'soap-envelope"><s:Body><GetSystemDateAndTime xmlns="{DEV_NS}"/>'
                f'</s:Body></s:Envelope>').encode()
        half = len(body) // 2
        chunked = (f"POST /onvif/device_service HTTP/1.1\r\nHost: {host}\r\n"
                   f"Transfer-Encoding: chunked\r\nContent-Type: application/soap+xml"
                   f"\r\nConnection: close\r\n\r\n").encode()
        chunked += f"{half:x}\r\n".encode() + body[:half] + b"\r\n"
        chunked += f"{len(body) - half:x}\r\n".encode() + body[half:] + b"\r\n0\r\n\r\n"
        resp = raw_http(host, port, chunked)
        check("http: chunked request bodies are decoded",
              b"200 OK" in resp and b"UTCDateTime" in resp, resp[:40])
        expect = (f"POST /onvif/device_service HTTP/1.1\r\nHost: {host}\r\n"
                  f"Expect: 100-continue\r\nContent-Length: {len(body)}\r\n"
                  f"Content-Type: application/soap+xml\r\n\r\n").encode()
        s = socket.create_connection((host, port), timeout=6)
        try:
            s.sendall(expect)
            first = s.recv(4096)
            check("http: Expect: 100-continue gets an interim 100", b"100 Continue" in first, first[:30])
            s.sendall(body)
            rest = b""
            s.settimeout(4)
            try:
                while True:
                    b = s.recv(65536)
                    if not b:
                        break
                    rest += b
                    if b"</s:Envelope>" in rest:
                        break
            except socket.timeout:
                pass
            check("http: the body after 100-continue is answered", b"UTCDateTime" in rest)
        finally:
            s.close()
        one0 = (f"POST /onvif/device_service HTTP/1.0\r\nHost: {host}\r\n"
                f"Content-Length: {len(body)}\r\nContent-Type: application/soap+xml"
                f"\r\n\r\n").encode() + body
        s = socket.create_connection((host, port), timeout=6)
        try:
            s.sendall(one0)
            got = b""
            s.settimeout(4)
            closed = False
            try:
                while True:
                    b = s.recv(65536)
                    if not b:
                        closed = True
                        break
                    got += b
            except socket.timeout:
                pass
            check("http: HTTP/1.0 without keep-alive is closed after the reply",
                  closed and b"Connection: close" in got and b"UTCDateTime" in got)
        finally:
            s.close()
        resp = raw_http(host, port, (f"HEAD /snapshot HTTP/1.1\r\nHost: {host}\r\n"
                                     f"Connection: close\r\n\r\n").encode())
        check("http: HEAD gets headers only", resp.startswith(b"HTTP/1.1 401")
              and resp.endswith(b"\r\n\r\n"), resp[:30])
        resp = raw_http(host, port, (f"DELETE /onvif/device_service HTTP/1.1\r\nHost: {host}\r\n"
                                     f"Connection: close\r\n\r\n").encode())
        check("http: an unsupported method is 405, not a dropped socket",
              resp.startswith(b"HTTP/1.1 405"), resp[:30])

        # ---- ONVIF faults and shapes
        code, _, r = soap_call_status(
            dev, f'<GetSystemDateAndTime xmlns="{DEV_NS}"/>')
        check("onvif: GetSystemDateAndTime carries LocalDateTime", "LocalDateTime" in r)
        r = soap_call(med, f'<GetProfile xmlns="{MED_NS}"><ProfileToken>nope'
                           '</ProfileToken></GetProfile>', u, p)
        check("onvif: unknown ProfileToken is ter:NoProfile", "NoProfile" in r)
        r = soap_call(med, f'<GetStreamUri xmlns="{MED_NS}"><ProfileToken>nope'
                           '</ProfileToken></GetStreamUri>', u, p)
        check("onvif: GetStreamUri with a bad token faults too", "NoProfile" in r)
        r = soap_call(med2, f'<GetVideoEncoderConfigurations xmlns="{MED2_NS}"/>', u, p)
        check("onvif: Media2 GetVideoEncoderConfigurations uses tr2:Configurations",
              r.count("<tr2:Configurations") == 2 and "<tt:VideoEncoderConfiguration" not in r)
        r = soap_call(med2, f'<GetVideoEncoderConfigurationOptions xmlns="{MED2_NS}"/>', u, p)
        check("onvif: Media2 encoder options are tr2:Options", "<tr2:Options" in r)
        r = soap_call(dev, f'<GetNetworkProtocols xmlns="{DEV_NS}"/>', u, p)
        check("onvif: GetNetworkProtocols lists RTSP on the real port",
              f"<tt:Port>{cam.cfg.rtsp_port}</tt:Port>" in r)
        r = soap_call(evt, f'<GetEventProperties xmlns="{EVT_NS}"/>', u, p)
        check("onvif: xs: prefix is declared in event properties",
              'xmlns:xs="http://www.w3.org/2001/XMLSchema"' in r and "xs:boolean" in r)

        # an operation that raises must come back as a Fault, not a dead socket
        orig = cam.onvif.op_GetHostname

        def boom(el, path):
            raise ValueError("synthetic")
        cam.onvif.op_GetHostname = boom
        code, _, r = soap_call_status(dev, f'<GetHostname xmlns="{DEV_NS}"/>')
        # unauthenticated -> 401 first; authenticate via WS-Security instead
        r = soap_call(dev, f'<GetHostname xmlns="{DEV_NS}"/>', u, p)
        cam.onvif.op_GetHostname = orig
        check("onvif: an internal error answers with a SOAP Fault",
              "<s:Fault>" in r and "Internal error" in r)
        r = soap_call(dev, f'<GetHostname xmlns="{DEV_NS}"/>', u, p)
        check("onvif: the camera keeps answering afterwards", "HostnameInformation" in r)

        # ---- events: expiry and bare-URL pulls
        r = soap_call(evt, f'<CreatePullPointSubscription xmlns="{EVT_NS}">'
                           '<InitialTerminationTime>PT2S</InitialTerminationTime>'
                           '</CreatePullPointSubscription>', u, p)
        addr = r.split("<wsa:Address>")[1].split("</wsa:Address>")[0] if "<wsa:Address>" in r else ""
        check("events: short subscription created", "sub=" in addr, addr)
        eng.trigger_motion(cam.cfg.name, True)
        time.sleep(0.3)
        r = soap_call(evt, f'<PullMessages xmlns="{EVT_NS}"><Timeout>PT1S</Timeout>'
                           '<MessageLimit>5</MessageLimit></PullMessages>', u, p)
        check("events: a pull on the bare event URL reaches the only subscription",
              "IsMotion" in r)
        # the subscription ends while this pull is waiting: a real device
        # faults the pull at once rather than answering after the timeout
        t0 = time.time()
        r = soap_call(addr, f'<PullMessages xmlns="{EVT_NS}"><Timeout>PT10S</Timeout>'
                            '<MessageLimit>5</MessageLimit></PullMessages>', u, p, timeout=30)
        check("events: expiry wakes a waiting pull immediately",
              "Subscription not found" in r and time.time() - t0 < 4.0,
              f"{time.time() - t0:.1f}s")
        check("events: an expired subscription is gone", not cam.onvif.events.subs,
              f"{len(cam.onvif.events.subs)} live")
        r = soap_call(evt, f'<CreatePullPointSubscription xmlns="{EVT_NS}">'
                           '<InitialTerminationTime>PT60S</InitialTerminationTime>'
                           '</CreatePullPointSubscription>', u, p)
        addr = r.split("<wsa:Address>")[1].split("</wsa:Address>")[0] if "<wsa:Address>" in r else ""
        t0 = time.time()
        r = soap_call(addr, f'<PullMessages xmlns="{EVT_NS}"><Timeout>PT3S</Timeout>'
                            '<MessageLimit>5</MessageLimit></PullMessages>', u, p, timeout=20)
        check("events: an empty pull waits its timeout without spinning",
              "PullMessagesResponse" in r and 2.5 <= time.time() - t0 <= 5.0,
              f"{time.time() - t0:.1f}s")
        term = r.split("<tev:TerminationTime>")[1].split("<")[0] if "TerminationTime" in r else ""
        cur = r.split("<tev:CurrentTime>")[1].split("<")[0] if "CurrentTime" in r else ""
        check("events: PullMessages reports the real termination time",
              term > cur and term[:13] == cur[:13], f"{cur} -> {term}")
        soap_call(addr, '<Unsubscribe xmlns="http://docs.oasis-open.org/wsn/b-2"/>', u, p)
        check("events: Unsubscribe removes it", not cam.onvif.events.subs)

        # ---- discovery honours the Types filter
        def probe(types: str) -> int:
            xml = ('<?xml version="1.0"?><e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
                   'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
                   'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
                   'xmlns:dn="http://www.onvif.org/ver10/network/wsdl" '
                   'xmlns:wsdp="http://schemas.xmlsoap.org/ws/2006/02/devprof">'
                   '<e:Header><w:MessageID>uuid:edge-1</w:MessageID>'
                   '<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>'
                   f'</e:Header><e:Body><d:Probe><d:Types>{types}</d:Types></d:Probe>'
                   '</e:Body></e:Envelope>').encode()
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(2.5)
            n = 0
            try:
                s.sendto(xml, ("239.255.255.250", 3702))
                end = time.time() + 2.5
                while time.time() < end:
                    try:
                        data, _ = s.recvfrom(65535)
                    except socket.timeout:
                        break
                    if b"EdgeCam" in data:
                        n += data.count(b"<d:ProbeMatch>")
            finally:
                s.close()
            return n
        check("discovery: a probe for wsdp:Device gets no camera matches",
              probe("wsdp:Device") == 0)
        check("discovery: a probe for tds:Device is answered", probe("tds:Device") >= 2)
        check("discovery: matches advertise tds:Device too",
              b"tds:Device" in __import__("vcamsim.discovery", fromlist=["x"])._match(cam).encode())

        # ---- RTSP
        rp = cam.cfg.rtsp_port
        r = rtsp_exchange(host, rp, [f"OPTIONS rtsp://{host}:{rp}/Streaming/Channels/101 RTSP/1.0",
                                     "CSeq: 1"])
        check("rtsp: OPTIONS answers", "RTSP/1.0 200" in r and "DESCRIBE" in r)
        digest_ok = False
        # SETUP with a multicast-then-TCP transport list must pick TCP
        import re as _re
        r = rtsp_exchange(host, rp, [f"DESCRIBE rtsp://{host}:{rp}/Streaming/Channels/101 RTSP/1.0",
                                     "CSeq: 2", "Accept: application/sdp"])
        m = _re.search(r'nonce="([^"]+)"', r)
        if m:
            nonce = m.group(1)
            uri = f"rtsp://{host}:{rp}/Streaming/Channels/101/trackID=0"
            ha1 = hashlib.md5(f"{u}:IPCamera:{p}".encode()).hexdigest()
            ha2 = hashlib.md5(f"SETUP:{uri}".encode()).hexdigest()
            resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
            auth = (f'Digest username="{u}", realm="IPCamera", nonce="{nonce}", '
                    f'uri="{uri}", response="{resp}"')
            r = rtsp_exchange(host, rp, [f"SETUP {uri} RTSP/1.0", "CSeq: 3",
                                         f"Authorization: {auth}",
                                         "Transport: RTP/AVP;multicast;port=5000-5001,"
                                         "RTP/AVP/TCP;unicast;interleaved=2-3"])
            digest_ok = "RTSP/1.0 200" in r and "interleaved=2-3" in r
        check("rtsp: a multicast-then-TCP Transport list picks TCP", digest_ok, r[:60])
        r = rtsp_exchange(host, rp, [f"SETUP rtsp://{host}:{rp}/Streaming/Channels/101 RTSP/1.0",
                                     "CSeq: 4", "Transport: RTP/AVP;multicast;port=5000-5001"])
        check("rtsp: multicast-only is 461 (or 401 first)",
              "RTSP/1.0 461" in r or "RTSP/1.0 401" in r, r[:30])
        cam.cfg.faults.rtsp_offline = True
        r = rtsp_exchange(host, rp, [f"OPTIONS rtsp://{host}:{rp}/ RTSP/1.0", "CSeq: 5"], timeout=3)
        cam.cfg.faults.rtsp_offline = False
        check("rtsp: an offline camera does not even answer OPTIONS", r == "")

        # ---- timeline is monotonic-clock based
        v = cam.variants["profile_1"]
        f1 = cam.live_frame(v, align_keyframe=False)
        time.sleep(1.0)
        f2 = cam.live_frame(v, align_keyframe=False)
        check("timeline: advances ~fps per second", 20 <= f2 - f1 <= 30, str(f2 - f1))

        # ---- stop while a client streams, then start again immediately
        url = cam.rtsp_url(cam.cfg.profiles[0]).replace("rtsp://", f"rtsp://{u}:{p}@")
        proc = subprocess.Popen(["ffmpeg", "-v", "quiet", "-rtsp_transport", "tcp", "-i", url,
                                 "-t", "30", "-f", "null", "-"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 15
        while time.time() < deadline and cam.clients < 1:
            time.sleep(0.2)
        check("stream: a client is counted", cam.clients >= 1, str(cam.clients))
        t0 = time.time()
        eng.stop()
        check("engine: stop with a live client completes promptly",
              time.time() - t0 < 10, f"{time.time() - t0:.1f}s")
        proc.wait(timeout=15)
        eng.start()
        for _ in range(120):
            if eng.phase == "running":
                break
            time.sleep(0.5)
        check("engine: restarts cleanly on the same ports",
              len(eng.running_cameras()) == 2, str([c.state for c in eng.cams]))
        # a second start while the first is stopping is refused, not doubled
        eng._stopping.set()
        eng.running = False
        try:
            eng.start()
            doubled = True
        except RuntimeError as ex:
            doubled = "still stopping" not in str(ex)
        finally:
            eng._stopping.clear()
            eng.running = True
        check("engine: start while stopping is refused", not doubled)
    finally:
        eng.stop()

    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
