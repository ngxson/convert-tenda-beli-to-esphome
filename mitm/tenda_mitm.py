#!/usr/bin/env python3
"""Target-filtered Tenda Beli proxy for the verified FE firmware transfer.

Run natively on the LAN. The server only arms a transfer after an explicit
admin push; startup merely relays the selected plug's cloud traffic.
Capture logs are private and excluded from version control. See README.md.
"""

import argparse
import asyncio
import json
import ipaddress
from pathlib import Path
import os
import socket
import struct
import time

LOG = None
T0 = time.time()
PLUG_WRITERS = {}      # label -> writer towards the plug
UPGRADE = {"image": None, "seq": 0x5AA0,
           "stream_options": {"chunk_size": 1024, "delay_ms": 5, "final_data": False,
                              "envelope": "fe"}}
AUTO_ADDR = {"on": True, "port": 11813}
TARGET_IP = None
TARGET_SN = None
VERIFIED_RUNTIME = None
CHALLENGE = None
CLOUD_UPGRADE = ("47.254.24.171", 1823)


def maybe_auto_reply(f):
    """Called for every frame the plug sends toward the cloud. The plug's
    WHERE_UPGRADE_Q is a cmd-0xFA relay frame with flags[0:2]=(3,0x1A).
    Reply immediately with cmd 0xFB carrying TLVs 0x14/0x15 (upgrade server
    address) pointing at us - same shape as the real cloud's reply."""
    if not AUTO_ADDR["on"] or len(f) < 16:
        return
    if f[5] == 0xFA and f[8:10] == b"\x03\x1a":
        payload = tlv(0x14, socket.inet_aton(MY_IP)) + tlv(0x15, struct.pack(">H", AUTO_ADDR["port"]))
        reply = frame(0xFB, 0, payload, direction=3)
        log("plug asked WHERE_UPGRADE_Q (cmd FA) -> auto-replying with our address")
        log(frame_str(reply, ">>INJ  "))
        send_to_plug("runtime", reply)

CMD_NAMES = {
    0x17: "REGISTER",
    0x61: "RUNTIME_ON",
    0x65: "KEEPALIVE",
    0x66: "STATUS",
    0xd2: "REDIRECT",
    0xd5: "POWER_REQ",
    0x5e: "CMD_ACK",
    0xe5: "USERLIST",
    0x89: "ENERGY",
    0x1a: "WHERE_UPGRADE",
    0x1c: "UPGRADE_REQ",
    0x67: "SN_REPORT",
}


def log(msg):
    line = f"[{time.time() - T0:9.3f}] {msg}"
    print(line, flush=True)
    if LOG:
        LOG.write(line + "\n")
        LOG.flush()


def hexs(b):
    return b.hex()


def decode_payload(payload):
    if not payload:
        return ""
    if payload[:1] in (b"{", b"["):
        try:
            return "JSON " + json.dumps(json.loads(payload.rstrip(b"\x00")))
        except Exception:
            pass
    if payload.startswith(b"81958711"):
        return "OTA-IMAGE-MAGIC"
    out = []
    i = 0
    while i + 4 <= len(payload):
        tag, ln = struct.unpack(">HH", payload[i:i + 4])
        val = payload[i + 4:i + 4 + ln]
        if len(val) < ln:
            out.append(f"TLV? {hexs(payload[i:])}")
            break
        if tag == 0x10 or tag == 0x14:  # ip
            if ln == 4:
                val = socket.inet_ntoa(val)
        elif tag == 0x11 or tag == 0x15:  # port
            if ln == 2:
                val = str(struct.unpack(">H", val)[0])
        out.append(f"TLV(0x{tag:02X},{ln})={hexs(val) if isinstance(val, bytes) else val}")
        i += 4 + ln
    return " ".join(out) if out else "RAW " + hexs(payload[:48])


def frame_str(frame, arrow):
    if len(frame) < 16:
        return f"{arrow} SHORT {hexs(frame)}"
    dir_, state, cmd = frame[2], frame[3], frame[5]
    module = frame[4]
    ln = struct.unpack(">H", frame[6:8])[0]
    name = CMD_NAMES.get(cmd, f"cmd_{cmd:02X}")
    return (f"{arrow} dir={dir_:02X} st={state:02X} mod={module:02X} {name} "
            f"len={ln} flags={hexs(frame[8:16])} :: {decode_payload(frame[16:])}")


async def read_frames(reader, out_queue, label, arrow, runtime_writer=None):
    """Split a TCP stream into frames, decode+log, forward to queue."""
    global VERIFIED_RUNTIME
    buf = b""
    while True:
        try:
            data = await reader.read(4096)
        except Exception:
            data = b""
        if not data:
            if buf:
                log(f"{label}: stream ended with {len(buf)} unparsed bytes: {hexs(buf[:64])}")
            await out_queue.put(None)
            return
        buf += data
        while True:
            if len(buf) < 2:
                break
            idx = buf.find(b"\x24\x00")
            if idx < 0:
                buf = buf[-1:]
                break
            if idx > 0:
                log(f"{label}: dropping {idx} garbage bytes: {hexs(buf[:idx][:48])}")
                buf = buf[idx:]
            if len(buf) < 16:
                break
            ln = struct.unpack(">H", buf[6:8])[0]
            total = 16 + ln
            if ln > 64 * 1024:
                log(f"{label}: absurd len {ln}, dropping 2 bytes")
                buf = buf[2:]
                continue
            if len(buf) < total:
                break
            frame = buf[:total]
            buf = buf[total:]
            if runtime_writer is not None and frame[4:6] == b"\x00\x17":
                if tlv_value(frame[16:], 1) != TARGET_SN:
                    log("runtime: rejecting registration with unexpected serial")
                    runtime_writer.close()
                    await out_queue.put(None)
                    return
                VERIFIED_RUNTIME = runtime_writer
                log("runtime: target serial verified")
            log(frame_str(frame, arrow))
            if arrow.startswith("<PLUG"):
                maybe_auto_reply(frame)
            await out_queue.put(frame)


async def pump(queue, writer):
    while True:
        frame = await queue.get()
        if frame is None:
            try:
                writer.close()
            except Exception:
                pass
            return
        try:
            writer.write(frame)
            await writer.drain()
        except Exception:
            return


class Relay:
    """Relay plug<->cloud on one port, with optional frame rewriting."""

    def __init__(self, listen_port, upstream_addr, label, rewrite=None):
        self.listen_port = listen_port
        self.upstream_addr = upstream_addr          # callable(plug_addr) or tuple
        self.label = label
        self.rewrite = rewrite                      # callable(frame) -> frame

    async def start(self):
        server = await asyncio.start_server(self.handle, "0.0.0.0", self.listen_port)
        log(f"listening on :{self.listen_port} -> {self.upstream_addr} ({self.label})")
        return server

    async def handle(self, plug_reader, plug_writer):
        peer = plug_writer.get_extra_info("peername")
        if not peer or peer[0] != TARGET_IP:
            log(f"{self.label}: rejecting non-target peer {peer}")
            plug_writer.close()
            return
        PLUG_WRITERS[self.label] = plug_writer
        upstream = self.upstream_addr(peer) if callable(self.upstream_addr) else self.upstream_addr
        log(f"{self.label}: plug {peer} connected, relaying to {upstream}")
        try:
            up_reader, up_writer = await asyncio.open_connection(*upstream)
        except Exception as e:
            log(f"{self.label}: upstream connect failed: {e}")
            plug_writer.close()
            return

        q2c = asyncio.Queue()   # frames towards cloud
        q2p = asyncio.Queue()   # frames towards plug

        async def plug_to_cloud():
            await read_frames(plug_reader, q2c, f"{self.label} plug", "<PLUG ",
                              runtime_writer=plug_writer if self.label == "runtime" else None)

        async def cloud_to_plug():
            async def wrap(frame):
                if self.rewrite and frame is not None:
                    frame = self.rewrite(frame, peer)
                await q2p.put(frame)
            # read_frames but with rewriting: reuse with a shim queue
            buf = b""
            while True:
                try:
                    data = await up_reader.read(4096)
                except Exception:
                    data = b""
                if not data:
                    await q2p.put(None)
                    return
                buf += data
                while True:
                    if len(buf) < 2:
                        break
                    idx = buf.find(b"\x24\x00")
                    if idx < 0:
                        buf = buf[-1:]
                        break
                    if idx > 0:
                        buf = buf[idx:]
                    if len(buf) < 16:
                        break
                    ln = struct.unpack(">H", buf[6:8])[0]
                    if ln > 64 * 1024:
                        buf = buf[2:]
                        continue
                    total = 16 + ln
                    if len(buf) < total:
                        break
                    frame = buf[:total]
                    buf = buf[total:]
                    log(frame_str(frame, ">CLOUD "))
                    if self.rewrite:
                        new = self.rewrite(frame, peer)
                        if new is None:
                            continue
                        if new is not frame and new != frame:
                            log(f"  rewritten -> {frame_str(new, '>>CLOUD')}")
                        frame = new
                    await q2p.put(frame)

        await asyncio.gather(
            plug_to_cloud(), cloud_to_plug(),
            pump(q2c, up_writer), pump(q2p, plug_writer),
        )


# ---- state shared between relays -------------------------------------------
REAL_RUNTIME = {}          # peer-ip -> (ip, port) discovered in redirect


def rt_key(peer):
    return peer[0] if peer else "*"
MY_IP = None


def rewrite_redirect(frame, peer):
    """Rewrite cmd 0xd2 redirect TLVs so the plug connects to us:11812.
    Also rewrites cmd 0xFB (cloud's upgrade-address reply) to us:11813."""
    if len(frame) < 16:
        return frame
    if frame[5] == 0xFB:
        # our auto-reply to the plug's cmd-0xFA query already delivered OUR
        # upgrade-server address; a second delivery re-fires the FSM event
        # and aborts the upgrade. Drop the cloud's copy.
        log("  dropping cloud cmd_FB (auto-reply already sent)")
        return None
    if frame[5] != 0xd2:
        return frame
    payload = bytearray(frame[16:])
    out = b""
    i = 0
    changed = False
    while i + 4 <= len(payload):
        tag, ln = struct.unpack(">HH", payload[i:i + 4])
        val = bytes(payload[i + 4:i + 4 + ln])
        if len(val) < ln:
            break
        if tag == 0x10 and ln == 4:
            # stash the REAL runtime server before rewriting to us
            prev_port = REAL_RUNTIME.get(rt_key(peer), ("", 11822))[1]
            REAL_RUNTIME[rt_key(peer)] = (socket.inet_ntoa(val), prev_port)
            log(f"  cloud redirected {peer} to real runtime {socket.inet_ntoa(val)}")
            val = socket.inet_aton(MY_IP)
            changed = True
        elif tag == 0x11 and ln == 2:
            real_port = struct.unpack(">H", val)[0]
            prev_ip = REAL_RUNTIME.get(rt_key(peer), ("47.251.6.232",))[0]
            REAL_RUNTIME[rt_key(peer)] = (prev_ip, real_port)
            log(f"  real runtime port {real_port}")
            val = struct.pack(">H", 11812)
            changed = True
        out += struct.pack(">HH", tag, ln) + val
        i += 4 + ln
    # capture full original for the log
    if not changed:
        return frame
    return frame[:16] + out


def rewrite_upgrade_addr(frame):
    """Rewrite cmd 0xFB TLVs 0x14/0x15 (upgrade server ip/port) to us."""
    payload = frame[16:]
    out = b""
    i = 0
    changed = False
    while i + 4 <= len(payload):
        tag, ln = struct.unpack(">HH", payload[i:i + 4])
        val = payload[i + 4:i + 4 + ln]
        if len(val) < ln:
            break
        if tag == 0x14 and ln == 4:
            log(f"  cloud upgrade addr {socket.inet_ntoa(val)} -> {MY_IP}")
            val = socket.inet_aton(MY_IP)
            changed = True
        elif tag == 0x15 and ln == 2:
            log(f"  cloud upgrade port {struct.unpack('>H', val)[0]} -> {AUTO_ADDR['port']}")
            val = struct.pack(">H", AUTO_ADDR["port"])
            changed = True
        out += struct.pack(">HH", tag, ln) + val
        i += 4 + ln
    if not changed:
        return frame
    return frame[:16] + out


def runtime_upstream(peer):
    ip, port = REAL_RUNTIME.get(rt_key(peer), ("47.251.6.232", 11822))
    return (ip, port)


def frame(cmd, module, payload=b"", direction=3, aux=0, flags=b"\x00" * 8):
    """Build a runtime-protocol frame (server->plug)."""
    hdr = bytes([0x24, 0x00, direction, aux, module, cmd]) + struct.pack(">H", len(payload)) + flags
    return hdr + payload


def tlv(tag, value):
    return struct.pack(">HH", tag, len(value)) + value


def ipv4(value):
    try:
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError as exc:
        raise argparse.ArgumentTypeError("expected an IPv4 address") from exc


def tlv_value(payload, wanted):
    offset = 0
    while offset + 4 <= len(payload):
        tag, length = struct.unpack_from(">HH", payload, offset)
        offset += 4
        if offset + length > len(payload):
            return None
        value = payload[offset:offset + length]
        if tag == wanted:
            return value
        offset += length
    return None


async def fetch_cloud_challenge(register):
    """Fetch this plug's challenge; never ship a captured device token in code."""
    if register[4:6] != b"\x00\x17" or tlv_value(register[16:], 1) != TARGET_SN:
        raise ValueError("challenge request does not match the target serial")
    reader, writer = await asyncio.wait_for(asyncio.open_connection(*CLOUD_UPGRADE), 10)
    try:
        writer.write(register)
        await writer.drain()
        header = await asyncio.wait_for(reader.readexactly(16), 10)
        length = struct.unpack_from(">H", header, 6)[0]
        if header[:2] != b"\x24\x00" or header[2] != 8 or header[4:6] != b"\x00\x18" or length > 4096:
            raise ValueError("cloud did not return an upgrade challenge")
        payload = await asyncio.wait_for(reader.readexactly(length), 10)
        challenge = tlv_value(payload, 4)
        if challenge is None or len(challenge) != 32:
            raise ValueError("invalid cloud challenge payload")
        return challenge
    finally:
        writer.close()
        await writer.wait_closed()


def replace_tlv_value(f, tag, value):
    """Change a cloud-bound registration TLV, preserving the frame envelope."""
    payload = f[16:]
    out = bytearray()
    offset = 0
    found = False
    while offset < len(payload):
        if offset + 4 > len(payload):
            raise ValueError("truncated TLV header")
        current, length = struct.unpack_from(">HH", payload, offset)
        end = offset + 4 + length
        if end > len(payload):
            raise ValueError("truncated TLV value")
        out.extend(tlv(current, value if current == tag else payload[offset + 4:end]))
        found |= current == tag
        offset = end
    if not found:
        raise ValueError(f"missing registration TLV {tag}")
    return f[:6] + struct.pack(">H", len(out)) + f[8:16] + out


def send_to_plug(label, data):
    w = PLUG_WRITERS.get(label)
    if not w or w.is_closing():
        log(f"inject: no plug writer for '{label}'")
        return False
    peer = w.get_extra_info("peername")
    if not peer or peer[0] != TARGET_IP:
        log(f"inject: refusing non-target peer {peer}")
        return False
    if label == "runtime" and w is not VERIFIED_RUNTIME:
        log("inject: runtime serial has not been verified")
        return False
    try:
        w.write(data)
        return True
    except Exception as e:
        log(f"inject failed: {e}")
        return False


def build_where_upgrade(ip, port):
    # cmd 0x1B (module 3) = "upgrade server address" - TLVs 0x14=ip, 0x15=port
    payload = tlv(0x14, socket.inet_aton(ip)) + tlv(0x15, struct.pack(">H", port))
    return frame(0x1B, 3, payload)


def build_d5(module, cmd, payload=b""):
    """Build a d5 'relay' frame: the runtime server tells the plug to run an
    internal handler identified by flags[0:2] = (module, cmd). The real cloud
    uses this for checkVer (flags 0203) - captured live."""
    UPGRADE["d5seq"] = UPGRADE.get("d5seq", 0x0595) + 1
    flags = bytes([module, cmd, 0, 0]) + struct.pack(">I", UPGRADE["d5seq"])
    return frame(0xD5, 0, payload, direction=3, flags=flags)


# ---- OTA download header (56 bytes) -----------------------------------------
# Layout (from get_ota_tartget_header/recv_ota_file_hdr):
#   +0x00 u32 version/magic (unchecked in our path)
#   +0x04 u32 entry count          (= 2 -> header size 8 + 2*24 = 56)
#   +0x08 entry0 (24 bytes):
#     +0x08 char[4] image id  (strncmp'd against the plug's image_id)
#     +0x0C u32 24            (entry size / "mode", must != 1)
#     +0x10 u32 CHECKSUM       (verify_ota_checksum expected value)
#     +0x14 u32 SIZE           (len(composite); download size = size - 8)
#     +0x18 u32                (raw-path stream offset; legacy experiment uses 0)
#     +0x1C u32 0
#   +0x20 entry1 (24 bytes): second image-id candidate (no RDP entry ->
#            ctx[0x38] stays 0 -> verify skips the RDP check)
#
# Checksum equation for the SEPARATE raw TCP updater (verify_ota_checksum):
#   byte_sum(flash[img+8 .. img+8+(SIZE-8)]) + byte_sum(8B field) == CHECKSUM
#   = byte_sum(composite[8:]) + 424

def byte_sum(b):
    return sum(b) & 0xFFFFFFFF


def build_dl_header(composite, ids=("OTA2", "OTA1"), checksum=None, entry_size=None, rdp_len=None):
    """Build the 56-byte download header. checksum/entry_size override the
    auto-computed fields. With rdp_len, entry1 becomes an "RDP" entry.
    These fields describe the raw updater; their use by the live SP9 cloud
    path is unproven. RDP timing does not establish cloud header parsing.
    """
    size = len(composite)
    s = byte_sum(composite[8:])
    if checksum is None:
        checksum = (s + 424) & 0xFFFFFFFF     # default: + "81958711" magic
    if entry_size is None:
        entry_size = size
    addr = 0
    hdr = struct.pack("<II", 1, 2)
    e0 = ids[0].encode()[:4].ljust(4, b"\x00")
    hdr += e0 + struct.pack("<IIIII", 24, checksum, entry_size, addr, 0)
    if rdp_len is not None:
        e1 = b"RDP\x00"
        hdr += e1 + struct.pack("<IIIII", 24, 0, rdp_len, 0, 0)  # cksum=0, len=rdp_len
    else:
        e1 = ids[1].encode()[:4].ljust(4, b"\x00")
        hdr += e1 + struct.pack("<IIIII", 24, checksum, entry_size, addr, 0)
    assert len(hdr) == 56, len(hdr)
    UPGRADE["header"] = hdr
    UPGRADE["checksum"] = checksum
    log(f"header: ids={ids} image_size={size} bytesum[8:]={s} "
        f"entry_size={entry_size} rdp_len={rdp_len} checksum={hex(checksum)}")
    return hdr


def build_upgrade_start():
    # cmd 0x1C (module 3), empty payload = posts one OPT_TYPE_UPGRADE event
    return frame(0x1C, 3, b"")


def iter_upgrade_chunks(image, header, chunk_size=1024, final_data=False):
    """One source of stream bytes for the live server and Thumb emulator."""
    if not 1 <= chunk_size <= 2048:
        raise ValueError("chunk size must be 1..2048")
    if len(header) != 56:
        raise ValueError("expected a 56-byte download header")
    if not image.startswith(b"81958711") or not 32 < len(image) <= 0x75000:
        raise ValueError("invalid composite image or image exceeds inactive slot")
    yield 1, header
    stream = image + image
    for offset in range(0, len(stream), chunk_size):
        data = stream[offset:offset + chunk_size]
        last = offset + len(data) == len(stream)
        yield 3 if last and final_data else 2, data
    if not final_data:
        yield 3, None


def build_config(flags07=0, flags08=0, flags09=0):
    """Runtime config frame (module 0, cmd 0x1A) - the real cloud sends this with
    TLVs 07/08/09=00, 0A=keepalive(s), 0B=policy interval. One of the flags
    should arm the upgrade FSM."""
    payload = (tlv(0x07, bytes([flags07])) + tlv(0x08, bytes([flags08])) +
              tlv(0x09, bytes([flags09])) + tlv(0x0A, struct.pack(">H", 100)) +
              tlv(0x0B, struct.pack(">I", 0x15180)))
    return frame(0x1A, 0, payload)


async def admin_server(port):
    """Telnet-ish control endpoint: raw <hex> | upaddr <port> | status"""
    async def handle(reader, writer):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                line = line.decode().strip()
                if not line:
                    continue
                log(f"admin: {line}")
                parts = line.split()
                resp = "ok\n"
                if parts[0] == "raw":
                    data = bytes.fromhex("".join(parts[1:]))
                    log(f"inject raw -> plug: {data.hex()}")
                    log(frame_str(data, ">>INJ  "))
                    ok = send_to_plug("runtime", data)
                    resp = ("ok\n" if ok else "no plug connection\n")
                elif parts[0] == "cfg":
                    a, b, c = (parts[1:4] + ["0"] * 3)[:3]
                    f = build_config(int(a), int(b), int(c))
                    log(f"inject config 07={a} 08={b} 09={c}")
                    log(frame_str(f, ">>INJ  "))
                    ok = send_to_plug("runtime", f)
                    resp = ("ok\n" if ok else "no plug connection\n")
                elif parts[0] == "d5inj":
                    mod, cm = int(parts[1], 0), int(parts[2], 0)
                    payload = " ".join(parts[3:]).encode()
                    f = build_d5(mod, cm, payload)
                    log(f"inject d5 relay -> module {mod} cmd 0x{cm:02X} payload {payload[:64]!r}")
                    log(frame_str(f, ">>INJ  "))
                    ok = send_to_plug("runtime", f)
                    resp = ("ok\n" if ok else "no plug connection\n")
                elif parts[0] == "imginfo":
                    # d5 module 3 cmd 0x19: image info. Sets mode/target/threshold
                    # and ERASES the target slot. IMAGE_SIZE must be 56 + 2*len(img):
                    # the stock protocol streams the image TWICE (first pass
                    # discarded, second written) - threshold = 56+len(img),
                    # erase = (IMAGE_SIZE-56)>>1 = len(img), completion = IMAGE_SIZE.
                    size = 56 + 2 * len(UPGRADE["image"])
                    ver = " ".join(parts[1:]) or "V2.0.0(1)_LT"
                    payload = json.dumps({"IMAGE_SIZE": size, "VERSION": ver}).encode()
                    f = build_d5(3, 0x19, payload)
                    log(f"inject d5 03 19 IMAGE_SIZE={size} (erases inactive slot!)")
                    log(frame_str(f, ">>INJ  "))
                    ok = send_to_plug("runtime", f)
                    resp = ("ok\n" if ok else "no plug connection\n")
                elif parts[0] == "trigger":
                    # d5 module 2 cmd 4: arm + post the internal 0x1C FSM event
                    f = build_d5(2, 4)
                    log("inject d5 02 04 upgrade trigger")
                    log(frame_str(f, ">>INJ  "))
                    ok = send_to_plug("runtime", f)
                    resp = ("ok\n" if ok else "no plug connection\n")
                elif parts[0] == "push":
                    if UPGRADE.get("streaming") or UPGRADE.get("armed"):
                        raise ValueError("a push is already pending or streaming")
                    srv = UPGRADE.get("server")
                    if srv and srv.session and not srv.session.is_closing():
                        raise ValueError("previous upgrade session still exists; reconnect the proxy before another push")
                    # push [id0 id1] [expected_hex [entry_size [rdp_len]]]
                    args2 = parts[1:]
                    if len(args2) >= 2:
                        ids = tuple(args2[:2])
                        rest = args2[2:]
                    else:
                        ids = ("OTA2", "OTA1")
                        rest = args2
                    ck = int(rest[0], 0) if len(rest) >= 1 else None
                    es = int(rest[1], 0) if len(rest) >= 2 else None
                    rdp = int(rest[2], 0) if len(rest) >= 3 else None
                    UPGRADE["capture"] = False
                    UPGRADE["active_options"] = dict(UPGRADE["stream_options"])
                    build_dl_header(UPGRADE["image"], ids, ck, es, rdp)
                    size = 56 + 2 * len(UPGRADE["image"])
                    payload = json.dumps({"IMAGE_SIZE": size,
                                          "VERSION": "V2.0.0(1)_LT"}).encode()
                    f1 = build_d5(3, 0x19, payload)
                    log(f"PUSH step 1: d5 03 19 IMAGE_SIZE={size}")
                    log(frame_str(f1, ">>INJ  "))
                    if not send_to_plug("runtime", f1):
                        resp = "no plug connection\n"
                    else:
                        await asyncio.sleep(3)
                        f2 = build_d5(2, 4)
                        log("PUSH step 2: d5 02 04 trigger")
                        log(frame_str(f2, ">>INJ  "))
                        send_to_plug("runtime", f2)
                        UPGRADE["armed"] = True
                        resp = "push started - watch the log\n"
                elif parts[0] == "streamcfg":
                    if UPGRADE.get("armed") or UPGRADE.get("streaming"):
                        raise ValueError("cannot change stream options during a push")
                    chunk, delay = int(parts[1]), int(parts[2])
                    ending = parts[3] if len(parts) > 3 else "empty"
                    if not 1 <= chunk <= 2048 or not 0 <= delay <= 1000 or ending not in ("empty", "data"):
                        raise ValueError("streamcfg <1..2048 bytes> <0..1000 ms> <empty|data>")
                    envelope = parts[4] if len(parts) > 4 else "fe"
                    if envelope not in ("d5", "ff", "fe", "raw1e"):
                        raise ValueError("envelope must be d5, ff, fe, or raw1e")
                    UPGRADE["stream_options"] = {"chunk_size": chunk, "delay_ms": delay,
                                                 "final_data": ending == "data", "envelope": envelope}
                    resp = f"stream options: {UPGRADE['stream_options']}\n"
                elif parts[0] == "upclose":
                    if UPGRADE.get("armed") or UPGRADE.get("streaming"):
                        raise ValueError("cannot close during a pending or active push")
                    srv = UPGRADE.get("server")
                    if srv and srv.session:
                        srv.session.close()
                    resp = "previous upgrade connection closed\n"
                elif parts[0] == "capture":
                    # Request service using the existing image metadata. Do not
                    # send IMAGE_SIZE (which erases flash) or firmware chunks.
                    UPGRADE["armed"] = False
                    UPGRADE["capture"] = True
                    UPGRADE["capture_version"] = parts[1] if len(parts) > 1 else None
                    ok = send_to_plug("runtime", build_d5(2, 4))
                    resp = "capture requested - cloud data will be withheld\n" if ok else "no plug connection\n"
                elif parts[0] == "streamnow":
                    srv = UPGRADE.get("server")
                    if not UPGRADE.get("header"):
                        build_dl_header(UPGRADE["image"])
                    if srv and srv.session:
                        asyncio.get_event_loop().create_task(srv.stream(srv.session))
                        resp = "streaming into live session\n"
                    else:
                        resp = "no upgrade session\n"
                elif parts[0] == "upstart":
                    f = build_upgrade_start()
                    log("inject upgrade-start event (cmd 0x1C mod 3)")
                    ok = send_to_plug("runtime", f)
                    resp = ("ok\n" if ok else "no plug connection\n")
                elif parts[0] == "upaddr":
                    port_up = int(parts[1])
                    f = build_where_upgrade(MY_IP, port_up)
                    log(f"inject where-upgrade -> {MY_IP}:{port_up}")
                    log(frame_str(f, ">>INJ  "))
                    ok = send_to_plug("runtime", f)
                    resp = ("ok\n" if ok else "no plug connection\n")
                elif parts[0] == "status":
                    resp = (f"plug writers: {list(PLUG_WRITERS)} "
                            f"image: {UPGRADE['image'] and len(UPGRADE['image'])} "
                            f"armed: {UPGRADE.get('armed')} "
                            f"capture: {UPGRADE.get('capture', False)} "
                            f"streaming: {UPGRADE.get('streaming', False)} "
                            f"stream_options: {UPGRADE['stream_options']} "
                            f"target: {TARGET_IP} "
                            f"serial_verified: {PLUG_WRITERS.get('runtime') is not None and PLUG_WRITERS.get('runtime') is VERIFIED_RUNTIME} "
                            f"checksum: {hex(UPGRADE.get('checksum', 0))}\n")
                else:
                    resp = ("commands: capture [advertised_version] | push [id0 id1] | imginfo [ver] | trigger | "
                            "d5inj <mod> <cmd> [json] | raw <hex> | upaddr <port> | "
                            "upstart | cfg <a> <b> <c> | status\n")
                writer.write(resp.encode())
                await writer.drain()
        except Exception as e:
            log(f"admin error: {e}")
            writer.write(f"error: {e}\n".encode())
            await writer.drain()
        finally:
            try:
                writer.close()
            except Exception:
                pass
    srv = await asyncio.start_server(handle, "127.0.0.1", port)
    log(f"admin listening on :{port}")
    return srv


class UpgradeServer:
    """Impersonates Tenda's CL_UPGRADE_SRV, serving our own firmware image.

    Confirmed on the test SP9: FB address reply, registration handshake, FC
    service request, then wire FE / module 0 firmware chunks. FE dispatches
    the internal module-3 command 1E. See README.md before pushing; once
    ESPHome runs, use its native OTA service instead of this stock protocol.
    """

    def __init__(self, port, image):
        self.port = port
        self.image = image
        self.session = None

    async def start(self):
        srv = await asyncio.start_server(self.handle, "0.0.0.0", self.port)
        log(f"upgrade server listening on :{self.port} ({len(self.image)} byte image)")
        return srv

    async def handle(self, reader, writer):
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else ""
        if ip != TARGET_IP:
            log(f"UPGRADE: rejecting non-target peer {peer}")
            writer.close()
            return
        if UPGRADE.get("capture"):
            await self.capture_cloud(reader, writer)
            return
        log(f"UPGRADE: plug {peer} connected!")
        self.session = writer
        buf = b""
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    log("UPGRADE: plug disconnected")
                    self.session = None
                    return
                buf += data
                while True:
                    if len(buf) < 2 or buf[:2] != b"\x24\x00":
                        idx = buf.find(b"\x24\x00")
                        buf = buf[idx:] if idx >= 0 else b""
                        if not buf:
                            break
                    if len(buf) < 16:
                        break
                    ln = struct.unpack(">H", buf[6:8])[0]
                    if ln > 1024 * 1024:
                        buf = buf[2:]
                        continue
                    total = 16 + ln
                    if len(buf) < total:
                        break
                    f = buf[:total]
                    buf = buf[total:]
                    log(frame_str(f, "<PLUG "))
                    await self.on_frame(f, writer)
        except Exception as e:
            log(f"UPGRADE error: {e}")
            self.session = None

    async def capture_cloud(self, reader, writer):
        """Relay authentication, record the real service reply, withhold data.

        Only known module-0 session replies reach the plug. In particular FD
        and module-3 frames never reach it, so capture cannot stream an update.
        """
        upstream = None
        tasks = []
        try:
            cloud_reader, upstream = await asyncio.wait_for(
                asyncio.open_connection(*CLOUD_UPGRADE), 10)
            log(f"CAPTURE: connected to real upgrade service {CLOUD_UPGRADE}")

            async def read_frame(src):
                header = await src.readexactly(16)
                if header[:2] != b"\x24\x00":
                    raise ValueError("invalid capture frame magic")
                return header + await src.readexactly(struct.unpack(">H", header[6:8])[0])

            async def to_cloud():
                registered = False
                while True:
                    f = await read_frame(reader)
                    if not registered:
                        if f[5] != 0x17 or tlv_value(f[16:], 1) != TARGET_SN:
                            raise ValueError("capture registration does not match target serial")
                        registered = True
                        version = UPGRADE.get("capture_version")
                        if version:
                            f = replace_tlv_value(f, 3, version.encode("ascii"))
                            log(f"CAPTURE: cloud-only advertised version={version}")
                    log(frame_str(f, "CAPTURE <PLUG "))
                    upstream.write(f)
                    await upstream.drain()

            async def from_cloud():
                while True:
                    f = await read_frame(cloud_reader)
                    log(frame_str(f, "CAPTURE >CLOUD "))
                    if f[4] == 0 and f[5] in (0x18, 0x1A, 0x66):
                        writer.write(f)
                        await writer.drain()
                    else:
                        log(f"CAPTURE: WITHHELD frame={f.hex()}")

            tasks = [asyncio.create_task(to_cloud(), name="plug-to-cloud"),
                     asyncio.create_task(from_cloud(), name="cloud-to-plug")]
            done, _ = await asyncio.wait(tasks, timeout=90, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                log(f"CAPTURE: {task.get_name()} ended")
                task.result()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError) as e:
            log(f"CAPTURE: connection ended ({type(e).__name__})")
        except Exception as e:
            log(f"CAPTURE error: {e}")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if upstream:
                upstream.close()
            writer.close()
            UPGRADE["capture"] = False
            log("CAPTURE: finished; no cloud firmware data forwarded")

    async def on_frame(self, f, writer):
        cmd, module = f[5], f[4]
        payload = f[16:]
        if cmd == 0x17:
            if tlv_value(payload, 1) != TARGET_SN:
                log("UPGRADE: rejecting unexpected registration serial")
                writer.close()
                return
            # Random challenges caused immediate disconnects in the original
            # experiment. Use this plug's real cloud reply or a private capture.
            challenge = CHALLENGE
            if challenge is None:
                challenge = await fetch_cloud_challenge(f)
            reply = frame(0x18, 0, tlv(0x04, challenge), direction=8)
            log("UPGRADE: registered, sending real cloud challenge (dir=08)")
            writer.write(reply)
            await writer.drain()
        elif cmd == 0x19:
            # challenge response -> complete the session with the config frame
            reply = (frame(0x1A, 0,
                           tlv(0x07, b"\x00") + tlv(0x08, b"\x00") +
                           tlv(0x09, b"\x00") + tlv(0x0A, struct.pack(">H", 100)) +
                           tlv(0x0B, struct.pack(">I", 0x15180)), direction=8))
            log(f"UPGRADE: challenge answered {payload.hex()}, sending config")
            writer.write(reply)
            await writer.drain()
        elif module == 0 and cmd == 0x65:
            writer.write(frame(0x66, 0, direction=8))
            await writer.drain()
        elif (module == 3 and cmd == 0x1C) or (cmd == 0xFC and f[8:10] == b"\x03\x1c"):
            log(f"UPGRADE: upgrade_service_req: {payload.decode(errors='replace')}")
            if not UPGRADE.get("armed"):
                log("UPGRADE: NOT armed - not streaming (admin 'push' arms it)")
                return
            if UPGRADE.get("streaming"):
                log("UPGRADE: ignoring duplicate request while streaming")
                return
            if not UPGRADE.get("header"):
                build_dl_header(self.image)
            UPGRADE["streaming"] = True
            asyncio.get_event_loop().create_task(self.start_and_stream(writer))
        elif module == 3 and cmd == 0x18:
            log("UPGRADE: image-info query seen")
        else:
            log(f"UPGRADE: frame mod={module} cmd=0x{cmd:02X} "
                f"payload={payload[:64].hex()}")
    async def start_and_stream(self, writer):
        """Stream the emulated cloud format without speculative FD messages."""
        try:
            await asyncio.sleep(1)
            await self.stream(writer)
        except Exception as e:
            log(f"UPGRADE start_and_stream error: {e}")
        finally:
            UPGRADE["streaming"] = False
            UPGRADE["armed"] = False

    async def stream(self, writer):
        try:
            hdr = UPGRADE["header"]
            img = self.image
            t0 = time.time()
            n = 0
            sent = 0
            options = UPGRADE.get("active_options", UPGRADE["stream_options"])
            log(f"UPGRADE: clean stream start options={options} IMAGE_SIZE={56 + 2 * len(img)}")

            async def send(state, data):
                nonlocal n, sent
                payload = tlv(0x1A, bytes([state]))
                if data is not None:
                    payload += tlv(0x1C, data)
                envelope = options.get("envelope", "fe")
                if envelope == "d5":
                    f = frame(0xD5, 0, payload, direction=8, flags=b"\x03\x1e" + bytes(6))
                elif envelope in ("ff", "fe"):
                    f = frame(int(envelope, 16), 0, payload, direction=8)
                else:
                    f = frame(0x1E, 3, payload, direction=8)
                writer.write(f)
                await writer.drain()
                n += 1
                sent += len(data) if data is not None else 0
                await asyncio.sleep(options["delay_ms"] / 1000)

            total = 56 + 2 * len(img)
            for state, data in iter_upgrade_chunks(img, hdr, options["chunk_size"], options["final_data"]):
                await send(state, data)
                if n % 64 == 0:
                    log(f"UPGRADE: {sent}/{total} stream bytes ({n} chunks, "
                        f"{time.time()-t0:.1f}s)")
            log(f"UPGRADE: clean stream done: {sent}/{total} bytes in {n} frames, {time.time()-t0:.1f}s")
            UPGRADE["armed"] = False
        except Exception as e:
            log(f"UPGRADE stream error: {e}")


async def main():
    global MY_IP, LOG, TARGET_IP, TARGET_SN, CHALLENGE, CLOUD_UPGRADE
    ap = argparse.ArgumentParser()
    ap.add_argument("--my-ip", required=True, type=ipv4, help="LAN IP of this proxy (plug-visible)")
    ap.add_argument("--target-ip", required=True, type=ipv4, help="only this plug may connect")
    ap.add_argument("--target-sn", required=True, help="serial number from the selected plug")
    ap.add_argument("--challenge-file", type=Path, help="optional private 32-byte captured challenge")
    ap.add_argument("--cloud-upgrade", default="47.254.24.171:1823", help="real upgrade service")
    ap.add_argument("--real-rendezvous", default="47.254.24.171:1821")
    ap.add_argument("--logs", default=str(Path(__file__).resolve().parents[1] / "logs"))
    ap.add_argument("--no-rewrite", action="store_true",
                    help="relay only; do not rewrite the redirect (runtime stays direct)")
    ap.add_argument("--upgrade-port", type=int, default=11813,
                    help="port for our fake upgrade server")
    ap.add_argument("--upgrade-image", default=str(Path(__file__).resolve().parents[1] / "esphome/sp9_esphome_ota2.bin"),
                    help="OTA image file to serve")
    args = ap.parse_args()
    MY_IP = args.my_ip
    TARGET_IP = args.target_ip
    if len(args.target_sn) != 17 or not args.target_sn.isascii() or not args.target_sn.isalnum():
        ap.error("--target-sn must be the selected plug's 17-character alphanumeric serial")
    TARGET_SN = args.target_sn.encode("ascii")
    if args.challenge_file:
        CHALLENGE = args.challenge_file.read_bytes()
        if len(CHALLENGE) != 32:
            ap.error("--challenge-file must contain exactly 32 raw bytes")
    cloud_host, cloud_port = args.cloud_upgrade.rsplit(":", 1)
    CLOUD_UPGRADE = (cloud_host, int(cloud_port))
    AUTO_ADDR["port"] = args.upgrade_port
    image = Path(args.upgrade_image).read_bytes()
    # Validate before opening listeners or allowing IMAGE_SIZE to erase flash.
    if image[:8] != b"81958711" or not 32 < len(image) <= 0x75000:
        ap.error("invalid/oversized prepared OTA2 image")
    if struct.unpack_from("<I", image, 12)[0] != 0:
        ap.error("unexpected XIP load-address field; use the prepared OTA2 artifact")
    os.makedirs(args.logs, exist_ok=True)
    LOG = open(os.path.join(args.logs, "mitm.log"), "a")

    r_ip, r_port = args.real_rendezvous.split(":")
    rendezvous = Relay(1821, (r_ip, int(r_port)), "rendezvous",
                      rewrite=None if args.no_rewrite else rewrite_redirect)
    await rendezvous.start()

    # runtime relay: upstream resolved from the rewritten redirect stash
    # we need the REAL ip too - patch rewrite_redirect to stash it
    runtime = Relay(11812, runtime_upstream, "runtime",
                   rewrite=None if args.no_rewrite else rewrite_redirect)
    await runtime.start()

    # The image was validated before opening any listeners.
    UPGRADE["image"] = image
    up = UpgradeServer(args.upgrade_port, image)
    UPGRADE["server"] = up
    await up.start()
    await admin_server(9999)

    log(f"MITM up. my-ip={MY_IP} no-rewrite={args.no_rewrite}")
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
