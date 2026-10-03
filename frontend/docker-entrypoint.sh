#!/bin/sh
set -eu
cat > /usr/share/nginx/html/runtime-config.js <<EOF
window.__APP_CONFIG__ = { mapboxAccessToken: "${MAPBOX_ACCESS_TOKEN:-}" };
EOF
