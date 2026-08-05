import os
import tempfile
import unittest
from pathlib import Path


TEST_DIR = tempfile.TemporaryDirectory()
os.environ.setdefault("SOULTIDE_DB_PATH", str(Path(TEST_DIR.name) / "soultide-test.db"))

import protocol_codec
import tcp_server


class ProtocolCodecTests(unittest.TestCase):
    def test_story_completion_matches_verified_encoder(self):
        pod = {
            "cid": 1901013,
            "unlockChapters": {1: True},
            "isAllComplete": True,
        }
        generated = protocol_codec.encode_method(3404, pod, [], 37, 0, 37, 0)
        verified = tcp_server.encode_story_completion_notify(1901013, 1, 37, True)
        self.assertEqual(generated, verified)

    def test_gift_result_scalar_types(self):
        self.assertEqual(
            protocol_codec.encode_method(1907, 0, 20010001, 10001, False, 40).hex(),
            "505f11543101531127005128",
        )

    def test_empty_oath_pod_shape(self):
        body = protocol_codec.encode_method(
            1914,
            0,
            20010001,
            {"activation": False, "countData": {}, "dateData": {}},
        )
        self.assertEqual(body.hex(), "505f11543101c1035101005102c05103c0")

    def test_rejects_wrong_bool_type(self):
        with self.assertRaises(ValueError):
            protocol_codec.encode_method(1907, 0, 20010001, 10001, 0, 40)

    def test_sparse_integer_byte_mask(self):
        self.assertEqual(protocol_codec.encode_int(30010112).hex(), "5eebc901")
        decoder = protocol_codec.Decoder(bytes.fromhex("5eebc901"))
        self.assertEqual(decoder.integer(), 30010112)

    def test_long_uses_eight_byte_mask(self):
        value = 5056590455767145143
        encoded = protocol_codec.encode_value("long", value)
        self.assertEqual(encoded.hex(), "90ffb79aa1823a9e2c46")
        decoder = protocol_codec.Decoder(encoded)
        self.assertEqual(decoder.value("long"), value)

    def test_zero_double_uses_empty_byte_mask(self):
        self.assertEqual(protocol_codec.encode_value("double", 0.0), b"\x80\x00")
        decoder = protocol_codec.Decoder(b"\x80\x00")
        self.assertEqual(decoder.value("double"), 0.0)

    def test_basic_home_info_reward_list_is_typed(self):
        value = {
            "id": 1575339108350226935,
            "pname": "人偶师",
            "currentComfort": 0,
            "maxComfort": 0,
            "alreadyReward": [1, 3],
        }
        encoded = protocol_codec.encode_value("BasicHomeInfoPOD", value)
        decoder = protocol_codec.Decoder(encoded)
        self.assertEqual(decoder.value("BasicHomeInfoPOD"), value)
        self.assertEqual(decoder.offset, len(encoded))


if __name__ == "__main__":
    unittest.main()
