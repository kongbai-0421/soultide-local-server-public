import argparse
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import zipfile

from tools.patch_apk_entries import _installed_manifest
from tools import sync_nonfull_resources as device_sync
from tools.sync_from_official_device import local_candidates, synchronize
from apk_merge import _manifest_bytes, merge


class ResourceManifestTests(unittest.TestCase):
    def test_official_source_is_only_needed_for_missing_or_invalid_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            valid = root / "valid.ab"
            valid.write_bytes(b"valid-resource")
            invalid = root / "invalid.ab"
            invalid.write_bytes(b"wrong")
            rows = [
                {
                    "RelativePath": "valid.ab",
                    "storeRootPathId": 2,
                    "Length": valid.stat().st_size,
                    "Md5": "c5c7f6f8863b2f0a2a5f8f38f8b0e4d1",
                },
                {
                    "RelativePath": "invalid.ab",
                    "storeRootPathId": 2,
                    "Length": len(b"expected-resource"),
                    "Md5": "5cfd4f0e4d0d2b97f2c23e2e5d4a8d11",
                },
                {
                    "RelativePath": "missing.ab",
                    "storeRootPathId": 2,
                    "Length": 7,
                    "Md5": "321c3cf486ed509164edec1e1981fec8",
                },
            ]
            rows[0]["Md5"] = hashlib.md5(valid.read_bytes()).hexdigest()
            candidates = local_candidates(root, rows)
            self.assertEqual(
                [relative for _, relative in candidates],
                ["invalid.ab", "missing.ab"],
            )

    def test_complete_local_mirror_has_no_official_candidates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = b"complete"
            path = root / "bundle.ab"
            path.write_bytes(payload)
            rows = [
                {
                    "RelativePath": "bundle.ab",
                    "storeRootPathId": 2,
                    "Length": len(payload),
                    "Md5": hashlib.md5(payload).hexdigest(),
                }
            ]
            self.assertEqual(local_candidates(root, rows), [])

    def test_complete_mirror_does_not_call_official_device(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload = b"complete"
            resource = root / "bundle.ab"
            resource.write_bytes(payload)
            manifest = root / "version-local-nonfull.json"
            manifest.write_text(
                json.dumps(
                    {
                        "AssetBundleList": [
                            {
                                "RelativePath": "bundle.ab",
                                "storeRootPathId": 2,
                                "Length": len(payload),
                                "Md5": hashlib.md5(payload).hexdigest(),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            fake_adb = root / "adb.exe"
            args = argparse.Namespace(
                adb=fake_adb,
                resource_root=root,
                manifest=manifest,
                scope="all-external",
                device="not-connected",
                package="com.example.game",
                remote_root=None,
            )
            with patch("tools.sync_from_official_device.source_manifest") as source:
                self.assertEqual(synchronize(args), 0)
                source.assert_not_called()

    def test_external_zero_tag_bundles_keep_official_tag_and_download_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "version-remote.json").write_text(
                json.dumps(
                    {
                        "AssetBundleList": [
                            {"RelativePath": "embedded.ab", "Tags": [0]},
                            {"RelativePath": "external.ab", "Tags": [0]},
                            {"RelativePath": "official.ab", "Tags": [10, 20010025]},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            embedded = root / "embedded.json"
            embedded.write_text(
                json.dumps(
                    {
                        "AssetBundleList": [
                            {"RelativePath": "embedded.ab", "storeRootPathId": 1}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            manifest = json.loads(_installed_manifest(root, embedded))
            by_path = {item["RelativePath"]: item for item in manifest["AssetBundleList"]}
            self.assertEqual(by_path["embedded.ab"]["storeRootPathId"], 1)
            self.assertEqual(by_path["external.ab"]["storeRootPathId"], 2)
            self.assertEqual(by_path["external.ab"]["Tags"], [0])
            self.assertEqual(by_path["official.ab"]["Tags"], [10, 20010025])
            self.assertEqual(
                manifest["AbTagInfos"],
                [
                    {"Tag": 0, "VersionCode": 0},
                    {"Tag": 20010025, "VersionCode": 0},
                ],
            )

    def test_nonfull_apk_manifest_matches_external_root_split(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "version-local.json").write_text(
                json.dumps(
                    {
                        "AssetBundleList": [
                            {"RelativePath": "embedded.ab", "Tags": [0], "storeRootPathId": 1},
                            {"RelativePath": "external.ab", "Tags": [0], "storeRootPathId": 2},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            manifest = json.loads(_manifest_bytes(root, False))
            by_path = {item["RelativePath"]: item for item in manifest["AssetBundleList"]}
            self.assertEqual(by_path["embedded.ab"]["storeRootPathId"], 1)
            self.assertEqual(by_path["external.ab"]["storeRootPathId"], 2)
            self.assertEqual(by_path["external.ab"]["Tags"], [0])
            self.assertEqual(manifest["AbTagInfos"], [{"Tag": 0, "VersionCode": 0}])

    def test_nonfull_manifest_is_preferred_when_present(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "version-local-nonfull.json").write_text(
                json.dumps(
                    {
                        "AssetBundleList": [
                            {
                                "RelativePath": "whisper.mp4",
                                "Tags": [10],
                                "storeRootPathId": 2,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (root / "version-local.json").write_text(
                json.dumps(
                    {
                        "AssetBundleList": [
                            {
                                "RelativePath": "whisper.mp4",
                                "Tags": [10],
                                "storeRootPathId": 1,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            manifest = json.loads(_manifest_bytes(root, False))
            row = manifest["AssetBundleList"][0]
            self.assertEqual(row["storeRootPathId"], 2)

    def test_nonfull_manifest_keeps_patched_luajit_embedded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = root / "patched-luajit.ab.x64"
            bundle.write_bytes(b"patched-luajit")
            relative = "16_luaab/luajit/luajit_base.ab.x64"
            (root / "version-local-nonfull.json").write_text(
                json.dumps(
                    {
                        "AssetBundleList": [
                            {
                                "RelativePath": relative,
                                "Tags": [0],
                                "storeRootPathId": 1,
                                "Length": 1,
                                "Md5": "OLD",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            manifest = json.loads(
                _manifest_bytes(root, False, {relative: bundle})
            )
            row = manifest["AssetBundleList"][0]
            self.assertEqual(row["storeRootPathId"], 1)
            self.assertEqual(row["Tags"], [0])
            self.assertEqual(manifest["AbTagInfos"], [{"Tag": 0, "VersionCode": 0}])
            self.assertEqual(row["Length"], bundle.stat().st_size)
            self.assertEqual(
                row["Md5"],
                hashlib.md5(bundle.read_bytes()).hexdigest().upper(),
            )

    def test_update_staging_embeds_changed_root_one_and_keeps_patched_luajit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            resource_root = root / "Android"
            (resource_root / "16_luaab" / "luajit").mkdir(parents=True)
            (resource_root / "changed").mkdir()
            (resource_root / "Android").write_bytes(b"asset-map")
            (resource_root / "assetMap.clv").write_bytes(b"asset-map-clv")
            official_luajit = resource_root / "16_luaab" / "luajit" / "luajit_base.ab.x64"
            official_luajit.write_bytes(b"official-5392-luajit")
            official_x86 = resource_root / "16_luaab" / "luajit" / "luajit_base.ab.x86"
            official_x86.write_bytes(b"official-5392-x86")
            embedded_change = resource_root / "changed" / "ui.ab"
            embedded_change.write_bytes(b"changed-root-one")
            external_change = resource_root / "changed" / "external.ab"
            external_change.write_bytes(b"changed-root-two")
            relative_x64 = "16_luaab/luajit/luajit_base.ab.x64"
            relative_x86 = "16_luaab/luajit/luajit_base.ab.x86"
            rows = [
                {"RelativePath": relative_x64, "storeRootPathId": 1, "Tags": [0]},
                {"RelativePath": relative_x86, "storeRootPathId": 1, "Tags": [0]},
                {"RelativePath": "changed/ui.ab", "storeRootPathId": 1, "Tags": [0]},
                {"RelativePath": "changed/external.ab", "storeRootPathId": 2, "Tags": [0]},
            ]
            manifest = {"InternalResourceVersion": 5392, "AssetBundleList": rows}
            for name in ("version-local-nonfull.json", "version-local.json"):
                (resource_root / name).write_text(json.dumps(manifest), encoding="utf-8")
            (resource_root / "staging-report.json").write_text(
                json.dumps({"changedPaths": [relative_x64, "changed/ui.ab", "changed/external.ab"]}),
                encoding="utf-8",
            )
            patched_x64 = root / "patched-x64.ab"
            patched_x64.write_bytes(b"patched-5392-luajit")
            patched_x86 = root / "patched-x86.ab"
            patched_x86.write_bytes(b"patched-5392-x86")
            source_apk = root / "source.apk"
            with zipfile.ZipFile(source_apk, "w") as archive:
                archive.writestr("assets/original.txt", b"original")
            output_apk = root / "output.apk"
            built_manifest = root / "built.json"
            merge(
                source_apk,
                resource_root,
                output_apk,
                local_luajit_x64=patched_x64,
                local_luajit_x86=patched_x86,
                manifest_output=built_manifest,
            )
            with zipfile.ZipFile(output_apk) as archive:
                self.assertEqual(
                    archive.read("assets/Android/" + relative_x64),
                    patched_x64.read_bytes(),
                )
                self.assertEqual(
                    archive.read("assets/Android/changed/ui.ab"),
                    embedded_change.read_bytes(),
                )
                self.assertNotIn("assets/Android/changed/external.ab", archive.namelist())
            built_rows = {
                row["RelativePath"]: row
                for row in json.loads(built_manifest.read_text(encoding="utf-8"))["AssetBundleList"]
            }
            self.assertEqual(built_rows[relative_x64]["Length"], patched_x64.stat().st_size)
            self.assertEqual(
                built_rows[relative_x64]["Md5"],
                hashlib.md5(patched_x64.read_bytes()).hexdigest().upper(),
            )

    def test_nonfull_manifest_restores_polluted_zero_tags(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "version-local-nonfull.json").write_text(
                json.dumps(
                    {
                        "AssetBundleList": [
                            {
                                "RelativePath": "ordinary.ab",
                                "Tags": [10, 20010002],
                                "storeRootPathId": 2,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (root / "version-local.json").write_text(
                json.dumps(
                    {
                        "AssetBundleList": [
                            {
                                "RelativePath": "ordinary.ab",
                                "Tags": [0],
                                "storeRootPathId": 2,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            manifest = json.loads(_manifest_bytes(root, False))
            self.assertEqual(manifest["AssetBundleList"][0]["Tags"], [0])

    def test_built_manifest_matches_nonfull_runtime_contract(self):
        root = Path(__file__).resolve().parent
        candidates = (
            root / "analysis" / "official-5392" / "build" / "version-local-nonfull-built-5392-v2.json",
            root / "apk_build" / "version-local-nonfull-built.json",
        )
        built = next((path for path in candidates if path.is_file()), candidates[-1])
        if not built.is_file():
            self.skipTest("built non-full manifest is not present")
        manifest = json.loads(built.read_text(encoding="utf-8"))
        rows = manifest["AssetBundleList"]
        external = [row for row in rows if int(row.get("storeRootPathId") or 0) == 2]
        media = [
            row
            for row in external
            if str(row.get("RelativePath") or "").lower().endswith((".mp4", ".webm", ".mov"))
        ]
        self.assertEqual(len(rows), 937)
        self.assertEqual(len(external), 782)
        self.assertEqual(len(media), 302)
        self.assertEqual(
            sum(row.get("Tags") == [0] for row in external),
            629,
        )
        self.assertIn(
            {"Tag": 0, "VersionCode": int(manifest["InternalResourceVersion"])},
            manifest["AbTagInfos"],
        )

    def test_nonfull_manifest_preserves_whisper_owner_tags(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "version-local-nonfull.json").write_text(
                json.dumps(
                    {
                        "AssetBundleList": [
                            {
                                "RelativePath": "21_Media/CG/Whisper/Juewa/Whisper01/Juewa_02.mp4",
                                "Tags": [10, 20010017],
                                "storeRootPathId": 2,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            manifest = json.loads(_manifest_bytes(root, False))
            row = manifest["AssetBundleList"][0]
            self.assertEqual(row["storeRootPathId"], 2)
            self.assertEqual(row["Tags"], [10, 20010017])

    @patch("tools.sync_nonfull_resources.push_atomic")
    @patch("tools.sync_nonfull_resources.prepare_external_directories")
    @patch("tools.sync_nonfull_resources.remote_owner", return_value=None)
    @patch("tools.sync_nonfull_resources.remote_hashes", return_value={})
    @patch("tools.sync_nonfull_resources.remote_sizes", return_value={})
    @patch("tools.sync_nonfull_resources.run_adb", return_value="")
    def test_batched_device_repair_does_not_publish_manifest(
        self, _run_adb, _remote_sizes, _remote_hashes, _remote_owner,
        _prepare_directories, push_atomic,
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = []
            for index in range(3):
                payload = f"bundle-{index}".encode()
                relative = f"bundle-{index}.ab"
                (root / relative).write_bytes(payload)
                rows.append(
                    {
                        "RelativePath": relative,
                        "storeRootPathId": 2,
                        "Length": len(payload),
                        "Md5": hashlib.md5(payload).hexdigest(),
                    }
                )
            manifest = root / "version.json"
            manifest.write_text(json.dumps({"AssetBundleList": rows}), encoding="utf-8")
            adb = root / "adb.exe"
            adb.write_bytes(b"")
            args = argparse.Namespace(
                adb=adb,
                resource_root=root,
                manifest=manifest,
                device="emulator-test",
                package="com.example.game",
                quick_verify=False,
                max_repairs=2,
            )
            self.assertEqual(device_sync.synchronize(args), 2)
            self.assertEqual(push_atomic.call_count, 2)
            destinations = [call.args[3] for call in push_atomic.call_args_list]
            self.assertFalse(any(path.endswith("version.json") for path in destinations))

    def test_production_repair_does_not_use_mumu_exec_in_tar_stream(self):
        source = (
            Path(__file__).resolve().parent / "tools" / "sync_nonfull_resources.py"
        ).read_text(encoding="utf-8")
        repair_loop = source[source.index("selected = candidates[:repair_limit]"):source.index("if args.max_repairs and len(repaired)")]
        self.assertIn("push_atomic", repair_loop)
        self.assertNotIn("push_tar_batch", repair_loop)

    def test_installer_runs_manifest_handshake_before_external_sync(self):
        installer = (
            Path(__file__).resolve().parent / "tools" / "install_nonfull.ps1"
        ).read_text(encoding="utf-8")
        handshake = installer.index("Running first-launch manifest handshake")
        sync = installer.index("StreamingMd5 handshake complete; synchronizing")
        second_launch = installer.index("Running second-launch resource stability verification")
        quick_verify = installer.index("-QuickVerify", second_launch)
        self.assertLess(handshake, sync)
        self.assertLess(sync, second_launch)
        self.assertLess(second_launch, quick_verify)
        self.assertIn("$handshakeComplete", installer)
        self.assertIn("$manifestResourceVersion = [int]$manifestObject.InternalResourceVersion", installer)
        self.assertIn("second-launch resources=$($externalRows.Count)/$($externalRows.Count)", installer)
        self.assertNotIn("[int]$ExpectedResourceVersion = 5387", installer)

    def test_sync_wrapper_exposes_quick_verify_without_repair(self):
        wrapper = (
            Path(__file__).resolve().parent / "tools" / "sync_nonfull_resources.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("[switch]$QuickVerify", wrapper)
        self.assertIn('if ($QuickVerify) { $arguments += "--quick-verify" }', wrapper)

    def test_sync_wrapper_prefers_manifest_built_into_current_apk(self):
        wrapper = (
            Path(__file__).resolve().parent / "tools" / "sync_nonfull_resources.ps1"
        ).read_text(encoding="utf-8")
        built = wrapper.index('apk_build\\version-local-nonfull-built.json')
        fallback = wrapper.index('version-local-nonfull.json', built)
        self.assertLess(built, fallback)

    def test_apk_builder_exposes_official_update_inputs(self):
        builder = (Path(__file__).resolve().parent / "build_apk.ps1").read_text(encoding="utf-8")
        for parameter in (
            "[string]$ResourceRoot",
            "[string]$BuildDirectory",
            "[string]$OutputApk",
            "[string]$ManifestOutput",
            "[string]$PatchedLuaX64",
            "[string]$PatchedLuaX86",
        ):
            self.assertIn(parameter, builder)
        self.assertIn('"--manifest-output", "$ManifestOutput"', builder)
        self.assertIn("Specify both -PatchedLuaX64 and -PatchedLuaX86", builder)

    @patch("tools.sync_nonfull_resources.run_adb", return_value="")
    def test_full_device_inventory_uses_root_read_channel(self, run_adb):
        device_sync.remote_sizes(
            Path("adb.exe"),
            "emulator-test",
            "/storage/emulated/0/Android/data/com.example.game/files/Android",
        )
        self.assertEqual(run_adb.call_args.args[2:5], ("exec-out", "su", "-c"))

    @patch("tools.sync_nonfull_resources.run_adb")
    def test_streaming_md5_is_read_from_private_playerprefs(self, run_adb):
        run_adb.return_value = '<string name="StreamingMd5">ABCDEF0123456789ABCDEF0123456789</string>'
        value = device_sync.remote_streaming_md5(
            Path("adb.exe"), "emulator-test", "com.example.game"
        )
        self.assertEqual(value, "abcdef0123456789abcdef0123456789")
        self.assertEqual(run_adb.call_args.args[2:5], ("exec-out", "su", "-c"))

    def test_tar_batches_stay_below_transport_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name, size in (("one.ab", 6), ("two.ab", 6), ("three.ab", 4)):
                (root / name).write_bytes(b"x" * size)
            batches = device_sync.tar_batches(
                root, ["one.ab", "two.ab", "three.ab"], max_bytes=10
            )
            self.assertEqual(batches, [["one.ab"], ["two.ab", "three.ab"]])

    def test_default_tar_batch_limit_stays_below_mumu_stream_stall_size(self):
        default_limit = device_sync.tar_batches.__defaults__[0]
        self.assertLessEqual(default_limit, 500_000_000)
        self.assertLess(default_limit, 873_594_880)

    def test_tar_stream_contains_only_selected_resources(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a").mkdir()
            (root / "a" / "one.ab").write_bytes(b"one")
            (root / "two.ab").write_bytes(b"two")
            stream = io.BytesIO()
            device_sync.write_tar_stream(stream, root, ["a/one.ab", "two.ab"])
            stream.seek(0)
            with tarfile.open(fileobj=stream, mode="r:") as archive:
                self.assertEqual(archive.getnames(), ["a/one.ab", "two.ab"])
                self.assertEqual(archive.extractfile("a/one.ab").read(), b"one")
                self.assertEqual(archive.extractfile("two.ab").read(), b"two")

    @patch("tools.sync_nonfull_resources.subprocess.Popen")
    def test_tar_batch_uses_direct_adb_stream_without_host_temp_file(self, popen):
        stdin = io.BytesIO()
        stdout = io.BytesIO(b"")
        process = MagicMock()
        process.stdin = stdin
        process.stdout = stdout
        process.wait.return_value = 0
        popen.return_value = process
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "bundle.ab").write_bytes(b"bundle")
            device_sync.push_tar_batch(
                Path("adb.exe"),
                "emulator-test",
                root,
                "/storage/emulated/0/Android/data/com.example.game/files/Android",
                ["bundle.ab"],
            )
        command = popen.call_args.args[0]
        self.assertEqual(command[:5], ["adb.exe", "-s", "emulator-test", "exec-in", "su"])
        self.assertIn("tar -xf -", command[-1])
        self.assertNotIn("soultide-resource-repair.tar", command[-1])
        self.assertFalse(any("soultide-resource-repair.tar" in str(part) for part in command))

    @patch("tools.sync_nonfull_resources.subprocess.Popen")
    def test_tar_batch_reports_device_stream_failure(self, popen):
        process = MagicMock()
        process.stdin = io.BytesIO()
        process.stdout = io.BytesIO(b"tar: invalid archive")
        process.wait.return_value = 2
        popen.return_value = process
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "bundle.ab").write_bytes(b"bundle")
            with self.assertRaisesRegex(RuntimeError, "tar: invalid archive"):
                device_sync.push_tar_batch(
                    Path("adb.exe"),
                    "emulator-test",
                    root,
                    "/storage/emulated/0/Android/data/com.example.game/files/Android",
                    ["bundle.ab"],
                )

    @patch("tools.sync_nonfull_resources.subprocess.Popen")
    def test_tar_batch_limits_large_device_error_output_to_tail(self, popen):
        process = MagicMock()
        process.stdin = io.BytesIO()
        process.stdout = io.BytesIO(b"prefix-marker\n" + b"x" * 1200000 + b"\ntail-marker")
        process.wait.return_value = 2
        popen.return_value = process
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "bundle.ab").write_bytes(b"bundle")
            with self.assertRaises(RuntimeError) as raised:
                device_sync.push_tar_batch(
                    Path("adb.exe"),
                    "emulator-test",
                    root,
                    "/storage/emulated/0/Android/data/com.example.game/files/Android",
                    ["bundle.ab"],
                )
        message = str(raised.exception)
        self.assertNotIn("prefix-marker", message)
        self.assertIn("tail-marker", message)
        self.assertLess(len(message), 1100000)


if __name__ == "__main__":
    unittest.main()
