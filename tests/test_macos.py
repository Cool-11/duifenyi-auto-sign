"""macOS 桌面适配测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from duifene.config import IniStore, resolve_ini_path
from duifene.credentials import CredentialStore, SavedCredentials


class MacOSPathsTest(unittest.TestCase):
    @mock.patch("duifene.config.sys.platform", "darwin")
    def test_config_uses_application_support(self) -> None:
        path = resolve_ini_path("duifenyi.ini")
        self.assertIn(os.path.join("Library", "Application Support", "duifenyi-auto-sign"), path)


class MacOSKeychainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = IniStore(os.path.join(self.temp.name, "duifenyi.ini"))
        self.credentials = CredentialStore(self.store)

    @mock.patch("duifene.credentials._save_macos_keychain")
    @mock.patch("duifene.credentials.sys.platform", "darwin")
    def test_save_uses_keychain_not_plaintext(self, save: mock.Mock) -> None:
        self.credentials.save("student", "secret")
        save.assert_called_once_with("student", "secret")
        values = self.store.load_account()
        self.assertEqual(values["password_scheme"], "keychain")
        self.assertNotIn("secret", values["password_data"])

    @mock.patch("duifene.credentials._load_macos_keychain", return_value="secret")
    def test_load_reads_keychain(self, load: mock.Mock) -> None:
        self.store.save_account({
            "account": "student",
            "password_scheme": "keychain",
            "password_data": "stored-in-login-keychain",
        })
        self.assertEqual(self.credentials.load(), SavedCredentials("student", "secret"))
        load.assert_called_once_with("student")
