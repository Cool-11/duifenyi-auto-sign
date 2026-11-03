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
    """新增的三个二维码/取码参数应可正常校验并写回。"""

    def test_apply_accepts_new_fields(self) -> None:
        settings = Settings()
        error = settings.apply(
            {
                "qr_retry_interval_s": "10",
                "qr_max_rounds": "6",
                "answer_wait_rounds": "20",
            }
        )
        self.assertEqual(error, "")
        self.assertEqual(settings.qr_retry_interval_s, 10.0)
        self.assertEqual(settings.qr_max_rounds, 6)
        self.assertEqual(settings.answer_wait_rounds, 20)

    def test_apply_rejects_out_of_range(self) -> None:
        settings = Settings()
        error = settings.apply({"qr_max_rounds": "0"})
        self.assertNotEqual(error, "")
        self.assertEqual(settings.qr_max_rounds, 8)  # 失败不落地

    def test_persistable_values_contain_new_fields(self) -> None:
        values = Settings().persistable_values()
        for key in ("qr_retry_interval_s", "qr_max_rounds", "answer_wait_rounds"):
            self.assertIn(key, values)


if __name__ == "__main__":
    unittest.main()
