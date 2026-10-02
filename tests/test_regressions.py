import asyncio
import dataclasses
import os
import sys
import threading
import time
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from vcamsim import addressing, media, soap
from vcamsim.config import AppCfg, CameraCfg, ProfileCfg, to_dict
from vcamsim.daemon import Runner
from vcamsim.engine import CameraRuntime, Engine
from vcamsim.proxy import LocalEngine
from vcamsim.rtsp import RtspConnection, RtspServer, Session


def camera():
    cfg = CameraCfg(name="Test camera", ip="127.0.0.1", onvif_port=18000)
    cfg.ensure_profiles()
    return CameraRuntime(cfg, Engine(AppCfg(cameras=[cfg])))


@pytest.mark.parametrize("token", ["A&B", "<missing>", "'quoted'", 'a"b'])
def test_unknown_profile_is_a_parseable_fault(token):
    request = ET.Element("GetStreamUri")
    ET.SubElement(request, "ProfileToken").text = token
    response = camera().onvif.op_GetStreamUri(request, "/onvif/media_service")
    root = ET.fromstring(response)
    assert "NoProfile" in "".join(root.itertext())
    assert token in "".join(root.itertext())


def test_capabilities_parse_without_fault_injection():
    cam = camera()
    status, _, body, _ = asyncio.run(cam.onvif.handle(
        "/onvif/device_service", soap.envelope("<tds:GetCapabilities/>"), "test"))
    assert status == 200
    assert ET.fromstring(body).find(".//{http://www.onvif.org/ver10/schema}Media") is not None


def test_cache_separates_sources_and_gop(tmp_path):
    paths = [tmp_path / sub / "clip.mp4" for sub in ("a", "b")]
    for path, content in zip(paths, (b"AAAA", b"BBBB")):
        path.parent.mkdir()
        path.write_bytes(content)
        os.utime(path, (1700000000, 1700000000))
    profile = ProfileCfg()
    assert media.variant_key(str(paths[0]), profile) != media.variant_key(str(paths[1]), profile)
    assert media.variant_key(str(paths[0]), profile) != media.variant_key(
        str(paths[0]), dataclasses.replace(profile, gop=100))
    assert profile.key() == dataclasses.replace(profile, gop=profile.fps).key()


def test_alias_cleanup_owns_only_new_addresses(monkeypatch):
    calls = []
    def netsh(args):
        calls.append(args)
        if "add" in args and "address=10.10.1.100" in args:
            return 1, "already exists"
        return 0, ""
    monkeypatch.setattr(addressing, "is_admin", lambda: True)
    monkeypatch.setattr(addressing, "_netsh", netsh)
    allocator = addressing.AddressAllocator(addressing.AddressingCfg(mode="alias"), log=lambda _: None)
    allocator.allocate([CameraCfg(), CameraCfg()])
    assert allocator.alias_active
    allocator.release()
    deletes = [c for c in calls if "delete" in c]
    assert len(deletes) == 1
    assert "address=10.10.1.101" in deletes[0]


def test_failed_save_preserves_service_configuration(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    old = AppCfg(cameras=[CameraCfg(name="Original")])
    old.save(path)
    before = path.read_bytes()
    runner = Runner(str(path))
    stopped = []
    monkeypatch.setattr(runner, "_stop_engine", lambda: stopped.append(True))
    def fail_save(*_):
        raise OSError("disk full")
    monkeypatch.setattr(AppCfg, "save", fail_save)
    assert not runner.replace_config({"cameras": [{"name": "Changed"}]})
    assert runner.cfg.cameras[0].name == "Original"
    assert path.read_bytes() == before
    assert not stopped


def test_silent_import_is_cancelled_promptly():
    abort = threading.Event()
    timer = threading.Timer(0.1, abort.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(media.Aborted):
            media._run([sys.executable, "-c", "import time; time.sleep(10)"], abort.is_set, poll=0.03)
    finally:
        timer.join()
    assert time.monotonic() - started < 3


def test_progress_uses_microseconds_once():
    progress = []
    rc, _ = media._run([sys.executable, "-c", "print('out_time_us=500000'); print('out_time_ms=500000')"],
                      on_progress=progress.append, total_us=1000000)
    assert rc == 0
    assert progress == [0.5]


def test_local_edits_do_not_mutate_live_config_but_faults_do():
    cfg = AppCfg(cameras=[CameraCfg(name="Camera")])
    cfg.cameras[0].ensure_profiles()
    local = LocalEngine(cfg)
    live_cfg = local.engine.cfg.cameras[0]
    local.engine.cams = [CameraRuntime(live_cfg, local.engine)]
    cfg.cameras[0].profiles[0].width = 640
    cfg.cameras[0].password = "changed"
    assert live_cfg.profiles[0].width == 1920
    assert live_cfg.password == "admin"
    cfg.cameras[0].faults.freeze_video = True
    assert local.push_faults("Camera", cfg.cameras[0].faults)
    assert live_cfg.faults.freeze_video


class Writer:
    def get_extra_info(self, name):
        return ("127.0.0.1", 1)
    def write(self, data):
        pass
    async def drain(self):
        pass


def test_pause_finishes_producer_before_play():
    async def run():
        cam = camera()
        conn = RtspConnection(RtspServer(cam, "127.0.0.1", 0), None, Writer())
        variant = SimpleNamespace(codec="H264", fps=10, vps=b"", sps=b"", pps=b"",
            frames=[[1, []]], prev_keyframe=lambda n: 0, nals=lambda n: (False, [b"\x61\x80"]))
        sess = Session(conn, cam.cfg.profiles[0], variant)
        conn.sessions[sess.id] = sess
        sess.playing = True
        cam.clients = 1
        old = sess.task = asyncio.create_task(conn._stream(sess))
        await asyncio.sleep(0.01)
        headers = {"session": sess.id, "authorization": "Basic YWRtaW46YWRtaW4="}
        await conn._dispatch("PAUSE rtsp://localhost/main RTSP/1.0", headers)
        assert old.done()
        assert cam.clients == 0
        await conn._dispatch("PLAY rtsp://localhost/main RTSP/1.0", headers)
        new = sess.task
        try:
            await asyncio.sleep(0.12)
            assert not new.done()
            assert cam.clients == 1
        finally:
            sess.close()
            await asyncio.gather(new, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("protocol", ["http", "rtsp", "control"])
def test_stop_closes_accepted_idle_connections(protocol, monkeypatch):
    from vcamsim.httpd import HttpServer
    from vcamsim.control import ControlServer
    monkeypatch.setattr("vcamsim.control.ensure_token", lambda: "fixture-token")
    async def run():
        if protocol == "control":
            server = ControlServer(SimpleNamespace(log=lambda _: None), 0)
        else:
            server = (HttpServer if protocol == "http" else RtspServer)(camera(), "127.0.0.1", 0)
        await server.start()
        port = server.server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            await asyncio.sleep(0.03)
            await asyncio.wait_for(server.stop(), timeout=2)
            assert await asyncio.wait_for(reader.read(), timeout=1) == b""
        finally:
            writer.close()
            await writer.wait_closed()
    asyncio.run(run())


def test_service_command_rejection_reaches_gui():
    from vcamsim.proxy import RemoteEngine
    remote = RemoteEngine(AppCfg(), 19999)
    remote.client = SimpleNamespace(engine=lambda action: False)
    assert remote.start() is False
    assert remote.stop() is False
