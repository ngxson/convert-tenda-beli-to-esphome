#!/usr/bin/env python3
"""Check Git publication candidates without printing potentially sensitive values."""
import argparse
import ipaddress
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in
                         ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
PRIVATE_DIRS = {"tmp", "logs", "captures", "dump", "private", ".pio", ".pio_cache",
                ".esphome", "__pycache__", ".venv"}
ARTIFACTS = {".bin", ".uf2", ".elf", ".map", ".pcap", ".pcapng", ".log", ".jsonl",
             ".apk", ".zip", ".env", ".pem", ".key", ".pyc", ".o", ".a"}
PATTERNS = {
    "personal home path": re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+/"),
    "stock device serial": re.compile(r"\bEB\d{15}\b"),
    "private key": re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    "possible embedded token": re.compile(r"(?<![\w])[A-Za-z0-9+/]{43}=(?![\w])"),
}


def git_files(root):
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                            capture_output=True, text=True)
    if result.returncode == 0 and Path(result.stdout.strip()).resolve() == root.resolve():
        command = ["git", "-C", str(root)]
        data = subprocess.check_output(command + ["ls-files", "--cached", "--others", "--exclude-standard", "-z"])
    else:
        # Honor project ignore rules without creating or changing its repository.
        with tempfile.TemporaryDirectory(prefix="beli-index-") as temp:
            subprocess.run(["git", "init", "-q", temp], check=True, capture_output=True)
            command = ["git", "--git-dir", str(Path(temp) / ".git"), "--work-tree", str(root)]
            data = subprocess.check_output(command + ["ls-files", "--others", "--exclude-standard", "-z"], cwd=root)
    return sorted({Path(p.decode()) for p in data.split(b"\0") if p})


def findings(relative, data):
    if any(part in PRIVATE_DIRS for part in relative.parts) or relative.suffix in ARTIFACTS:
        yield 0, "private artifact in publication set"
    if relative.name.startswith(".env") or (relative.name.startswith("secrets") and relative.name != "secrets.example.yaml"):
        yield 0, "credential file in publication set"
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        yield 0, "binary file needs manual review"
        return
    for line_number, line in enumerate(content.splitlines(), 1):
        for label, pattern in PATTERNS.items():
            if pattern.search(line):
                yield line_number, label
        for value in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", line):
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if any(address in network and not (
                    address == network.network_address and f"{address}/{network.prefixlen}" in line)
                   for network in PRIVATE_NETWORKS):
                yield line_number, "private LAN address"
        for value in re.findall(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", line):
            if value.rsplit("@", 1)[1] not in {"example.com", "example.org", "example.net"}:
                yield line_number, "email address"
        for value in re.findall(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b", line):
            if not value.lower().startswith("02:00:00:00:"):
                yield line_number, "device MAC address"
        if relative.suffix in {".yaml", ".yml"}:
            match = re.match(r"\s*(?:\w*password|\w*ssid|\w*encryption_key|key):\s*(.+)", line)
            if match and not match[1].startswith(("!secret ", "REPLACE_ME", "Beli Recovery")):
                yield line_number, "literal YAML credential or network name"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=ROOT)
    args = ap.parse_args()
    root = args.root.resolve()
    candidates = git_files(root)
    count = 0
    for relative in candidates:
        path = root / relative
        if not path.exists():  # tracked deletion
            continue
        if path.is_symlink():
            print(f"{relative}: symlink needs manual review")
            count += 1
            continue
        for line, label in findings(relative, path.read_bytes()):
            print(f"{relative}:{line}: {label}")
            count += 1
    if count:
        raise SystemExit(f"FAIL: {count} finding(s); values withheld. Review before publishing.")
    print(f"PASS: {len(candidates)} publication candidates; no flagged identifiers, credentials, or artifacts.")


if __name__ == "__main__":
    main()
