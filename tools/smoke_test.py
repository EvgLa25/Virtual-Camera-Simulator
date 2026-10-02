"""End-to-end smoke test: ONVIF + RTSP + snapshot + discovery + events.

Usage:  python tools/smoke_test.py [source_video.mp4]
Creates a 10 s test clip with ffmpeg if no video is given.
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
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vcamsim.config import AddressingCfg, AppCfg, CameraCfg   # noqa: E402
from vcamsim.engine import Engine                              # noqa: E402

OK, FAIL = [], []


def check(name, cond, detail=""):
    (OK if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {detail}" if detail else ""))


def make_test_video(path: Path, seconds=10):
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", f"testsrc=size=1280x720:rate=25:duration={seconds}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
                   check=True)
    return path


def wsse(user, pwd) -> str:
    nonce = os.urandom(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    dig = base64.b64encode(
        hashlib.sha1(nonce + created.encode() + pwd.encode()).digest()).decode()
    return (
        '<s:Header><Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/'
        '2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
        f'<UsernameToken><Username>{user}</Username>'
        '<Password Type="http://docs.oasis-open.org/wss/2004/01/'
        f'oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{dig}</Password>'
        '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/'
        'oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
        f'{base64.b64encode(nonce).decode()}</Nonce>'
        '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/'
        f'oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>'
        '</UsernameToken></Security></s:Header>')


def soap_call_status(url, body, timeout=15):
    """-> (status, headers, text) without WS-Security."""
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
           f'<s:Body>{body}</s:Body></s:Envelope>').encode()
    req = urllib.request.Request(url, data=xml,
                                 headers={"Content-Type": "application/soap+xml"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


def soap_call_httpdigest(url, body, user, pwd, timeout=15) -> str:
    """SOAP with NO WS-Security -- authenticate with HTTP Digest only."""
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
           f'<s:Body>{body}</s:Body></s:Envelope>').encode()
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, url, user, pwd)
    op = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))
    req = urllib.request.Request(url, data=xml,
                                 headers={"Content-Type": "application/soap+xml"})
    try:
        return op.open(req, timeout=timeout).read().decode("utf-8", "replace")
    except Exception as e:
        try:
            return e.read().decode("utf-8", "replace")
        except Exception:
            return f"<error>{e}</error>"


def soap_call(url, body, user=None, pwd=None, timeout=15) -> str:
    hdr = wsse(user, pwd) if user else ""
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
           f'{hdr}<s:Body>{body}</s:Body></s:Envelope>').encode()
    req = urllib.request.Request(url, data=xml,
                                 headers={"Content-Type": "application/soap+xml"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.read().decode("utf-8", "replace")


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else make_test_video(
        ROOT / "media_cache" / "_testsrc.mp4")
    print(f"source video: {src}")

    cams = []
    for i in (1, 2):
        c = CameraCfg(name=f"SmokeCam {i:02d}", video=str(src))
        c.ensure_profiles()
        for p in c.profiles:                      # keep the test fast
            p.width, p.height = (1280, 720) if p.token == "profile_1" else (640, 360)
            p.fps, p.bitrate = 25, (2048 if p.token == "profile_1" else 400)
        c.motion_interval_sec = 0
        cams.append(c)

    cfg = AppCfg(media_dir=str(ROOT / "media_cache"),
                 addressing=AddressingCfg(mode="port", advertise_ip="127.0.0.1",
                                          onvif_base_port=18000, rtsp_base_port=18550),
                 cameras=cams)

    eng = Engine(cfg, log_cb=lambda l: print("   |", l))
    print("\n--- starting engine (first run transcodes) ---")
    eng.start()
    for _ in range(120):
        if len(eng.running_cameras()) == len(cams):
            break
        time.sleep(0.5)
    print("--- running ---\n")

    try:
        st = eng.status()
        check("both cameras running", len(eng.running_cameras()) == 2,
              json.dumps([s["state"] for s in st]))
        if not eng.running_cameras():
            return

        cam = eng.running_cameras()[0]
        dev = f"{cam.base_url}/onvif/device_service"
        med = f"{cam.base_url}/onvif/media_service"
        med2 = f"{cam.base_url}/onvif/media2_service"
        evt = f"{cam.base_url}/onvif/event_service"
        u, p = cam.cfg.username, cam.cfg.password

        r = soap_call(dev, '<GetSystemDateAndTime xmlns="http://www.onvif.org/ver10/device/wsdl"/>')
        check("GetSystemDateAndTime (unauthenticated)", "UTCDateTime" in r)

        r = soap_call(dev, '<GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/>')
        check("GetDeviceInformation rejects missing auth", "NotAuthorized" in r)

        code, hdrs, r = soap_call_status(
            dev, '<GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/>')
        wa = hdrs.get("WWW-Authenticate", "")
        check("401 carries a WWW-Authenticate Digest challenge",
              code == 401 and wa.lower().startswith("digest"), f"{code} {wa[:40]}")

        r = soap_call_httpdigest(
            dev, '<GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/>',
            u, p)
        check("SOAP accepts HTTP Digest instead of WS-Security",
              cam.cfg.serial in r)

        r = soap_call_httpdigest(
            dev, '<GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/>',
            u, "wrongpass")
        check("HTTP Digest with a bad password is rejected",
              cam.cfg.serial not in r)

        code, _, r = soap_call_status(
            med, '<GetProfiles xmlns="http://www.onvif.org/ver10/media/wsdl"/>')
        check("media service also challenges unauthenticated callers", code == 401)

        r = soap_call(dev, '<GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/>',
                      u, "wrongpass")
        check("GetDeviceInformation rejects bad password", "NotAuthorized" in r)

        r = soap_call(dev, '<GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/>', u, p)
        check("GetDeviceInformation with WS digest", cam.cfg.serial in r)

        r = soap_call(dev, '<GetCapabilities xmlns="http://www.onvif.org/ver10/device/wsdl"/>')
        check("GetCapabilities", "media_service" in r and "event_service" in r)

        r = soap_call(dev, '<GetServices xmlns="http://www.onvif.org/ver10/device/wsdl">'
                           '<IncludeCapability>false</IncludeCapability></GetServices>')
        check("GetServices lists media2", "ver20/media/wsdl" in r)

        r = soap_call(med, '<GetProfiles xmlns="http://www.onvif.org/ver10/media/wsdl"/>', u, p)
        check("Media1 GetProfiles returns 2 profiles", r.count("<trt:Profiles") == 2)

        r = soap_call(med2, '<GetProfiles xmlns="http://www.onvif.org/ver20/media/wsdl"/>', u, p)
        check("Media2 GetProfiles", r.count("<tr2:Profiles") == 2)

        r = soap_call(med, '<GetStreamUri xmlns="http://www.onvif.org/ver10/media/wsdl">'
                           '<StreamSetup><Stream xmlns="http://www.onvif.org/ver10/schema">'
                           'RTP-Unicast</Stream><Transport xmlns="http://www.onvif.org/ver10/schema">'
                           '<Protocol>RTSP</Protocol></Transport></StreamSetup>'
                           '<ProfileToken>profile_1</ProfileToken></GetStreamUri>', u, p)
        uri = ""
        if "<tt:Uri>" in r:
            uri = r.split("<tt:Uri>")[1].split("</tt:Uri>")[0]
        check("GetStreamUri", uri.startswith("rtsp://"), uri)

        r = soap_call(med, '<GetSnapshotUri xmlns="http://www.onvif.org/ver10/media/wsdl">'
                           '<ProfileToken>profile_1</ProfileToken></GetSnapshotUri>', u, p)
        snap = r.split("<tt:Uri>")[1].split("</tt:Uri>")[0] if "<tt:Uri>" in r else ""
        check("GetSnapshotUri", snap.startswith("http://"), snap)

        # ---- snapshot over HTTP digest
        if snap:
            mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            mgr.add_password(None, snap, u, p)
            op = urllib.request.build_opener(urllib.request.HTTPDigestAuthHandler(mgr))
            try:
                data = op.open(snap, timeout=10).read()
            except Exception as e:
                data = b""
                print("      snapshot error:", e)
            check("snapshot JPEG over HTTP digest",
                  data[:2] == b"\xff\xd8" and len(data) > 2000, f"{len(data)} bytes")

        # ---- RTSP pull with ffprobe (TCP interleaved, digest auth)
        for tok, path_i in (("profile_1", 0), ("profile_2", 1)):
            url = cam.rtsp_url(cam.cfg.profiles[path_i])
            authed = url.replace("rtsp://", f"rtsp://{u}:{p}@")
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
                 "-print_format", "json", "-show_streams", "-read_intervals", "%+#50",
                 "-timeout", "5000000", authed],
                capture_output=True, timeout=60)
            info = {}
            try:
                info = json.loads(r.stdout.decode() or "{}")
            except json.JSONDecodeError:
                pass
            s = (info.get("streams") or [{}])[0]
            check(f"RTSP/TCP stream {tok}",
                  s.get("codec_name") in ("h264", "hevc"),
                  f"{s.get('codec_name')} {s.get('width')}x{s.get('height')}")

        url = cam.rtsp_url(cam.cfg.profiles[0]).replace("rtsp://", f"rtsp://{u}:{p}@")
        r = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "udp",
                            "-print_format", "json", "-show_streams",
                            "-read_intervals", "%+#30", url],
                           capture_output=True, timeout=60)
        try:
            s = (json.loads(r.stdout.decode() or "{}").get("streams") or [{}])[0]
        except json.JSONDecodeError:
            s = {}
        check("RTSP/UDP stream", s.get("codec_name") in ("h264", "hevc"),
              str(s.get("codec_name")))

        # ---- continuous loop: record longer than the source clip
        url = cam.rtsp_url(cam.cfg.profiles[0]).replace("rtsp://", f"rtsp://{u}:{p}@")
        out = ROOT / "media_cache" / "_loopcheck.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-rtsp_transport", "tcp",
                        "-i", url, "-t", "15", "-c", "copy", str(out)],
                       capture_output=True, timeout=120)
        r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                            "-show_format", "-count_frames", "-select_streams", "v:0",
                            "-show_streams", str(out)], capture_output=True, timeout=120)
        try:
            info = json.loads(r.stdout.decode() or "{}")
        except json.JSONDecodeError:
            info = {}
        dur = float(info.get("format", {}).get("duration", 0) or 0)
        nf = int((info.get("streams") or [{}])[0].get("nb_read_frames", 0) or 0)
        check("continuous loop past end of a 10s clip (15s recorded)",
              dur >= 14.0 and nf >= 340, f"{dur:.1f}s / {nf} frames")

        # ---- shared live timeline (real cameras have one, clients join mid-flight)
        v = cam.variants["profile_1"]
        f1 = cam.live_frame(v)
        time.sleep(2.0)
        f2 = cam.live_frame(v)
        check("live timeline advances with wall clock",
              45 <= (f2 - f1) <= 55, f"+{f2 - f1} frames in 2s @ {v.fps}fps")
        check("clients join on a keyframe", (f2 % len(v.frames)) in v.keyframes,
              f"frame {f2 % len(v.frames)}")
        check("timeline runs past the clip length (keeps looping)",
              f2 > 0 and cam.live_frame(v, align_keyframe=False) >= f2)

        # ---- events
        r = soap_call(evt, '<CreatePullPointSubscription '
                           'xmlns="http://www.onvif.org/ver10/events/wsdl">'
                           '<InitialTerminationTime>PT60S</InitialTerminationTime>'
                           '</CreatePullPointSubscription>', u, p)
        addr = ""
        if "<wsa:Address>" in r:
            addr = r.split("<wsa:Address>")[1].split("</wsa:Address>")[0]
        check("CreatePullPointSubscription", "sub=" in addr, addr)
        if addr:
            eng.trigger_motion(cam.cfg.name, True)
            time.sleep(0.5)
            r = soap_call(addr, '<PullMessages xmlns="http://www.onvif.org/ver10/events/wsdl">'
                                '<Timeout>PT5S</Timeout><MessageLimit>10</MessageLimit>'
                                '</PullMessages>', u, p, timeout=20)
            check("PullMessages delivers motion",
                  "CellMotionDetector/Motion" in r and 'Value="true"' in r)

        # ---- WS-Discovery
        probe = ('<?xml version="1.0" encoding="UTF-8"?>'
                 '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
                 'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
                 'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
                 'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
                 '<e:Header><w:MessageID>uuid:smoke-test-0001</w:MessageID>'
                 '<w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
                 '<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>'
                 '</e:Header><e:Body><d:Probe>'
                 '<d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe>'
                 '</e:Body></e:Envelope>').encode()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(4)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        matches = 0
        try:
            s.sendto(probe, ("239.255.255.250", 3702))
            end = time.time() + 4
            while time.time() < end:
                try:
                    data, _ = s.recvfrom(65535)
                except socket.timeout:
                    break
                if b"ProbeMatch" in data:
                    matches += data.count(b"<d:ProbeMatch>")
        finally:
            s.close()
        check("WS-Discovery ProbeMatch for every camera", matches >= 2, f"{matches} matches")

        # ---- fault injection
        cam.cfg.faults.reject_auth = True
        r = soap_call(dev, '<GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/>', u, p)
        check("fault: reject_auth blocks a valid login", "NotAuthorized" in r)
        cam.cfg.faults.reject_auth = False

        cam.cfg.faults.soap_fault = True
        r = soap_call(dev, '<GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/>', u, p)
        check("fault: soap_fault returns a SOAP Fault", "s:Fault" in r)
        cam.cfg.faults.soap_fault = False

        cam.cfg.faults.clock_drift_sec = 600
        r = soap_call(dev, '<GetDeviceInformation xmlns="http://www.onvif.org/ver10/device/wsdl"/>', u, p)
        check("fault: clock_drift breaks WS digest", "NotAuthorized" in r)
        cam.cfg.faults.clock_drift_sec = 0

        cam.cfg.faults.onvif_offline = True
        try:
            soap_call(dev, '<GetSystemDateAndTime '
                           'xmlns="http://www.onvif.org/ver10/device/wsdl"/>', timeout=4)
            dead = False
        except Exception:
            dead = True
        check("fault: onvif_offline drops the connection", dead)
        cam.cfg.faults.onvif_offline = False

    finally:
        print("\n--- stopping ---")
        eng.stop()

    print(f"\n{len(OK)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed:", ", ".join(FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
