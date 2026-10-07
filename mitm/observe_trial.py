#!/usr/bin/env python3
"""Observe only the designated SP9 during one explicitly requested OTA trial.

With --push, requests the configured push after checking the stock status API.
Records availability/latency, never Wi-Fi credentials or image contents.
"""
import argparse
import json
from pathlib import Path
import socket
import time
import urllib.request

TARGET = None


def admin(command):
    with socket.create_connection(("127.0.0.1", 9999), 5) as sock:
        sock.settimeout(15)
        sock.sendall(command.encode() + b"\n")
        return sock.recv(4096).decode().strip()


def stock_status():
    start = time.monotonic()
    try:
        with urllib.request.urlopen(f"http://{TARGET}:5000/getSta", timeout=1) as resp:
            obj = json.loads(resp.read())
            result = {"http": resp.status, "resp_code": obj.get("resp_code")}
    except Exception as exc:
        result = {"error": type(exc).__name__}
    result["latency_s"] = round(time.monotonic() - start, 3)
    return result


def port_open(port):
    try:
        with socket.create_connection((TARGET, port), 0.5):
            return True
    except OSError:
        return False


def main():
    global TARGET
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target-ip", required=True)
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--seconds", type=int, default=180)
    args = ap.parse_args()
    import ipaddress
    TARGET = str(ipaddress.IPv4Address(args.target_ip))
    root = Path(__file__).resolve().parents[1]
    logdir = root / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    path = logdir / f"trial-{time.time_ns()}.jsonl"
    baseline = stock_status()
    print("BASELINE", json.dumps(baseline), flush=True)
    status = admin("status")
    print("ADMIN", status, flush=True)
    if args.push and (f"target: {TARGET} " not in status or "serial_verified: True" not in status):
        raise SystemExit("Proxy target/verified serial does not match; no push sent")
    if args.push and (baseline.get("http"), baseline.get("resp_code")) != (200, 0):
        raise SystemExit("Stock API did not pass baseline; no push sent")
    start = time.monotonic()
    previous = None
    with path.open("w") as fh:
        fh.write(json.dumps({"event": "baseline", "unix": time.time(), "status": baseline}) + "\n")
        if args.push:
            result = admin("push")
            print("PUSH", result, flush=True)
            fh.write(json.dumps({"event": "push", "elapsed": time.monotonic() - start, "response": result}) + "\n")
            if not result.startswith("push started"):
                raise SystemExit("Push did not start")
        count = 0
        ports = {}
        while time.monotonic() - start < args.seconds:
            sample_start = time.monotonic()
            status = stock_status()
            if count % 5 == 0:
                ports = {str(port): port_open(port) for port in (80, 6053, 8892)}
            row = {"elapsed": round(time.monotonic() - start, 2), "stock": status, "ports": ports}
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            state = (status.get("http"), status.get("error"), tuple(ports.items()))
            if state != previous or count % 10 == 0:
                print(json.dumps(row), flush=True)
                previous = state
            if ports.get("6053") and ports.get("80"):
                print("ESPHome candidate detected; verify its HTTP identity before any further push", flush=True)
                break
            count += 1
            time.sleep(max(0, 1 - (time.monotonic() - sample_start)))
    print("RECORD", path, flush=True)


if __name__ == "__main__":
    main()
