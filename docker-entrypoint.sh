#!/bin/sh
set -eu
mkdir -p /app/data /app/downloads /app/downloads/.parts
# Docker bind mounts can be owned by a different user than files in the image.
chown flux:flux /app/data /app/downloads /app/downloads/.parts 2>/dev/null || true
exec gosu flux "$@"
