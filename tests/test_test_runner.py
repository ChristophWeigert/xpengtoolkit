"""Optional module-level skips are distinct from actual child-script failures."""
from contextlib import redirect_stdout
import importlib.util
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

spec = importlib.util.spec_from_file_location("isolated_runner", Path(__file__).resolve().parents[1] / "tools/run_tests.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def run_scripts(self, scripts):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            for name, source in scripts.items():
                (root / "tests" / name).write_text(source, encoding="utf-8")
            before = runner.ROOT
            try:
                runner.ROOT = root
                output = StringIO()
                with redirect_stdout(output):
                    result = runner.main()
                return result, output.getvalue()
            finally:
                runner.ROOT = before

    def test_optional_dependency_skip_does_not_fail_the_suite(self):
        result, output = self.run_scripts({
            "test_ok.py": "assert 2 + 2 == 4\n",
            "test_optional.py": "import unittest\nraise unittest.SkipTest('optional dependency absent')\n",
        })
        self.assertFalse(result)
        self.assertIn("SKIP test_optional.py", output)
        self.assertIn("1/2 test scripts passed; 1 skipped", output)

    def test_real_failure_is_not_suppressed(self):
        result, output = self.run_scripts({"test_bad.py": "raise AssertionError('regression')\n"})
        self.assertTrue(result)
        self.assertIn("FAIL test_bad.py", output)
        self.assertIn("regression", output)


if __name__ == "__main__":
    unittest.main()
