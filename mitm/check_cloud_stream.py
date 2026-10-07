#!/usr/bin/env python3
"""Run the SP3 dump's actual Thumb OTA chunk/FSM code without touching a plug.

Requires unicorn. Hardware, logging, and connection teardown are stubbed;
the FSM, TLV parser, byte counters, and write address calculations are real.
This tests the available SP3 dump, not the newer SP9 firmware on the target.
"""

from pathlib import Path
import argparse
import hashlib
import struct

from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_HOOK_CODE
from unicorn.arm_const import (
    UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
    UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC,
)

from tenda_mitm import build_dl_header, iter_upgrade_chunks, tlv

ROOT = Path(__file__).resolve().parents[1]
CTX = 0x10005EA8
FSM = 0x1000DA34
REGS = (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)


class CloudEmulator:
    def __init__(self, image_size, dump_dir=ROOT / "dump"):
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB)
        self.uc.mem_map(0x08000000, 0x100000)
        self.uc.mem_map(0x10000000, 0x40000)
        self.uc.mem_write(0x0800B020, (dump_dir / "xip_payload.bin").read_bytes())
        self.uc.mem_write(0x10005000, (dump_dir / "ram_payload.bin").read_bytes())
        # State immediately after sending the FC request; OTA1 is running.
        self.uc.mem_write(FSM, bytes(0x208))
        self.uc.mem_write(CTX, struct.pack("<9I", 1, 0x80000, image_size, 1,
                                         56 + (image_size - 56) // 2, 0, 0, 0, 1))
        self.set32(FSM, 7)
        self.writes = []
        self.flash = bytearray(b"\xff" * 0x100000)
        self.completed = False
        self.verifier_calls = 0
        self.handlers = {}
        self.uc.hook_add(UC_HOOK_CODE, self.hook)
        # Execute the real registration routines, including their literal
        # pools. Earlier checks bypassed internal registration and missed that
        # internal 1D has no callback; 1E is the incoming chunk handler.
        for address in (0x0803AC70, 0x0803ACF8):
            self.uc.reg_write(UC_ARM_REG_SP, 0x1003F000)
            self.uc.reg_write(UC_ARM_REG_LR, 0x10022001)
            self.uc.emu_start(address | 1, 0x10022000, count=100000)
        assert self.handlers[(3, 0x1D)] == 0
        assert self.handlers[(3, 0x1E)] == 0x080133E5

    def get32(self, addr):
        return struct.unpack("<I", self.uc.mem_read(addr, 4))[0]

    def set32(self, addr, value):
        self.uc.mem_write(addr, struct.pack("<I", value))

    def ret(self, value=0):
        self.uc.reg_write(UC_ARM_REG_R0, value)
        self.uc.reg_write(UC_ARM_REG_PC, self.uc.reg_read(UC_ARM_REG_LR))

    def hook(self, uc, addr, size, _):
        if addr == 0x080126F0:  # register(module, command, sender, receiver, aux)
            module, command, _, receiver = (uc.reg_read(r) for r in REGS)
            self.handlers[(module, command)] = receiver
            self.ret()
        elif addr == 0x0800C898:  # memcpy: erased in this dump
            dest, src, count = (uc.reg_read(r) for r in REGS[:3])
            if count:
                uc.mem_write(dest, bytes(uc.mem_read(src, count)))
            self.ret(dest)
        elif addr == 0x0800C8A8:  # memset: erased in this dump
            dest, value, count = (uc.reg_read(r) for r in REGS[:3])
            if count:
                uc.mem_write(dest, bytes([value & 255]) * count)
            self.ret(dest)
        elif addr == 0x080198A2:  # flash_stream_write
            _, dest, count, src = (uc.reg_read(r) for r in REGS)
            assert 0x80000 <= dest <= dest + count <= 0xF5000, (dest, count)
            data = bytes(uc.mem_read(src, count))
            self.flash[dest:dest + count] = data
            self.writes.append((dest, count))
            self.ret(1)
        elif addr in (0x08019A68, 0x08019A92, 0x0800FE5C):
            self.ret()  # lock/unlock/log
        elif addr == 0x08012DA2:
            self.ret()  # connection teardown; stop before hardware operations below
        elif addr == 0x0801331C:
            self.completed = True
            uc.emu_stop()  # would change boot slot next
        elif addr == 0x0801A100:
            self.verifier_calls += 1
            raise AssertionError("Unexpected raw OTA checksum verifier in cloud path")

    def chunk(self, state, data=None):
        payload = tlv(0x1A, bytes([state]))
        if data is not None:
            payload += tlv(0x1C, data)
        self.uc.mem_write(0x10020000, payload)
        self.uc.mem_write(0x10021000, b"\x08" + bytes(19))
        # Enter through the actually registered incoming handler, including
        # its direction/status checks, rather than calling the FSM directly.
        for reg, val in zip(REGS, (0x10021000, 0, 0x10020000, len(payload))):
            self.uc.reg_write(reg, val)
        self.uc.reg_write(UC_ARM_REG_SP, 0x1003F000)
        self.set32(0x1003F000, 0)
        self.uc.reg_write(UC_ARM_REG_LR, 0x10022001)
        self.uc.emu_start(self.handlers[(3, 0x1E)], 0x10022000, count=100000)
        if not self.completed:
            assert self.uc.reg_read(UC_ARM_REG_PC) == 0x10022000, "instruction limit reached"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--image", type=Path, default=ROOT / "esphome/sp9_esphome_ota2.bin")
    ap.add_argument("--dump-dir", type=Path, default=ROOT / "dump")
    ap.add_argument("--chunk-size", type=int, default=1024)
    ap.add_argument("--final-data", action="store_true")
    args = ap.parse_args()
    img = args.image.read_bytes()
    hdr = build_dl_header(img)
    emu = CloudEmulator(56 + 2 * len(img), args.dump_dir)
    for state, data in iter_upgrade_chunks(img, hdr, args.chunk_size, args.final_data):
        emu.chunk(state, data)
    received, written = emu.get32(CTX + 0x14), emu.get32(CTX + 0x18)
    actual = bytes(emu.flash[0x80000:0x80000 + len(img)])
    assert received == 56 + 2 * len(img), received
    assert written == len(img), written
    assert actual == img, "written image differs"
    assert emu.completed, "completion did not reach OTA slot change"
    print(f"PASS: received={received}, written={written}, flash writes={len(emu.writes)}")
    print("PASS: actual module-3 registration: cmd 1D has no receiver; cmd 1E -> 080133e4")
    print(f"PASS: slot 0x80000 SHA256={hashlib.sha256(actual).hexdigest()}")
    print(f"PASS: state-3 reaches slot change; chunk_size={args.chunk_size}, final_data={args.final_data}, checksum verifier calls={emu.verifier_calls}")


if __name__ == "__main__":
    main()
