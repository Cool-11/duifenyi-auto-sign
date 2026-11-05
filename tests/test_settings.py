"""设置层的一致性测试：前端表单、可持久化字段与取值区间必须互相对齐。"""

from __future__ import annotations

import pathlib
import re
import unittest

from duifene.settings import PERSISTABLE_FIELDS, SETTING_LIMITS, Settings

APP_JS = pathlib.Path(__file__).resolve().parents[1] / "web" / "app.js"


class SettingsConsistencyTest(unittest.TestCase):
    """三处定义漂移会导致「保存设置失败」，这里把它们钉死。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ui_keys = re.findall(r'key:\s*"([a-z_]+)"', APP_JS.read_text("utf-8"))

    def test_ui_has_fields(self) -> None:
        self.assertTrue(self.ui_keys, "前端设置字段解析失败")

    def test_ui_keys_are_persistable(self) -> None:
        unknown = [key for key in self.ui_keys if key not in PERSISTABLE_FIELDS]
        self.assertEqual(unknown, [], f"界面存在不可持久化的字段：{unknown}")

    def test_persistable_keys_are_editable(self) -> None:
        missing = [key for key in PERSISTABLE_FIELDS if key not in self.ui_keys]
        self.assertEqual(missing, [], f"可持久化字段未在界面暴露：{missing}")

    def test_limits_target_known_fields(self) -> None:
        unknown = [key for key in SETTING_LIMITS if key not in PERSISTABLE_FIELDS]
        self.assertEqual(unknown, [], f"区间定义指向未知字段：{unknown}")


class ApplyRoundTripTest(unittest.TestCase):
    """门禁与兜底参数应可正常校验并写回。"""

    def test_apply_accepts_new_fields(self) -> None:
        settings = Settings()
        error = settings.apply(
            {
                "headcount_ratio": "0.35",
                "headcount_deadline_s": "30",
                "sign_delay_s": "8",
            }
        )
        self.assertEqual(error, "")
        self.assertAlmostEqual(settings.headcount_ratio, 0.35)
        self.assertAlmostEqual(settings.headcount_deadline_s, 30.0)
        self.assertAlmostEqual(settings.sign_delay_s, 8.0)

    def test_apply_rejects_out_of_range(self) -> None:
        settings = Settings()
        error = settings.apply({"headcount_ratio": "1.5"})
        self.assertNotEqual(error, "")
        self.assertAlmostEqual(settings.headcount_ratio, 0.0)  # 失败不落地

    def test_defaults_favour_reliability(self) -> None:
        """默认不等人（占比门槛关闭），避免「等到签到结束都没签上」。"""
        settings = Settings()
        self.assertEqual(settings.headcount_ratio, 0.0)
        self.assertLessEqual(settings.error_backoff_max_s, 15.0)
        self.assertLessEqual(settings.headcount_deadline_s, 20.0)

    def test_apply_rejects_unknown_field(self) -> None:
        settings = Settings()
        self.assertNotEqual(settings.apply({"qr_max_rounds": "5"}), "")

    def test_apply_rejects_inverted_poll_range(self) -> None:
        settings = Settings()
        error = settings.apply({"poll_min_s": "9", "poll_max_s": "4"})
        self.assertNotEqual(error, "")

    def test_proxy_switches_tls_verification(self) -> None:
        settings = Settings()
        self.assertEqual(settings.apply({"proxy_url": "127.0.0.1:8080"}), "")
        self.assertEqual(settings.proxy_url, "http://127.0.0.1:8080")
        self.assertFalse(settings.verify_tls)
        self.assertEqual(settings.apply({"proxy_url": ""}), "")
        self.assertTrue(settings.verify_tls)

    def test_persistable_values_are_stable(self) -> None:
        values = Settings().persistable_values()
        self.assertIn("verify_tls", values)
        self.assertIn("headcount_deadline_s", values)


class MonitorWindowTest(unittest.TestCase):
    """监听时间窗默认全天。"""

    def test_default_is_full_day(self) -> None:
        settings = Settings()
        self.assertEqual(settings.monitor_start_hour, 0)
        self.assertEqual(settings.monitor_end_hour, 24)

    def test_apply_full_day(self) -> None:
        settings = Settings()
        settings.apply({"monitor_start_hour": "0", "monitor_end_hour": "24"})
        self.assertEqual((settings.monitor_start_hour, settings.monitor_end_hour), (0, 24))


if __name__ == "__main__":
    unittest.main()
