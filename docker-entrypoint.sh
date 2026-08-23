#!/bin/sh
set -eu

mkdir -p /config

if [ ! -f /config/config.yaml ]; then
    cp /app/config.yaml /config/config.yaml
fi

exec "$@"
