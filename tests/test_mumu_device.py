import unittest

from tools.resolve_mumu_device import DeviceProbe, group_instances, resolve_probe


PACKAGE_PATH = "package:/data/app/com.glkj.lhcx.aligames/base.apk"


def probe(serial, android_id, boot_id, package_path=PACKAGE_PATH):
    return DeviceProbe(serial, "device", android_id, boot_id, package_path)


class MumuDeviceResolverTests(unittest.TestCase):
    def test_duplicate_serials_are_one_instance_and_tcp_alias_is_preferred(self):
        probes = [
            probe("emulator-5556", "android-local", "boot-local"),
            probe("127.0.0.1:16416", "android-local", "boot-local"),
            probe("127.0.0.1:16384", "android-official", "boot-official", ""),
        ]
        self.assertEqual(len(group_instances(probes)), 1)
        selected, aliases = resolve_probe(probes)
        self.assertEqual(selected.serial, "127.0.0.1:16416")
        self.assertEqual(
            [item.serial for item in aliases],
            ["127.0.0.1:16416", "emulator-5556"],
        )

    def test_explicit_alias_is_preserved(self):
        probes = [
            probe("emulator-5556", "android-local", "boot-local"),
            probe("127.0.0.1:16416", "android-local", "boot-local"),
        ]
        selected, aliases = resolve_probe(probes, "emulator-5556")
        self.assertEqual(selected.serial, "emulator-5556")
        self.assertEqual(len(aliases), 2)

    def test_distinct_local_instances_require_explicit_selection(self):
        probes = [
            probe("127.0.0.1:16416", "android-one", "boot-one"),
            probe("127.0.0.1:16448", "android-two", "boot-two"),
        ]
        with self.assertRaisesRegex(RuntimeError, "multiple distinct ADB instances"):
            resolve_probe(probes)

    def test_official_instance_without_local_package_is_ignored(self):
        probes = [
            probe("127.0.0.1:16384", "android-official", "boot-official", ""),
            probe("127.0.0.1:16416", "android-local", "boot-local"),
        ]
        selected, _ = resolve_probe(probes)
        self.assertEqual(selected.serial, "127.0.0.1:16416")


if __name__ == "__main__":
    unittest.main()
