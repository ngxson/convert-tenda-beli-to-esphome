"""Synthetic artifact checks: no real firmware or credentials in fixtures."""
import struct
import unittest

from prepare_cloud_image import prepare


def segment(length, address):
    return b"81958711" + struct.pack("<II", length, address) + bytes(16)


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.ram = b"synthetic RAM" + bytes(4)
        self.prefix = segment(4, 0) + b"XIP!" + segment(len(self.ram), 0x10005000)
        self.image = self.prefix + self.ram[:-4]

    def test_restores_only_confirmed_zeros(self):
        image, missing = prepare(self.image, self.ram)
        self.assertEqual(missing, 4)
        self.assertEqual(image, self.prefix + self.ram)

    def test_complete_image_is_unchanged(self):
        image = self.prefix + self.ram
        self.assertEqual(prepare(image, self.ram), (image, 0))

    def test_rejects_missing_nonzero_data(self):
        with self.assertRaisesRegex(ValueError, "confirmed zeros"):
            prepare(self.image, self.ram[:-1] + b"!")

    def test_rejects_mismatched_artifacts(self):
        with self.assertRaisesRegex(ValueError, "do not match"):
            prepare(self.image, b"different RAM" + bytes(4))

    def test_rejects_unexpected_xip_header_address(self):
        with self.assertRaisesRegex(ValueError, "XIP load-address"):
            prepare(self.image[:12] + struct.pack("<I", 0x0800B020) + self.image[16:], self.ram)

    def test_rejects_truncated_header(self):
        with self.assertRaises(ValueError):
            prepare(b"81958711", b"")

    def test_rejects_oversized_image(self):
        xip = bytes(0x75000)
        image = segment(len(xip), 0) + xip + segment(len(self.ram), 0x10005000) + self.ram
        with self.assertRaisesRegex(ValueError, "exceeds"):
            prepare(image, self.ram)


if __name__ == "__main__":
    unittest.main()
