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
                "OSError: " + "/" + "private/path/should-not-emit\n"
                "PermissionError: password=should-not-emit\n"
                "passwordError: password=should-not-emit\n"
                "secret.pyError: " + "/" + "private/path/should-not-emit\n"
                "VeryLong" + "x" * 200 + "Error: too long\n"
                "  File \"" + "/" + "repo/addin/STEVEUpdater/update_core.py\", line 123, in _sync_directory\n"
                "  File \"" + "C:" + "\\" + "secret" + "\\" + "password.py\", line 9999999, in bad\n"
                "  File \"" + "/" + "repo/secret.py\", line 7, in leaked\x1b\n"
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
                + "  File " + "\\\"" + "/" + "repo/update_core.py" + "\\\", line 123, in _sync_directory\n",
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
            quote = chr(34)
            slash = chr(92)
            posix_frame = "  File " + quote + slash + "repo/update_core.py" + quote + ", line 12, in _sync_directory\n"
            windows_frame = "  File " + quote + "C:" + slash + "repo" + slash + "live_update.py" + quote + ", line 34, in swap\n"
            unc_frame = "  File " + quote + slash + slash + "server" + slash + "share" + slash + "STEVEUpdater.py" + quote + ", line 56, in run\n"
            path.write_text(posix_frame + windows_frame + unc_frame, encoding="utf-8")
            output = "\n".join(summarize(path))
        self.assertIn("TEST_FRAME: file=update_core.py line=12", output)
        self.assertIn("TEST_FRAME: file=live_update.py line=34", output)
        self.assertIn("TEST_FRAME: file=STEVEUpdater.py line=56", output)

        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "missing.log"
            self.assertEqual(main(["summarize_test_failures.py", str(missing)]), 3)

    def test_correlates_allowlisted_details_without_emitting_messages_or_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tests.log"
            quote = chr(34)
            slash = chr(92)
            posix_path = slash + "repo" + slash + "update_core.py"
            windows_path = "C:" + slash + "repo" + slash + "update_core.py"
            hostile_path = "C:" + slash + "secret" + slash + "password.py"
            path.write_text(
                "ERROR: test_one (test_live_update.LiveUpdateTests.test_one)\n"
                "  File " + quote + windows_path + quote + ", line 70, in _sync_tree\n"
                "OSError: secret message " + slash + "home" + slash + "private " + hostile_path + "\n"
                "ERROR: test_two (test_live_update.LiveUpdateTests.test_two)\n"
                "  File " + quote + posix_path + quote + ", line 1087, in apply\n"
                "AssertionError: workflow-command ::error file=secret.py::leak\n"
                "Ran 2 tests in 0.01s\n"
                "FAILED (errors=1, failures=1)\n",
                encoding="utf-8",
            )
            output = "\n".join(summarize(path))
        self.assertIn(
            "TEST_DETAIL: kind=ERROR name=test_live_update.LiveUpdateTests.test_one "
            "exception=OSError frames=update_core.py:70",
            output,
        )
        self.assertIn(
            "TEST_DETAIL: kind=ERROR name=test_live_update.LiveUpdateTests.test_two "
            "exception=AssertionError frames=update_core.py:1087",
            output,
        )
        self.assertNotIn("password.py", output)
        self.assertNotIn("/home/private", output)
        self.assertNotIn("secret message", output)
        self.assertNotIn("::error", output)
        self.assertLessEqual(len(output), 4096)

    def test_detail_output_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tests.log"
            records = []
            for index in range(100):
                records.append(
                    f"ERROR: test_{index} (test_live_update.LiveUpdateTests.test_{index})\n"
                    "OSError: hidden\n"
                    "  File \"/repo/update_core.py\", line 70, in sync\n"
                )
            path.write_text("".join(records), encoding="utf-8")
            output = summarize(path)
        details = [line for line in output if line.startswith("TEST_DETAIL:")]
        self.assertLessEqual(len(details), summary_module._MAX_DETAILS)

    def test_failure_details_prioritize_failures_in_mixed_native_log(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tests.log"
            records = []
            for index in range(15):
                records.extend([
                    f"ERROR: error_{index} (test_live_update.LiveUpdateTests.error_{index})\n",
                    "OSError: hidden\n",
                    "  File \"/repo/update_core.py\", line 70, in sync\n",
                ])
            for index in range(5):
                records.extend([
                    f"FAIL: failure_{index} (test_live_update.LiveUpdateTests.failure_{index})\n",
                    "AssertionError: hidden\n",
                    "  File \"/repo/test_live_update.py\", line 1294, in assert_state\n",
                ])
            path.write_text("".join(records), encoding="utf-8")
            output = summarize(path)
        details = [line for line in output if line.startswith("TEST_DETAIL:")]
        fail_details = [line for line in details if "name=test_live_update.LiveUpdateTests.failure_" in line]
        self.assertEqual(len(fail_details), 5)
        self.assertTrue(all("exception=AssertionError" in line for line in fail_details))
        self.assertLessEqual(len(details), summary_module._MAX_DETAILS)

    def test_cli_prioritizes_fail_details_over_long_failure_headings(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tests.log"
            records = []
            for index in range(40):
                name = "test_" + ("x" * 110) + str(index)
                records.extend([
                    f"ERROR: {name} (test_live_update.LiveUpdateTests.{name})\n",
                    "OSError: hidden\n",
                    "  File \"/repo/update_core.py\", line 70, in sync\n",
                ])
            records.extend([
                "FAIL: test_failure (test_live_update.LiveUpdateTests.test_failure)\n",
                "AssertionError: hidden\n",
                "  File \"/repo/test_live_update.py\", line 1294, in assert_state\n",
            ])
            path.write_text("".join(records), encoding="utf-8")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(["summarize_test_failures.py", str(path)]), 0)
        text = stdout.getvalue()
        self.assertLessEqual(len(text), summary_module._MAX_OUTPUT)
        self.assertIn("TEST_DETAIL: kind=FAIL name=test_live_update.LiveUpdateTests.test_failure", text)
        self.assertTrue(all(not line.startswith("TEST_FAILURE:") or "..." not in line
                            for line in text.splitlines()))

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

    def test_cli_keeps_complete_fail_details_within_output_cap(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tests.log"
            records = []
            for index in range(15):
                records.extend([
                    f"ERROR: error_{index} (test_live_update.LiveUpdateTests.error_{index})\n",
                    "OSError: hidden\n",
                    "  File \"/repo/update_core.py\", line 70, in sync\n",
                ])
            for index in range(5):
                records.extend([
                    f"FAIL: failure_{index} (test_live_update.LiveUpdateTests.failure_{index})\n",
                    "AssertionError: hidden\n",
                    "  File \"/repo/test_live_update.py\", line 1294, in assert_state\n",
                ])
            path.write_text("".join(records), encoding="utf-8")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(["summarize_test_failures.py", str(path)]), 0)
        text = stdout.getvalue()
        self.assertLessEqual(len(text), summary_module._MAX_OUTPUT)
        self.assertTrue(text.endswith("\n"))
        detail_lines = [line for line in text.splitlines() if line.startswith("TEST_DETAIL:")]
        fail_details = [line for line in detail_lines if "kind=FAIL" in line]
        self.assertEqual(len(fail_details), 5)
        self.assertTrue(all(line.startswith("TEST_DETAIL: ") for line in detail_lines))


if __name__ == "__main__":
    unittest.main()
