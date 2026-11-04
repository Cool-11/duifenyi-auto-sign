"""Bridge（界面门面）的离线测试：配置读写、手动签到登记、状态输出。

不触发 ``bootstrap()``，因此不会发起任何网络请求。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from duifene.bridge import Bridge
from duifene.settings import Settings


class BridgeTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ini = str(Path(self._tmp.name) / "duifenyi.ini")
        self.bridge = Bridge(Settings(ini_filename=self.ini))

    def tearDown(self) -> None:
        self._tmp.cleanup()


class SettingsTest(BridgeTestBase):
    def test_get_settings_returns_editable_fields(self) -> None:
        result = self.bridge.get_settings()
        self.assertTrue(result["ok"])
        self.assertIn("poll_min_s", result["values"])
        self.assertIn("poll_min_s", result["limits"])
        self.assertNotIn("verify_tls", result["values"])

    def test_update_settings_persists_to_ini(self) -> None:
        result = self.bridge.update_settings({"poll_min_s": "4", "poll_max_s": "8"})
        self.assertTrue(result["ok"], result)
        saved = Path(self.ini).read_text("utf-8")
        self.assertIn("poll_min_s = 4.0", saved)

    def test_update_settings_rejects_invalid_patch(self) -> None:
        result = self.bridge.update_settings({"poll_min_s": "9", "poll_max_s": "4"})
        self.assertFalse(result["ok"])
        self.assertIn("不能大于", result["message"])

    def test_settings_survive_reload(self) -> None:
        self.bridge.update_settings({"headcount_ratio": "0.5"})
        again = Bridge(Settings(ini_filename=self.ini))
        self.assertAlmostEqual(again.get_settings()["values"]["headcount_ratio"], 0.5)


class ManualCodeTest(BridgeTestBase):
    def test_manual_code_is_validated(self) -> None:
        self.assertFalse(self.bridge.submit_manual_code("123")["ok"])
        self.assertTrue(self.bridge.submit_manual_code("12 34")["ok"])

    def test_manual_code_is_consumed_once(self) -> None:
        self.bridge.submit_manual_code("5678")
        self.assertEqual(self.bridge._take_manual_code(), "5678")
        self.assertIsNone(self.bridge._take_manual_code())


class StateTest(BridgeTestBase):
    def test_state_defaults(self) -> None:
        state = self.bridge.get_state()
        self.assertFalse(state["running"])
        self.assertFalse(state["logged_in"])
        self.assertEqual(state["last_tick_ts"], 0.0)

    def test_logs_are_drained_incrementally(self) -> None:
        self.bridge._log("第一条")
        self.bridge._log("第二条")
        first = self.bridge.drain_logs(0)
        self.assertEqual(len(first["items"]), 2)
        second = self.bridge.drain_logs(first["next_id"])
        self.assertEqual(second["items"], [])

    def test_log_levels_are_inferred(self) -> None:
        self.bridge._log("签到成功")
        self.bridge._log("签到失败：签到码错误")
        items = self.bridge.drain_logs(0)["items"]
        self.assertEqual(items[0]["level"], "success")
        self.assertEqual(items[1]["level"], "error")

    def test_start_without_login_is_rejected(self) -> None:
        result = self.bridge.start_watching()
        self.assertFalse(result["ok"])
        self.assertIn("登录", result["message"])


if __name__ == "__main__":
    unittest.main()
