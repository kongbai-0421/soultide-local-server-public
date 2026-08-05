import unittest
from pathlib import Path
from unittest import mock

from tools.ensure_mumu_routes import HOST_NAMES, install_routes, parse_route_status
from tools.mumu_route_watchdog import env_flag


class MumuRouteStatusTests(unittest.TestCase):
    def setUp(self):
        self.server_ip = "192.168.1.136"
        self.hosts = "127.0.0.1 localhost\n" + "\n".join(
            f"{self.server_ip} {host}" for host in HOST_NAMES
        )
        self.chain = "\n".join(
            (
                "-N SOULTIDE_LOCAL",
                f"-A SOULTIDE_LOCAL -p tcp --dport 80 -j DNAT --to-destination {self.server_ip}:8081",
                f"-A SOULTIDE_LOCAL -p tcp --dport 8000 -j DNAT --to-destination {self.server_ip}:8000",
            )
        )
        self.output = "-P OUTPUT ACCEPT\n-A OUTPUT -j SOULTIDE_LOCAL"

    def status(self, hosts=None, chain=None, output=None, strict=False, filter_output=""):
        return parse_route_status(
            "127.0.0.1:16416",
            "android-id",
            "boot-id",
            ("127.0.0.1:16416", "emulator-5556"),
            self.server_ip,
            self.hosts if hosts is None else hosts,
            self.chain if chain is None else chain,
            self.output if output is None else output,
            filter_output,
            strict,
        )

    def test_complete_hosts_and_dnat_are_healthy(self):
        status = self.status()
        self.assertTrue(status.healthy)
        self.assertTrue(status.strict_ok)

    def test_missing_host_mapping_is_unhealthy(self):
        status = self.status(hosts="127.0.0.1 localhost\n")
        self.assertFalse(status.hosts_ok)
        self.assertFalse(status.healthy)

    def test_wrong_server_ip_is_unhealthy(self):
        status = self.status(chain=self.chain.replace(self.server_ip, "192.168.1.99"))
        self.assertFalse(status.dnat_http_ok)
        self.assertFalse(status.dnat_sdk_ok)

    def test_missing_output_jump_is_unhealthy(self):
        status = self.status(output="-P OUTPUT ACCEPT")
        self.assertFalse(status.output_jump_ok)
        self.assertFalse(status.healthy)

    def test_strict_mode_requires_offline_jump(self):
        missing = self.status(strict=True)
        present = self.status(strict=True, filter_output="-A OUTPUT -j SOULTIDE_OFFLINE")
        self.assertFalse(missing.strict_ok)
        self.assertTrue(present.strict_ok)


class MumuRouteWatchdogTests(unittest.TestCase):
    def test_strict_environment_flag_accepts_only_explicit_true_values(self):
        for value in ("1", "true", "TRUE", "yes", "on"):
            with self.subTest(value=value), mock.patch.dict(
                "os.environ", {"SOULTIDE_ROUTE_STRICT": value}
            ):
                self.assertTrue(env_flag("SOULTIDE_ROUTE_STRICT"))
        for value in ("0", "false", "off", ""):
            with self.subTest(value=value), mock.patch.dict(
                "os.environ", {"SOULTIDE_ROUTE_STRICT": value}
            ):
                self.assertFalse(env_flag("SOULTIDE_ROUTE_STRICT"))


class MumuRouteInstallTests(unittest.TestCase):
    @mock.patch("tools.ensure_mumu_routes.run_adb")
    @mock.patch("tools.ensure_mumu_routes.root_shell")
    def test_hosts_temporary_file_is_created_through_root_exec_out(
        self,
        root_shell_mock,
        run_adb_mock,
    ):
        root_shell_mock.return_value = ""

        install_routes(
            Path("adb.exe"),
            "127.0.0.1:16416",
            "192.168.1.136",
            strict=False,
        )

        run_adb_mock.assert_not_called()
        first_command = root_shell_mock.call_args_list[0].args[2]
        self.assertIn("base64 -d > /data/local/tmp/soultide_hosts", first_command)
        self.assertIn("chmod 644 /data/local/tmp/soultide_hosts", first_command)
        self.assertNotIn("adb shell", first_command)


if __name__ == "__main__":
    unittest.main()
