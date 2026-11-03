"""二维码解码模块与依赖声明的回归测试。

内嵌一张真实二维码图（内容是拿到 state 令牌的完整 URL），使「解码 → 提取令牌」
这一段可以离线端到端验证，不依赖外部服务或生成库。
"""

from __future__ import annotations

import pathlib
import unittest

from duifene import qrdecode
from duifene.api import DuifeneApi

ROOT = pathlib.Path(__file__).resolve().parents[1]

# 内容：https://www.duifene.com/_CheckIn/MB/QrCodeCheckOK.aspx?state=FIXTURE123
QR_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAKQAAACkAQAAAAAxzrjsAAABpElEQVR4nM1XyY0kMQyjG/2nMqj8w6oM"
    "qAi4oKrns7OvHT3GaBhtPwiBh6U6xvfVr39cAr/j9pxTpxpdBVTluIAL0ybAOcjMUT/HfQOHMI5YuHNzuMfO"
    "aVbd94Wj/0T4e71nF8Am7gs155V6HVQ0u9jUHH+Oe3y+RHtWA7w3cAGgoGq4hpGVXMCEYMp0CielDZ/BYdS2"
    "mJ8hyyu4NkgatgKs/FnhgZO41GlQSOErPCjAoJg8R7clfp2agzbwwJJuJvJAeKwxlOzwAFGIyTB76NBOLs7k"
    "rQEfQl0beUN00yiXeKTcJd3IeXcEKWSQS7jWGHdyFwG1lDc6pZKk0oKWdENKdYpN1OJlLeVYTiwcxER43qAl"
    "3ZyUTTTiiyXdkGo//gpoiN7om26wXTpEJdJorswl0tPe8u4oJW/5DHHD+I2hhFv9AsNwxEvyIt1SH3LiAIVb"
    "MxPajh8w84O+mNh5H14fyzK+KHbwtTOngulsDAVP5Rs8vGc/rjt74wIu3Fv1Ik97pIt4K7q9nokHl5JgheWV"
    "ORWPHzxzyWdc2+qb39Yv/s76A55lRas/1tG+AAAAAElFTkSuQmCC"
)


class DeclarationTest(unittest.TestCase):
    """依赖声明必须随代码一起存在，否则打包后二维码不可用。"""

    def test_requirements_declares_deps(self) -> None:
        text = (ROOT / "requirements.txt").read_text("utf-8").lower()
        self.assertIn("pillow", text)
        self.assertIn("pyzbar", text)

    def test_build_spec_collects_native_libs(self) -> None:
        spec = (ROOT / "build.spec").read_text("utf-8")
        self.assertIn("collect_dynamic_libs", spec)
        self.assertIn("pyzbar", spec)


class AvailableTest(unittest.TestCase):
    """available() 必须只返回布尔，绝不抛异常。"""

    def test_returns_bool(self) -> None:
        self.assertIsInstance(qrdecode.available(), bool)


class DecodeTest(unittest.TestCase):
    """端到端：PNG(base64) → 文本 → 令牌。"""

    def test_invalid_base64_returns_none(self) -> None:
        self.assertIsNone(qrdecode.decode_image_base64("not-a-base64!!"))

    def test_broken_png_returns_none(self) -> None:
        self.assertIsNone(qrdecode.decode_image_base64("AAAA"))

    @unittest.skipUnless(qrdecode.available(), "本机缺少 Pillow/pyzbar")
    def test_decode_fixture(self) -> None:
        text = qrdecode.decode_image_base64(QR_PNG_BASE64)
        self.assertIsNotNone(text)
        self.assertIn("state=FIXTURE123", text or "")

    @unittest.skipUnless(qrdecode.available(), "本机缺少 Pillow/pyzbar")
    def test_decode_then_extract_state(self) -> None:
        text = qrdecode.decode_image_base64(QR_PNG_BASE64)
        api = DuifeneApi.__new__(DuifeneApi)  # 只用纯解析方法，无需会话
        self.assertEqual(api._state_from_qr_content(text or ""), "FIXTURE123")


if __name__ == "__main__":
    unittest.main()
