"""Offline checks for the capture boundary; no sockets or flash access."""
import asyncio
from contextlib import ExitStack
import unittest
from unittest.mock import patch

import tenda_mitm as mitm


TEST_IP = "192.0.2.10"
TEST_SN = b"TESTSN00000000001"


class Writer:
    def __init__(self, ip=TEST_IP):
        self.data = bytearray()
        self.closed = False
        self.ip = ip

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass

    def is_closing(self):
        return self.closed

    def get_extra_info(self, key):
        return (self.ip, 12345) if key == "peername" else None


def reader(data):
    src = asyncio.StreamReader()
    src.feed_data(data)
    src.feed_eof()
    return src


class CaptureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(mitm, "TARGET_IP", TEST_IP))
        stack.enter_context(patch.object(mitm, "TARGET_SN", TEST_SN))
        stack.enter_context(patch.object(mitm, "VERIFIED_RUNTIME", None))
        stack.enter_context(patch.object(mitm, "log"))

    async def test_runtime_injection_requires_matching_current_registration(self):
        plug = Writer()
        queue = asyncio.Queue()
        registration = mitm.frame(0x17, 0, mitm.tlv(1, TEST_SN), direction=6)
        with patch.dict(mitm.PLUG_WRITERS, {"runtime": plug}, clear=True):
            self.assertFalse(mitm.send_to_plug("runtime", b"erase"))
            self.assertEqual(plug.data, b"")
            await mitm.read_frames(reader(registration), queue, "runtime", "test", plug)
            self.assertIs(mitm.VERIFIED_RUNTIME, plug)
            self.assertEqual(await queue.get(), registration)
            self.assertTrue(mitm.send_to_plug("runtime", b"allowed"))
            replacement = Writer()
            mitm.PLUG_WRITERS["runtime"] = replacement
            self.assertFalse(mitm.send_to_plug("runtime", b"erase"))
            self.assertEqual(replacement.data, b"")

    async def test_runtime_rejects_unexpected_serial_without_forwarding(self):
        plug, queue = Writer(), asyncio.Queue()
        registration = mitm.frame(0x17, 0, mitm.tlv(1, b"wrong"), direction=6)
        await mitm.read_frames(reader(registration), queue, "runtime", "test", plug)
        self.assertTrue(plug.closed)
        self.assertIsNone(mitm.VERIFIED_RUNTIME)
        self.assertIsNone(await queue.get())

    async def test_fetches_challenge_for_target_and_closes_cloud_connection(self):
        register = mitm.frame(0x17, 0, mitm.tlv(1, TEST_SN), direction=6)
        challenge = bytes(range(32))
        cloud = Writer()

        async def connect(*args):
            return reader(mitm.frame(0x18, 0, mitm.tlv(4, challenge), direction=8)), cloud

        with patch.object(mitm.asyncio, "open_connection", connect):
            self.assertEqual(await mitm.fetch_cloud_challenge(register), challenge)
        self.assertEqual(cloud.data, register)
        self.assertTrue(cloud.closed)

    async def test_fetch_rejects_wrong_device_before_connecting(self):
        register = mitm.frame(0x17, 0, mitm.tlv(1, b"wrong"), direction=6)
        with patch.object(mitm.asyncio, "open_connection") as connect:
            with self.assertRaises(ValueError):
                await mitm.fetch_cloud_challenge(register)
            connect.assert_not_called()

    async def test_fetch_rejects_non_challenge_and_wrong_length(self):
        register = mitm.frame(0x17, 0, mitm.tlv(1, TEST_SN), direction=6)
        for reply in (mitm.frame(0xFE, 0, mitm.tlv(4, bytes(32)), direction=8),
                      mitm.frame(0x18, 0, mitm.tlv(4, bytes(31)), direction=8),
                      mitm.frame(0x18, 0, mitm.tlv(4, bytes(32)), direction=3)):
            cloud = Writer()

            async def connect(*args):
                return reader(reply), cloud

            with patch.object(mitm.asyncio, "open_connection", connect):
                with self.assertRaises(ValueError):
                    await mitm.fetch_cloud_challenge(register)
            self.assertTrue(cloud.closed)

    def test_tlv_rejects_truncated_value(self):
        self.assertIsNone(mitm.tlv_value(b"\x00\x01\x00\x11short", 1))

    async def test_clean_stream_has_no_fd_and_preserves_data(self):
        img = b"81958711" + bytes(range(256)) * 4
        hdr = bytes(56)
        plug = Writer()
        with patch.dict(mitm.UPGRADE, {"header": hdr, "armed": True,
                                     "active_options": {"chunk_size": 256, "delay_ms": 0, "final_data": False}}), patch.object(mitm, "log"):
            await mitm.UpgradeServer(11813, img).stream(plug)
        buf = bytes(plug.data)
        received = bytearray()
        states = []
        while buf:
            length = int.from_bytes(buf[6:8], "big")
            f, buf = buf[:16 + length], buf[16 + length:]
            self.assertEqual(f[2:6], b"\x08\x00\x00\xfe")
            self.assertEqual(f[8:16], bytes(8))
            self.assertEqual(f[16:20], b"\x00\x1a\x00\x01")
            states.append(f[20])
            if len(f) > 21:
                self.assertEqual(f[21:23], b"\x00\x1c")
                received.extend(f[25:])
        self.assertEqual(received, hdr + img + img)
        self.assertEqual(states[0], 1)
        self.assertEqual(states[-1], 3)
        self.assertTrue(all(state == 2 for state in states[1:-1]))

    def test_advertised_version_preserves_serial_and_frame_fields(self):
        register = mitm.frame(0x17, 0, mitm.tlv(1, mitm.TARGET_SN) +
                              mitm.tlv(3, b"current") + mitm.tlv(0x13, b"SP9V1.0"),
                              direction=6, flags=b"\x03\x2a" + bytes(6))
        rewritten = mitm.replace_tlv_value(register, 3, b"older-version")
        expected = mitm.frame(0x17, 0, mitm.tlv(1, mitm.TARGET_SN) +
                             mitm.tlv(3, b"older-version") + mitm.tlv(0x13, b"SP9V1.0"),
                             direction=6, flags=b"\x03\x2a" + bytes(6))
        self.assertEqual(rewritten, expected)

    async def test_service_and_firmware_frames_never_reach_plug(self):
        handshake = mitm.frame(0x18, 0, mitm.tlv(4, bytes(32)), direction=8)
        info = mitm.frame(0xFD, 0, b"metadata", direction=8)
        chunk = b"".join(mitm.frame(cmd, module, mitm.tlv(0x1C, b"firmware"), direction=8)
                         for cmd, module in [(0x1D, 3), (0x1E, 3), (0xFF, 0), (0xFE, 0), (0xD5, 0)])
        register = mitm.frame(0x17, 0, mitm.tlv(1, mitm.TARGET_SN), direction=6)
        request = mitm.frame(0xFC, 0, b'{"UPGRADE_TYPE":1}', direction=6)
        plug, cloud = Writer(), Writer()

        async def connect(*args):
            return reader(handshake + info + chunk), cloud

        with patch.object(mitm.asyncio, "open_connection", connect), patch.object(mitm, "log"):
            await mitm.UpgradeServer(11813, b"").capture_cloud(reader(register + request), plug)
        self.assertEqual(plug.data, handshake)
        self.assertEqual(cloud.data, register + request)
        self.assertTrue(plug.closed and cloud.closed)

    async def test_wrong_serial_is_not_forwarded(self):
        cloud, plug = Writer(), Writer()

        async def connect(*args):
            return reader(b""), cloud

        register = mitm.frame(0x17, 0, mitm.tlv(1, b"another-plug"), direction=6)
        with patch.object(mitm.asyncio, "open_connection", connect), patch.object(mitm, "log"):
            await mitm.UpgradeServer(11813, b"").capture_cloud(reader(register), plug)
        self.assertEqual(cloud.data, b"")

    async def test_wrong_ip_is_rejected_before_cloud_connection(self):
        plug = Writer("192.0.2.11")
        with patch.object(mitm.asyncio, "open_connection") as connect, patch.object(mitm, "log"):
            await mitm.UpgradeServer(11813, b"").handle(reader(b""), plug)
        connect.assert_not_called()
        self.assertTrue(plug.closed)

    def test_injection_rejects_wrong_peer_and_stale_connection(self):
        with patch.object(mitm, "log"), patch.dict(mitm.PLUG_WRITERS, {}, clear=True):
            other = Writer("192.0.2.11")
            mitm.PLUG_WRITERS["runtime"] = other
            self.assertFalse(mitm.send_to_plug("runtime", b"test"))
            self.assertEqual(other.data, b"")
            stale = Writer()
            stale.close()
            mitm.PLUG_WRITERS["runtime"] = stale
            self.assertFalse(mitm.send_to_plug("runtime", b"test"))


if __name__ == "__main__":
    unittest.main()
