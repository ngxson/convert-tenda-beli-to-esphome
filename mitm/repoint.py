#!/usr/bin/env python3
"""Point the selected stock plug at the local proxy using its LAN API."""
import argparse
import ipaddress
import json
from pathlib import Path
import urllib.request
import yaml


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target-ip", required=True, type=ipaddress.IPv4Address)
    ap.add_argument("--proxy-ip", required=True, type=ipaddress.IPv4Address)
    ap.add_argument("--secrets", type=Path,
                    default=Path(__file__).resolve().parents[1] / "esphome/secrets.yaml")
    ap.add_argument("--location", required=True, help="IANA timezone, e.g. Etc/UTC")
    ap.add_argument("--timezone", type=int, required=True, help="UTC offset in minutes")
    args = ap.parse_args()
    values = yaml.safe_load(args.secrets.read_text())
    ssid, password = values["wifi_ssid"], values["wifi_password"]
    if not all(isinstance(v, str) and v and v != "REPLACE_ME" for v in (ssid, password)):
        ap.error("configure Wi-Fi secrets first")
    payload = {"account": "1", "key": password, "location": args.location,
               "server": str(args.proxy_ip), "ssid": ssid, "timezone": args.timezone}
    request = urllib.request.Request(f"http://{args.target_ip}:5000/guideDone",
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        result = json.load(response)
    if result.get("resp_code") != 0:
        raise SystemExit("Plug rejected provisioning; inspect its local status")
    print("Provisioning accepted. Wait for the target's runtime connection in the proxy.")


if __name__ == "__main__":
    main()
