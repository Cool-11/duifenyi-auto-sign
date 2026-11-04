"""运行日志落盘（``duifene/runlog.py``）单元测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
import sys

from duifene.runlog import FileLog, default_log_path


class FileLogTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "logs", "run.log")

    def _read(self) -> str:
        with open(self.path, encoding="utf-8") as handle:
            return handle.read()

    def test_creates_missing_directory_and_appends(self) -> None:
        log = FileLog(self.path)
        log.write("info", "第一条")
        log.write("error", "登录失效，请重新登录账号")
        text = self._read()
        self.assertIn("[info] 第一条", text)
        self.assertIn("[error] 登录失效，请重新登录账号", text)
        self.assertEqual(len(text.strip().splitlines()), 2)

    def test_multiline_text_split_into_one_line_per_line(self) -> None:
        log = FileLog(self.path)
        log.write("info", "第一行\n\n第二行")
        lines = self._read().strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("第一行", lines[0])
        self.assertIn("第二行", lines[1])

    def test_rotation_keeps_previous_file(self) -> None:
        log = FileLog(self.path)
        log.write("info", "起始")
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write("x" * (2 * 1024 * 1024))
        log.write("info", "轮转后")
        self.assertTrue(os.path.exists(f"{self.path}.1"))
        self.assertIn("轮转后", self._read())

    def test_write_failure_is_silent(self) -> None:
        # 文件名与目录同名：makedirs 失败，写盘应被静默吞掉而不是抛异常
        blocker = os.path.join(self._tmp.name, "blocked")
        with open(blocker, "w", encoding="utf-8") as handle:
            handle.write("x")
        log = FileLog(os.path.join(blocker, "run.log"))
        log.write("info", "不会抛错")

    def test_default_log_path_under_logs_directory(self) -> None:
        path = default_log_path()
        if sys.platform == "darwin":
            self.assertTrue(path.endswith(os.path.join("duifenyi-auto-sign", "run.log")))
            self.assertIn(os.path.join("Library", "Logs"), path)
        else:
            self.assertTrue(path.endswith(os.path.join("logs", "run.log")))


if __name__ == "__main__":
    unittest.main()
