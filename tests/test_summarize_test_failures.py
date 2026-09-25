import tempfile
from pathlib import Path
import unittest

from scripts.summarize_test_failures import main, summarize


class SummarizeTestFailuresTests(unittest.TestCase):
    def test_emits_only_allowlisted_bounded_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tests.log"
            path.write_text(
                "ERROR: test_bad\x1b[31m\n"
                "FAIL: test_ok\x1b[31m\r\n"
                "::error file=secret.py::do not emit\n"
                + "ERROR: " + "x" * 10000 + "\n"
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

    def test_unreadable_log_returns_error_without_traceback(self):
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
