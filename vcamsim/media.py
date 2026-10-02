"""Phase 1: media import pipeline + shared frame source.

ffmpeg transcodes a source video once per profile into Annex-B elementary
stream with a fixed closed GOP.  We then index every access unit so that at
runtime we only mmap + packetize -- never decode or encode.
"""
from __future__ import annotations

import base64
import hashlib
import bisect
import json
import mmap
import os
import queue
import shutil
import subprocess
import sys
import threading
import uuid
from collections import OrderedDict
from pathlib import Path

_NO_WINDOW = 0x08000000 if hasattr(subprocess, "CREATE_NO_WINDOW") else 0


def _app_dir() -> Path:
    """Folder holding the exe when frozen, else the package's parent."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def find_tool(name: str) -> str:
    """Locate ffmpeg/ffprobe: next to the exe, in ./ffmpeg[/bin], then PATH."""
    exe = name + (".exe" if sys.platform == "win32" else "")
    roots = [_app_dir(), _app_dir() / "ffmpeg", _app_dir() / "ffmpeg" / "bin"]
    if getattr(sys, "_MEIPASS", None):
        meipass = Path(sys._MEIPASS)
        roots = [meipass, meipass / "ffmpeg"] + roots
    for r in roots:
        p = r / exe
        if p.exists():
            return str(p)
    return shutil.which(name) or name


FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")


def tools_available() -> tuple[bool, str]:
    """-> (ok, message). Checked at engine start so failures are obvious."""
    global FFMPEG, FFPROBE
    # re-resolve every start: dropping ffmpeg.exe beside the exe while the
    # program is open must work without a restart
    FFMPEG, FFPROBE = find_tool("ffmpeg"), find_tool("ffprobe")
    try:
        r = subprocess.run([FFMPEG, "-version"], capture_output=True,
                           creationflags=_NO_WINDOW, timeout=20)
        if r.returncode == 0:
            first = r.stdout.decode("utf-8", "replace").splitlines()[:1]
            return True, (first[0] if first else "ffmpeg ok") + f"  [{FFMPEG}]"
        return False, f"ffmpeg at {FFMPEG} returned {r.returncode}"
    except Exception as e:
        return False, (f"ffmpeg not found ({e}). Put ffmpeg.exe next to the "
                       f"program, or in an 'ffmpeg' folder beside it, or on PATH.")


# ----------------------------------------------------------------- annex-b
def parse_annexb(data: bytes) -> list[tuple[int, int]]:
    """Return [(offset, length)] of every NAL payload (start codes stripped)."""
    out = []
    n = len(data)
    i = data.find(b"\x00\x00\x01")
    while i >= 0:
        s = i + 3
        j = data.find(b"\x00\x00\x01", s)
        e = j if j >= 0 else n
        # a 4-byte start code leaves a trailing 0x00 on the previous NAL
        while e > s and data[e - 1] == 0:
            e -= 1
        if e > s:
            out.append((s, e - s))
        i = j
    return out


def build_index(es_path: Path, codec: str, fps: int, width: int, height: int,
                bitrate: int) -> dict:
    # mmap rather than read_bytes(): importing a whole two-hour file would
    # otherwise pull gigabytes into RAM just to find the start codes
    with open(es_path, "rb") as fh:
        if es_path.stat().st_size == 0:
            return {"codec": codec, "fps": fps, "width": width, "height": height,
                    "bitrate": bitrate, "vps": "", "sps": "", "pps": "", "frames": []}
        data = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            return _index_from(data, codec, fps, width, height, bitrate)
        finally:
            data.close()


def _index_from(data, codec: str, fps: int, width: int, height: int,
                bitrate: int) -> dict:
    nals = parse_annexb(data)
    ps = {"vps": b"", "sps": b"", "pps": b""}
    frames: list[list] = []
    pending: list[list[int]] = []

    for off, ln in nals:
        b0 = data[off]
        if codec == "H264":
            typ = b0 & 0x1F
            vcl = 1 <= typ <= 5
            kf = typ == 5
            # first_mb_in_slice is the first ue(v) of the slice header; a
            # leading 1 bit means 0, i.e. the first slice of a new picture
            first = ln >= 2 and bool(data[off + 1] & 0x80)
            if typ == 7:
                ps["sps"] = data[off:off + ln]
                continue
            if typ == 8:
                ps["pps"] = data[off:off + ln]
                continue
            if typ == 9:                     # access unit delimiter, drop
                continue
        else:                                # H265
            typ = (b0 >> 1) & 0x3F
            vcl = typ < 32
            kf = 16 <= typ <= 21
            # first_slice_segment_in_pic_flag is the first bit after the
            # two-byte NAL header
            first = ln >= 3 and bool(data[off + 2] & 0x80)
            if typ == 32:
                ps["vps"] = data[off:off + ln]
                continue
            if typ == 33:
                ps["sps"] = data[off:off + ln]
                continue
            if typ == 34:
                ps["pps"] = data[off:off + ln]
                continue
            if typ == 35:                    # AUD
                continue
        if vcl:
            if frames and not first:
                # another slice of the picture already started (multi-slice
                # encode): it belongs to the same access unit, not a new frame
                frames[-1][1].extend(pending + [[off, ln]])
                pending = []
            else:
                frames.append([1 if kf else 0, pending + [[off, ln]]])
                pending = []
        else:
            pending.append([off, ln])

    return {
        "codec": codec, "fps": fps, "width": width, "height": height,
        "bitrate": bitrate, "nals": sum(len(f[1]) for f in frames),
        "vps": base64.b64encode(ps["vps"]).decode(),
        "sps": base64.b64encode(ps["sps"]).decode(),
        "pps": base64.b64encode(ps["pps"]).decode(),
        "frames": frames,
    }


# ----------------------------------------------------------------- importer
def probe(src: str) -> dict:
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-print_format", "json", "-show_streams",
             "-show_format", src],
            capture_output=True, creationflags=_NO_WINDOW, timeout=30)
        return json.loads(r.stdout.decode("utf-8", "replace") or "{}")
    except Exception:
        return {}


def describe(src: str) -> str:
    """One-line human summary of a source video ('' if it cannot be read)."""
    d = probe(src)
    v = next((s for s in d.get("streams", [])
              if s.get("codec_type") == "video"), None)
    if not v:
        return ""
    dur = float(d.get("format", {}).get("duration", 0) or 0)
    fps = 0.0
    try:
        num, _, den = (v.get("avg_frame_rate") or "0/1").partition("/")
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        pass
    size = int(d.get("format", {}).get("size", 0) or 0)
    bits = [f"{v.get('codec_name', '?')}",
            f"{v.get('width', '?')}x{v.get('height', '?')}"]
    if fps:
        bits.append(f"{fps:.4g} fps")
    if dur:
        bits.append(f"{dur:.1f}s")
    if size:
        bits.append(f"{size / 1048576:.1f} MB")
    return "  ".join(bits)


def _src_stamp(src: str) -> str:
    """Size+mtime of the source, so editing a video invalidates its cache."""
    try:
        s = Path(src).stat()
        return f"{s.st_size}_{s.st_mtime_ns}"
    except OSError:
        return ""


def variant_key(src: str, prof, seconds: int = 0) -> str:
    source = os.path.normcase(str(Path(src).resolve()))
    raw = f"v2|{source}|{_src_stamp(src)}|{prof.key()}|t{max(0, seconds)}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


class Aborted(Exception):
    """Raised when an import is cancelled (the user pressed Stop)."""


class SourceMissing(RuntimeError):
    """The source video is gone and nothing usable is cached for it."""


def find_cached(src: str, cache_dir: Path, prof, seconds: int = 0):
    """Locate a finished import of `src` whose source file no longer exists.

    The cache key folds in the source's size and mtime, so a deleted or moved
    video can no longer be keyed. Every index records what it was built from,
    so the cache can be searched instead -- the camera loops the cached
    stream, it never needs the original again."""
    want = (str(src), prof.key(), max(0, seconds))
    try:
        idxs = sorted(cache_dir.glob("*.idx.json"), key=lambda p: p.stat().st_mtime,
                      reverse=True)
    except OSError:
        return None
    for idx in idxs:
        try:
            d = json.loads(idx.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (d.get("src"), d.get("pkey"), d.get("seconds", 0)) != want:
            # Older caches omitted GOP from the key. Recover a missing source
            # only if the actual indexed keyframe spacing matches the profile.
            legacy = prof.key().rsplit("_g", 1)[0]
            keys = [i for i, fr in enumerate(d.get("frames", [])) if fr[0]]
            if (d.get("src"), d.get("pkey"), d.get("seconds", 0)) != (str(src), legacy, max(0, seconds)):
                continue
            if len(keys) < 2 or any(b - a != (prof.gop or prof.fps) for a, b in zip(keys, keys[1:])):
                continue
        ext = "h264" if d.get("codec") == "H264" else "h265"
        es = idx.with_name(idx.name[: -len(".idx.json")] + "." + ext)
        if es.exists() and es.stat().st_size > 0 and d.get("frames"):
            return es, idx
    return None


def find_cached_snapshots(src: str, cache_dir: Path, width: int, height: int,
                          seconds: int = 0):
    want = {"src": str(src), "w": width, "h": height, "seconds": max(0, seconds)}
    for meta in cache_dir.glob("snap_*/source.json"):
        try:
            if json.loads(meta.read_text(encoding="utf-8")) == want \
                    and any(meta.parent.glob("*.jpg")):
                return meta.parent
        except (OSError, ValueError):
            continue
    return None


def duration_us(src: str) -> int:
    """Source length in microseconds (0 if unknown), for progress reporting."""
    try:
        return int(float(probe(src).get("format", {}).get("duration", 0)) * 1e6)
    except (TypeError, ValueError):
        return 0


def _run(cmd: list[str], abort=None, on_progress=None, total_us: int = 0,
         poll: float = 0.25) -> tuple[int, bytes]:
    """Run ffmpeg cancellably, reporting progress as it goes.

    `cmd` must contain `-progress pipe:1`: the progress stream is what lets us
    both drive `on_progress` and notice `abort()` promptly instead of blocking
    in communicate() until a long encode finishes.
    """
    if abort is not None and abort():
        raise Aborted()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         creationflags=_NO_WINDOW)
    err: list[bytes] = []
    lines: queue.Queue = queue.Queue()
    def read_progress():
        try:
            for raw in p.stdout:
                lines.put(raw)
        finally:
            lines.put(None)

    reader = threading.Thread(target=read_progress, daemon=True)
    t = threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True)
    reader.start()
    t.start()
    try:
        ended = False
        while not ended or p.poll() is None:
            if abort is not None and abort():
                raise Aborted()
            try:
                raw = lines.get(timeout=poll)
            except queue.Empty:
                continue
            if raw is None:
                ended = True
                continue
            line = raw.decode("utf-8", "replace").strip()
            if on_progress and total_us > 0:
                if line.startswith("out_time_us="):
                    val = line.split("=", 1)[1]
                    try:
                        us = int(val)
                    except ValueError:
                        continue
                    on_progress(max(0.0, min(1.0, us / total_us)))
        p.wait()
        t.join(timeout=5)
        return p.returncode, b"".join(err)
    except BaseException:
        p.kill()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        reader.join(timeout=5)
        t.join(timeout=5)
        p.stdout.close()
        p.stderr.close()


def import_variant(src: str, cache_dir: Path, prof, log=print,
                   abort=None, seconds: int = 0,
                   on_progress=None) -> tuple[Path, Path]:
    """Transcode+index one profile variant. Cached; returns (es_path, idx_path).

    `seconds` > 0 imports only that much of the source. The camera loops its
    clip for ever, so importing a two-hour file costs an hour of transcode for
    no behavioural difference."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = variant_key(src, prof, seconds)
    ext = "h264" if prof.codec == "H264" else "h265"
    es = cache_dir / f"{key}.{ext}"
    idx = cache_dir / f"{key}.idx.json"
    if es.exists() and idx.exists() and es.stat().st_size > 0:
        return es, idx
    if not Path(src).exists():
        hit = find_cached(src, cache_dir, prof, seconds)
        if hit:
            log(f"[import] WARNING: {Path(src).name} is missing -- streaming "
                f"the cached copy ({hit[0].name})")
            return hit
        raise SourceMissing(f"video not found: {src}")
    # transcode to a private temp name and publish with an atomic rename, so
    # a second importer (a GUI engine beside the service, two tests) never
    # reads a half-written stream and never clobbers a finished one
    part = cache_dir / f"{key}.{ext}.{uuid.uuid4().hex}.part"

    src_us = duration_us(src)
    if seconds > 0:
        # an unknown duration (raw .h264, some .ts) still gets a real bar
        want_us = min(src_us, seconds * 1_000_000) if src_us > 0 else seconds * 1_000_000
    else:
        want_us = src_us

    gop = prof.gop or prof.fps
    common = [FFMPEG, "-y", "-v", "error", "-nostats", "-progress", "pipe:1",
              "-i", src, "-an"]
    if seconds > 0:
        common += ["-t", str(seconds)]
    common += ["-vf", f"scale={prof.width}:{prof.height},fps={prof.fps}",
               "-pix_fmt", "yuv420p",
               "-b:v", f"{prof.bitrate}k", "-maxrate", f"{prof.bitrate}k",
               "-bufsize", f"{prof.bitrate * 2}k"]
    if prof.codec == "H264":
        cmd = common + [
            "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "main",
            "-x264-params",
            f"keyint={gop}:min-keyint={gop}:scenecut=0:bframes=0:repeat-headers=1",
            "-f", "h264", str(part)]
    else:
        cmd = common + [
            "-c:v", "libx265", "-preset", "veryfast",
            "-x265-params",
            f"keyint={gop}:min-keyint={gop}:scenecut=0:bframes=0:repeat-headers=1",
            "-tag:v", "hvc1", "-f", "hevc", str(part)]

    cap = f" (first {seconds}s)" if seconds > 0 else ""
    log(f"[import] {Path(src).name} -> {prof.codec} "
        f"{prof.width}x{prof.height}@{prof.fps}{cap}")
    try:
        rc, err = _run(cmd, abort, on_progress, want_us)
        if rc != 0 or not part.exists() or part.stat().st_size == 0:
            msg = err.decode("utf-8", "replace")[-800:].strip()
            raise RuntimeError("ffmpeg failed: " + (msg or f"exit code {rc}"))
        data = build_index(part, prof.codec, prof.fps, prof.width, prof.height,
                           prof.bitrate)
        if not data["frames"]:
            raise RuntimeError(
                f"no frames indexed for {es.name} -- the source may not contain "
                f"a decodable video stream")
        if not data["sps"] or not data["pps"]:
            raise RuntimeError(f"{es.name}: the encoder emitted no SPS/PPS")
        # provenance, so find_cached() can recover this entry if the source
        # file disappears later
        data["src"], data["pkey"], data["seconds"] = str(src), prof.key(), max(0, seconds)
        # publish: stream first, index last -- a stream without an index is
        # simply re-imported next time, an index without a stream never exists
        os.replace(part, es)
        tmp_idx = idx.with_suffix(f".{uuid.uuid4().hex}.tmp")
        tmp_idx.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp_idx, idx)
    except BaseException:
        # never leave a half-written elementary stream behind. Cleanup must
        # not mask the real error if Windows still holds the handle a moment.
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    log(f"[import] indexed {len(data['frames'])} frames -> {es.name}")
    return es, idx


def import_snapshots(src: str, cache_dir: Path, width: int, height: int,
                     log=print, abort=None, seconds: int = 0) -> Path:
    """One JPEG per second of source; served as the snapshot URI."""
    key = hashlib.md5(
        f"{src}|{_src_stamp(src)}|{width}x{height}|t{max(0, seconds)}"
        .encode()).hexdigest()[:16]
    out = cache_dir / f"snap_{key}"
    if out.exists() and any(out.glob("*.jpg")):
        return out
    if not Path(src).exists():
        hit = find_cached_snapshots(src, cache_dir, width, height, seconds)
        if hit:
            return hit
        out.mkdir(parents=True, exist_ok=True)      # no snapshots; stream only
        log("[import] WARNING: source missing and no cached snapshots -- "
            "the snapshot URI will be empty")
        return out
    # extract into a private folder and rename it into place, for the same
    # reason as the elementary stream: an aborted run must not leave a
    # half-filled folder that the next start mistakes for a complete set
    part = cache_dir / f"snap_{key}.{uuid.uuid4().hex}.part"
    shutil.rmtree(part, ignore_errors=True)
    part.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-y", "-v", "error", "-nostats", "-progress", "pipe:1",
           "-i", src]
    if seconds > 0:
        cmd += ["-t", str(seconds)]
    cmd += ["-vf", f"fps=1,scale={width}:{height}", "-q:v", "5",
            str(part / "%05d.jpg")]
    try:
        rc, err = _run(cmd, abort)
        (part / "source.json").write_text(json.dumps(
            {"src": str(src), "w": width, "h": height, "seconds": max(0, seconds)}),
            encoding="utf-8")
    except BaseException:
        shutil.rmtree(part, ignore_errors=True)
        raise
    if rc != 0 or not any(part.glob("*.jpg")):
        # non-fatal: the camera still streams, only the snapshot URI is empty
        log(f"[import] WARNING: no snapshots produced -- "
            f"{err.decode('utf-8', 'replace')[-200:].strip() or 'ffmpeg failed'}")
        shutil.rmtree(part, ignore_errors=True)
        out.mkdir(parents=True, exist_ok=True)
        return out
    try:
        os.replace(part, out)
    except OSError:
        # another importer finished first; keep theirs
        shutil.rmtree(part, ignore_errors=True)
    log(f"[import] snapshots -> {out.name}")
    return out


# -------------------------------------------------------------- frame source
class Variant:
    """mmap'd elementary stream + access-unit index. Shared between cameras."""

    def __init__(self, es_path: Path, idx_path: Path):
        self.path = str(es_path)
        d = json.loads(Path(idx_path).read_text(encoding="utf-8"))
        if not d.get("frames"):
            raise RuntimeError(f"{idx_path.name} holds no frames")
        self._fh = open(es_path, "rb")
        try:
            self.mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        except (ValueError, OSError):
            self._fh.close()
            raise RuntimeError(f"{es_path.name} is empty or unreadable")
        self.codec = d["codec"]
        self.fps = int(d["fps"]) or 25
        self.width = d["width"]
        self.height = d["height"]
        self.bitrate = d["bitrate"]
        self.vps = base64.b64decode(d["vps"])
        self.sps = base64.b64decode(d["sps"])
        self.pps = base64.b64decode(d["pps"])
        self.frames = d["frames"]
        self.keyframes = [i for i, fr in enumerate(self.frames) if fr[0]] or [0]
        self.refs = 0

    def prev_keyframe(self, i: int) -> int:
        """Index of the last keyframe at or before i (within one loop)."""
        i %= len(self.frames)
        k = bisect.bisect_right(self.keyframes, i) - 1
        return self.keyframes[k] if k >= 0 else 0

    def nals(self, i: int) -> tuple[bool, list[bytes]]:
        kf, lst = self.frames[i % len(self.frames)]
        return bool(kf), [self.mm[o:o + l] for o, l in lst]

    def close(self):
        for fn in (self.mm.close, self._fh.close):
            try:
                fn()
            except Exception:
                pass


class SnapshotSet:
    """JPEGs for one source, shared between every camera using that video.

    The decoded bytes are kept in a bounded LRU: a 10-minute clip is 600 JPEGs,
    and holding all of them per camera would cost hundreds of MB at 30 cameras.
    """

    MAX_CACHED = 64

    def __init__(self, folder: Path):
        self.files = sorted(Path(folder).glob("*.jpg"))
        self.cache: "OrderedDict[int, bytes]" = OrderedDict()
        self.lock = threading.Lock()

    def __bool__(self) -> bool:
        return bool(self.files)

    def get(self, second: int) -> bytes:
        if not self.files:
            return b""
        i = second % len(self.files)
        with self.lock:
            hit = self.cache.get(i)
            if hit is not None:
                self.cache.move_to_end(i)
                return hit
        try:
            data = self.files[i].read_bytes()
        except OSError:
            return b""
        with self.lock:
            self.cache[i] = data
            while len(self.cache) > self.MAX_CACHED:
                self.cache.popitem(last=False)
        return data


_VARIANTS: dict[str, Variant] = {}
_SNAPS: dict[str, SnapshotSet] = {}
_LOCK = threading.Lock()


def get_variant(es: Path, idx: Path) -> Variant:
    with _LOCK:
        v = _VARIANTS.get(str(es))
        if v is None:
            try:
                v = Variant(es, idx)
            except Exception:
                # a damaged cache entry (truncated copy, disk full while
                # writing) must not wedge the camera for ever: drop it so the
                # next start re-imports instead of failing the same way again
                for f in (es, idx):
                    try:
                        f.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise
            _VARIANTS[str(es)] = v
        v.refs += 1
        return v


def get_snapshots(folder: Path) -> SnapshotSet:
    with _LOCK:
        s = _SNAPS.get(str(folder))
        if s is None:
            s = SnapshotSet(folder)
            _SNAPS[str(folder)] = s
        return s


def release(v: Variant):
    """Give back one reference; the mmap closes when the last user is gone.

    Per-reference, not global: two Engine objects can exist in one process
    (the service replaces its engine on every restart, a test may run two),
    and closing *every* mapping because *one* engine stopped pulled the file
    out from under a stream that was still playing."""
    with _LOCK:
        v.refs -= 1
        if v.refs <= 0:
            _VARIANTS.pop(v.path, None)
            v.close()
        if not _VARIANTS:
            _SNAPS.clear()            # JPEG cache only; nothing open


def release_all():
    """Force-close everything (process shutdown)."""
    with _LOCK:
        for v in _VARIANTS.values():
            v.close()
        _VARIANTS.clear()
        _SNAPS.clear()
