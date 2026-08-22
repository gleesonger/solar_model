#!/bin/sh
set -eu

mkdir -p /config

if [ ! -f /config/config.yaml ]; then
    cp /app/config.yaml /config/config.yaml
fi

if [ ! -f /config/sigen_register_map.json ]; then
    cp /app/sigen_register_map.json /config/sigen_register_map.json
fi

exec "$@"
