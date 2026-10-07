#!/usr/bin/env bash
# Pinned ESPHome builder with the custom 1 MiB board definition.
set -euo pipefail
cd "$(dirname "$0")"
IMAGE="${ESPHOME_IMAGE:-ghcr.io/esphome/esphome:2026.9.1}"
CONFIG="${1:-sp9.yaml}"
[[ "$CONFIG" != */* && -f "$CONFIG" ]] || { echo "Use a YAML filename in esphome/" >&2; exit 1; }
[[ -f secrets.yaml ]] || { echo "Run python3 esphome/init_secrets.py first" >&2; exit 1; }
mkdir -p ../.pio_cache ../logs
BUILDER="beli-build-$$"
cleanup() { docker rm -f "$BUILDER" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker run -d --name "$BUILDER" --entrypoint bash \
    -v "$PWD:/project" -v "$PWD/../.pio_cache:/root/.platformio" \
    "$IMAGE" -c 'sleep infinity' >/dev/null
# First pass installs LibreTiny. A fresh cache cannot know our custom board yet.
if ! docker exec "$BUILDER" esphome compile "/project/$CONFIG" > ../logs/build-bootstrap.log 2>&1; then
    if ! docker exec "$BUILDER" test -d /root/.platformio/platforms/libretiny/boards; then
        echo "LibreTiny installation failed; see logs/build-bootstrap.log" >&2
        exit 1
    fi
fi
docker cp ../boards/tenda-sp3.json "$BUILDER:/root/.platformio/platforms/libretiny/boards/tenda-sp3.json"
docker exec "$BUILDER" esphome compile "/project/$CONFIG"
DEVICE_NAME="$(docker exec "$BUILDER" python3 -c 'import sys,yaml; print(next(v.value for k,v in next(v for k,v in yaml.compose(open(sys.argv[1])).value if k.value == "esphome").value if k.value == "name"))' "/project/$CONFIG")"
[[ "$DEVICE_NAME" =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "Invalid device name" >&2; exit 1; }
python3 prepare_cloud_image.py \
    --build-dir ".esphome/build/$DEVICE_NAME/.pioenvs/$DEVICE_NAME"
echo "Native OTA file: esphome/.esphome/build/$DEVICE_NAME/.pioenvs/$DEVICE_NAME/firmware.uf2"
