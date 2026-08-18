import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_partner import platform_support


class PlatformSupportTests(unittest.TestCase):
    def test_windows_batch_commands_use_command_processor(self):
        with mock.patch.object(platform_support, "IS_WINDOWS", True), mock.patch.dict(
            os.environ, {"COMSPEC": r"C:\Windows\System32\cmd.exe"}
        ):
            command = platform_support.prepare_subprocess_command([r"C:\Users\me\npm.cmd", "--version"])
        self.assertEqual(r"C:\Windows\System32\cmd.exe", command[0])
        self.assertEqual(["/d", "/s", "/c"], command[1:4])
        self.assertIn("npm.cmd", command[4])

    def test_native_desktop_data_directories_do_not_use_package_tree(self):
        package_root = Path("package")
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(platform_support, "IS_WINDOWS", True), mock.patch.object(
                platform_support, "IS_MACOS", False
            ), mock.patch.dict(os.environ, {"LOCALAPPDATA": temporary}, clear=True):
                self.assertEqual(Path(temporary) / "CodexPartner", platform_support.default_data_dir(package_root))
            with mock.patch.object(platform_support, "IS_WINDOWS", False), mock.patch.object(
                platform_support, "IS_MACOS", True
            ), mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    Path.home() / "Library/Application Support/CodexPartner",
                    platform_support.default_data_dir(package_root),
                )

    def test_desktop_auth_defaults_to_local_only_mode(self):
        with mock.patch.object(platform_support, "SYSTEM", "Windows"):
            self.assertEqual("none", platform_support.default_auth_mode())
        with mock.patch.object(platform_support, "SYSTEM", "Darwin"):
            self.assertEqual("none", platform_support.default_auth_mode())
        with mock.patch.object(platform_support, "SYSTEM", "Linux"):
            self.assertEqual("ssh", platform_support.default_auth_mode())

    def test_unauthenticated_default_rejects_non_loopback_bind(self):
        platform_support.validate_bind_auth("127.0.0.1", "none", False)
        platform_support.validate_bind_auth("::1", "none", False)
        with self.assertRaisesRegex(RuntimeError, "loopback-only"):
            platform_support.validate_bind_auth("0.0.0.0", "none", False)

    def test_explicit_auth_choice_allows_non_loopback_bind(self):
        platform_support.validate_bind_auth("0.0.0.0", "none", True)
        platform_support.validate_bind_auth("0.0.0.0", "ssh", False)

    def test_readme_documents_native_macos_and_windows_commands(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
        self.assertIn("### macOS", readme)
        self.assertIn("### Windows", readme)
        self.assertIn("py -m codex_partner", readme)
        self.assertIn("PowerShell through ConPTY", readme)


if __name__ == "__main__":
    unittest.main()
