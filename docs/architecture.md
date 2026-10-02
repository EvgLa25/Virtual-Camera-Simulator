# Architecture

`config.py` loads and normalizes dataclasses from YAML and saves atomically.
`gui.py` edits configuration through PySide6. `proxy.py` presents the same
surface for a local engine or the service. Local engines hold a configuration
snapshot so ordinary UI edits cannot change advertised settings before apply;
fault commands explicitly update the live runtime.

`engine.py` owns an asyncio loop on a separate thread. It prepares media,
allocates addresses, starts cameras and shuts down active connections before
releasing media mappings. Each camera has a monotonic live timeline.

`media.py` invokes FFmpeg during import, indexes Annex-B H.264/H.265 access
units and shares read-only mmaps with reference counts. Cache keys include
the absolute source path, size, nanosecond timestamp and encoding profile/GOP.
Each import has unique temporary files; cancellation does not depend on
FFmpeg producing progress output.

`rtsp.py` handles sessions and transport, `rtp.py` packetizes video, `httpd.py`
serves SOAP and snapshots, `onvif_svc.py` implements operations and events,
`soap.py` builds XML, and `discovery.py` handles WS-Discovery.

`daemon.py` hosts an engine and a control channel. `control.py` binds the
channel to loopback and authenticates it with `control.token`. `service.py`
wraps the runner in pywin32; `svcctl.py` manages registration through sc.exe.

Network behavior is deliberately permissive in places to accommodate VMS
clients. Consult SECURITY.md before using this outside an isolated lab.
