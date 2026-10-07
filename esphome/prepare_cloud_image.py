#!/usr/bin/env python3
"""Preserve the complete declared RAM segment in an AmebaZ cloud image.

ltchiptool trims trailing RAM zeros but retains the ELF-derived header length.
Restore only zeros confirmed by its untrimmed RAM artifact; leave build output
unchanged. No Wi-Fi credentials or payload bytes are printed.
"""
import argparse
import hashlib
from pathlib import Path
import struct

HERE = Path(__file__).resolve().parent


def prepare(image, ram):
    if len(image) < 32 or image[:8] != b"81958711":
        raise ValueError("missing XIP magic")
    if struct.unpack_from("<I", image, 12)[0] != 0:
        raise ValueError("expected zero XIP load-address field")
    xip_len = struct.unpack_from("<I", image, 8)[0]
    offset = 32 + xip_len
    if len(image) < offset + 32 or image[offset:offset + 8] != b"81958711":
        raise ValueError("missing RAM magic at declared XIP end")
    length, address = struct.unpack_from("<II", image, offset + 8)
    if address != 0x10005000:
        raise ValueError("unexpected RAM address")
    present = image[offset + 32:]
    if len(present) > length or present != ram[:len(present)]:
        raise ValueError("RAM artifact and composite do not match")
    missing = length - len(present)
    if len(ram) < length or ram[len(present):length] != bytes(missing):
        raise ValueError("missing declared bytes are not confirmed zeros")
    result = image + bytes(missing)
    if len(result) > 0x75000:
        raise ValueError("image exceeds the OTA2 slot")
    return result, missing


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build-dir", type=Path, required=True,
                    help="PlatformIO environment directory containing the image artifacts")
    ap.add_argument("--output", type=Path, default=HERE / "sp9_esphome_ota2.bin")
    args = ap.parse_args()
    src = args.build_dir / "image_ota2.0x080000.bin"
    ram = args.build_dir / "image_ota2.ram_2.r.bin"
    image, added = prepare(src.read_bytes(), ram.read_bytes())
    dest = args.output
    dest.write_bytes(image)
    print(f"Prepared {dest.name}: {len(image)} bytes, restored {added} RAM zero bytes")
    print(f"SHA256 {hashlib.sha256(image).hexdigest()}")


if __name__ == "__main__":
    main()
