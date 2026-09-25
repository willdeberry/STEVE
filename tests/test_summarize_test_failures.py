import io
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
import unittest

from scripts import summarize_test_failures as summary_module
from scripts.summarize_test_failures import main, summarize


class SummarizeTestFailuresTests(unittest.TestCase):
    def test_emits_only_allowlisted_bounded_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tests.log"
            path.write_text(
                "ERROR: test_bad\x1b[31m\n"
                "FAIL: test_ok\x1b[31m\r\n"
                "::error file=secret.py::do not emit\n"
                "OSError: /private/path/should-not-emit\n"
                "PermissionError: password=should-not-emit\n"
                "passwordError: password=should-not-emit\n"
                "secret.pyError: /private/path/should-not-emit\n"
                "VeryLong" + "x" * 200 + "Error: too long\n"
                "  File \"/repo/addin/STEVEUpdater/update_core.py\", line 123, in _sync_directory\n"
                "  File \"C:\\secret\\password.py\", line 9999999, in bad\n"
                "  File \"/repo/secret.py\", line 7, in leaked\x1b\n"
                + "Ran 12 tests in 1.2s\n"
                + "FAILED (errors=1, credential=[REDACTED])\n",
                encoding="utf-8",
            )
            output = "\n".join(summarize(path))
        self.assertLessEqual(len(output), 4096)
        self.assertIn("TEST_SUMMARY: ran=12 failures=0", output)
        self.assertNotIn("::error", output)
        self.assertNotIn("secret.py", output)
        self.assertNotIn("\x1b", output)
        self.assertNotIn("test_bad", output)
        self.assertNotIn("test_ok", output)
        self.assertIn("TEST_EXCEPTION: type=OSError", output)
        self.assertIn("TEST_EXCEPTION: type=PermissionError", output)
        self.assertNotIn("TEST_EXCEPTION: type=passwordError", output)
        self.assertNotIn("secret.pyError", output)
        self.assertNotIn("VeryLong", output)
        self.assertIn("TEST_FRAME: file=update_core.py line=123", output)
        self.assertNotIn("password.py", output)
        self.assertNotIn("secret.py", output)

    def test_cli_output_includes_newline_within_total_cap(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tests.log"
            path.write_text(
                "Ran 1 tests in 0.01s\n"
                + "ERROR: test_failure (test_live_update.LiveUpdateTests.test_failure)\n"
                + "OSError: details\n"
                + "  File \\\"/repo/update_core.py\\\", line 123, in _sync_directory\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(["summarize_test_failures.py", str(path)]), 0)
        self.assertLessEqual(len(stdout.getvalue()), summary_module._MAX_OUTPUT)
        self.assertTrue(stdout.getvalue().endswith("\n"))

    def test_valid_posix_windows_and_unc_frames_are_emitted(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tests.log"
            path.write_text(
                "  File \"/repo/update_core.py\", line 12, in _sync_directory\n"
                "  File \"C:\\\\repo\\\\live_update.py\", line 34, in swap\n"
                "  File \"\\\\server\\share\\STEVEUpdater.py\", line 56, in run\n",
                encoding="utf-8",
            )
            output = "\n".join(summarize(path))
        self.assertIn("TEST_FRAME: file=update_core.py line=12", output)
        self.assertIn("TEST_FRAME: file=live_update.py line=34", output)
        self.assertIn("TEST_FRAME: file=STEVEUpdater.py line=56", output)

        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "missing.log"
            self.assertEqual(main(["summarize_test_failures.py", str(missing)]), 3)

    def test_valid_failure_identifiers_are_bounded_and_structured(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tests.log"
            path.write_text(
                "ERROR: test_failure (test_module.TestCase.test_failure)\n"
                "FAIL: test_other (test_module.TestCase.test_other)\n"
                "Ran 2 tests in 0.01s\n"
                "FAILED (failures=1, errors=1)\n",
                encoding="utf-8",
            )
            output = summarize(path)
        self.assertEqual(output[0], "TEST_SUMMARY: ran=2 failures=2")
        self.assertIn("TEST_FAILURE: kind=ERROR name=test_module.TestCase.test_failure", output)
        self.assertIn("TEST_FAILURE: kind=FAIL name=test_module.TestCase.test_other", output)
        self.assertIn("TEST_COUNTS: failures=1, errors=1", output)


if __name__ == "__main__":
    unittest.main()
