#!/usr/bin/env python3
"""Check device identity and increasing uptime through the ESPHome API without a key.

Run in the ESPHome container, which supplies aioesphomeapi and PyYAML.
"""
import argparse
import asyncio
import inspect
import json
from pathlib import Path
import re

from aioesphomeapi import APIClient
import yaml


def mac(value):
    value = value.replace(":", "").replace("-", "").lower()
    if not re.fullmatch(r"[0-9a-f]{12}", value):
        raise argparse.ArgumentTypeError("expected a 6-byte MAC address")
    return value


async def verify(args):
    # Compose the YAML tree without resolving unrelated Wi-Fi !secret tags.
    document = yaml.compose(args.config.read_text())
    esphome = next(value for key, value in document.value if key.value == "esphome")
    name = next(value.value for key, value in esphome.value if key.value == "name")
    client = APIClient(args.host, 6053, None, expected_name=name)
    await client.connect(login=True)
    try:
        info = await client.device_info()
        if mac(info.mac_address) != args.mac:
            raise RuntimeError("Wrong device MAC")
        print(json.dumps({field: getattr(info, field, None) for field in
                          ("name", "mac_address", "esphome_version", "model", "manufacturer")}), flush=True)
        entities, _ = await client.list_entities_services()
        uptime_keys = {e.key for e in entities if e.name == "Uptime" or e.name.endswith(" Uptime")}
        values = []

        def state_received(state):
            if state.key in uptime_keys:
                values.append(state.state)
                print("UPTIME", state.state, flush=True)

        result = client.subscribe_states(state_received)
        if inspect.isawaitable(result):
            await result
        await asyncio.sleep(70)
        if len(values) < 2 or not values[-1] > values[0]:
            raise RuntimeError("No increasing uptime")
        if not all(a <= b for a, b in zip(values, values[1:])):
            raise RuntimeError("Device rebooted")
        await client.device_info()
        print("PASS: matching MAC, API without a key, increasing uptime for 70 seconds", flush=True)
    finally:
        await client.disconnect()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", required=True, help="current ESPHome IP address or hostname")
    ap.add_argument("--mac", type=mac, required=True, help="MAC recorded before conversion")
    ap.add_argument("--config", type=Path, default=Path(__file__).with_name("sp9.yaml"))
    asyncio.run(verify(ap.parse_args()))


if __name__ == "__main__":
    main()
