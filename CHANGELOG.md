# Changelog

## v1.1 — Download destinations and separate media quality selection

- Added a per-job save directory (persisted in SQLite; compatible with existing databases).
- Added a configurable persistent default save directory in Preferences.
- Built a local directory browser with approved roots, folder creation, direct path navigation, write-permission verification, and symlink-safe validation.
- Added optional external folder roots using `FLUX_ALLOWED_DOWNLOAD_ROOTS`; on Docker these paths must also be mounted.
- Enabled same-disk atomic moves and cross-disk atomic finalization via temporary files.
- Separated **Video + audio** and **Audio only** workflows in the new-download dialog.
- Listed extractor-reported video resolutions and exact streams, and extractor-reported original audio bitrate/codec options, in separate selectable lists.
- Added target MP3 bitrates of 64, 96, 128, 192, 256, and 320 kbps plus VBR, with warnings about source limitations.
- Updated batch download and playlist import to honor the chosen output format and destination.
- Updated CSV history and completed-file handling to support custom destinations.
- Added comprehensive backend and mocked browser UI checks; 22 automated tests pass.

## v1.0 — Initial release

- Persistent queue, resumable and segmented HTTP transfers, yt-dlp media downloads, quality inspection, and responsive dashboard.
