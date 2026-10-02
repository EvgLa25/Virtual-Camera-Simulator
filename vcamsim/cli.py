"""Headless CLI. Same engine as the GUI, no Qt required."""
from __future__ import annotations

import argparse
import sys
import time

from .config import AppCfg, CameraCfg, generate_cameras, resolve_path
from .engine import Engine


def cmd_run(args):
    cfg = AppCfg.load(args.config)
    if not cfg.cameras:
        print(f"no cameras in {args.config} -- run 'gen' first")
        sys.exit(1)
    eng = Engine(cfg, log_cb=lambda l: print(l, flush=True))
    try:
        eng.start()
    except RuntimeError as e:
        print(f"cannot start: {e}")
        sys.exit(1)
    try:
        while eng.running or (eng.thread and eng.thread.is_alive()):
            time.sleep(1)
        print("engine stopped")
    except KeyboardInterrupt:
        print("stopping...")
    finally:
        eng.stop()


def cmd_gen(args):
    cfg = AppCfg.load(args.config)
    tpl = CameraCfg(name=args.name, video=args.video,
                    username=args.user, password=args.password)
    if args.seconds is not None:
        tpl.import_seconds = args.seconds
    tpl.ensure_profiles()
    if args.codec:
        for p in tpl.profiles:
            p.codec = args.codec
    cams = generate_cameras(tpl, args.count)
    if args.replace:
        cfg.cameras = cams
    else:
        cfg.cameras.extend(cams)
    if args.mode:
        cfg.addressing.mode = args.mode
    cfg.save(args.config)
    print(f"wrote {len(cams)} cameras to {args.config}")


def cmd_list(args):
    cfg = AppCfg.load(args.config)
    for c in cfg.cameras:
        print(f"{c.name:<20} {c.video}  profiles={len(c.profiles) or 2} "
              f"enabled={c.enabled}")


def cmd_firewall(args):
    from . import firewall
    from .addressing import is_admin
    if args.remove:
        ok, msg = firewall.remove_rule()
        print(("removed: " if ok else "failed: ") + msg)
        return
    if not is_admin():
        print("Administrator rights required. Re-run this command from an "
              "elevated prompt.")
        sys.exit(1)
    ok, msg = firewall.add_rule()
    print(("firewall rule added -- " if ok else "failed -- ") + msg)
    sys.exit(0 if ok else 1)


def cmd_daemon(args):
    """Exactly what the Windows service runs, but in the foreground."""
    from .daemon import run_foreground
    run_foreground(args.config, autostart=not args.no_start)


def cmd_service(args):
    from . import svcctl
    if args.action == "status":
        print(f"service: {svcctl.state()}")
        return
    fn = {"install": svcctl.install, "remove": svcctl.remove,
          "start": svcctl.start, "stop": svcctl.stop}[args.action]
    ok, msg = fn()
    print(("ok -- " if ok else "failed -- ") + msg)
    sys.exit(0 if ok else 1)


def cmd_gui(args):
    from .gui import main as gui_main
    gui_main(args.config, getattr(args, "autostart", False))


def main(argv=None):
    ap = argparse.ArgumentParser("vcamsim", description="Virtual ONVIF camera simulator")
    ap.add_argument("-c", "--config", default=None)
    ap.add_argument("--autostart", action="store_true",
                    help="GUI only: start every camera immediately")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("run", help="run headless").set_defaults(fn=cmd_run)
    sub.add_parser("list", help="list configured cameras").set_defaults(fn=cmd_list)
    sub.add_parser("gui", help="launch the desktop GUI").set_defaults(fn=cmd_gui)

    d = sub.add_parser("daemon", help="run headless with the control channel "
                                      "(what the Windows service runs)")
    d.add_argument("--no-start", action="store_true",
                   help="serve the control channel but leave cameras stopped")
    d.set_defaults(fn=cmd_daemon)

    sv = sub.add_parser("service", help="install/remove/start/stop the "
                                        "Windows service (needs admin)")
    sv.add_argument("action",
                    choices=["install", "remove", "start", "stop", "status"])
    sv.set_defaults(fn=cmd_service)

    fw = sub.add_parser("firewall", help="allow inbound ONVIF/RTSP through "
                                         "Windows Firewall (needs admin)")
    fw.add_argument("--remove", action="store_true")
    fw.set_defaults(fn=cmd_firewall)

    g = sub.add_parser("gen", help="generate N cameras from a video")
    g.add_argument("--video", required=True)
    g.add_argument("--count", type=int, default=1)
    g.add_argument("--name", default="VCam {n}")
    g.add_argument("--user", default="admin")
    g.add_argument("--password", default="admin")
    g.add_argument("--codec", choices=["H264", "H265"])
    g.add_argument("--seconds", type=int, default=None,
                   help="import only the first N seconds (0 = whole file)")
    g.add_argument("--mode", choices=["port", "alias"])
    g.add_argument("--replace", action="store_true")
    g.set_defaults(fn=cmd_gen)

    a = ap.parse_args(argv)
    if not a.config:
        a.config = str(resolve_path("config.yaml"))
    if not getattr(a, "fn", None):
        a.fn = cmd_gui
    a.fn(a)


if __name__ == "__main__":
    main()
