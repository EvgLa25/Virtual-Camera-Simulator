# Changelog

## 1.3.0

- Source-release packaging, dependency notices, MIT license, public docs and CI.
- Camera search, first-run guidance, unsaved-change status and password masking.
- Background Save & Apply in local and service modes; failed operations retain edits.
- Fixed service save error reporting, existing-alias cleanup, pause/resume
  task duplication, cache key collisions, import cancellation and XML faults.
- Shutdown closes accepted HTTP/RTSP connections before waiting on listeners.
- Executable tests no longer terminate unrelated application processes.
- Default packaging no longer bundles FFmpeg or private runtime data.
