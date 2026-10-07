#!/usr/bin/env python3
"""Create private Wi-Fi/OTA secrets without exposing them in shell history."""
import getpass
import json
import os
from pathlib import Path
import secrets


def main():
    path = Path(__file__).with_name("secrets.yaml")
    if path.exists():
        raise SystemExit("secrets.yaml already exists; edit it locally to preserve device keys")
    ssid = input("2.4 GHz Wi-Fi SSID: ").strip()
    password = getpass.getpass("Wi-Fi password: ")
    if not ssid or not password:
        raise SystemExit("SSID and password must be nonempty")
    values = {"wifi_ssid": ssid, "wifi_password": password,
              "ota_password": secrets.token_urlsafe(24),
              "fallback_password": secrets.token_urlsafe(18)}
    # JSON strings are valid YAML scalars, including quotes and backslashes.
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as output:
        for key, value in values.items():
            output.write(f"{key}: {json.dumps(value)}\n")
    print("Created private esphome/secrets.yaml; keep a backup outside Git.")


if __name__ == "__main__":
    main()
