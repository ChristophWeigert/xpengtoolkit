#!/usr/bin/env python3
"""Run standalone regression scripts in isolated processes; report optional skips."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
_SKIP = 77
_RUN_SCRIPT = """
from pathlib import Path
import runpy
import sys
import unittest
script = sys.argv[1]
sys.path.insert(0, str(Path(script).parent))
sys.argv = [script]
try:
    runpy.run_path(script, run_name="__main__")
except unittest.SkipTest as err:
    print(f"SKIP: {err}", flush=True)
    sys.exit(77)
"""


def main() -> int:
    failures = []
    skipped = []
    files = sorted((ROOT / "tests").glob("test_*.py"))
    for path in files:
        try:
            result = subprocess.run(
                [sys.executable, "-c", _RUN_SCRIPT, str(path)], cwd=ROOT,
                capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            failures.append(path.name)
            print(f"FAIL {path.name}: timed out after 120 seconds", flush=True)
            continue
        if result.returncode == _SKIP:
            skipped.append(path.name)
            print(f"SKIP {path.name}: {result.stdout.strip()}", flush=True)
            continue
        print(f"{'PASS' if result.returncode == 0 else 'FAIL'} {path.name}", flush=True)
        if result.returncode:
            failures.append(path.name)
            print(result.stdout + result.stderr, flush=True)
    passed = len(files) - len(failures) - len(skipped)
    print(f"{passed}/{len(files)} test scripts passed; {len(skipped)} skipped")
    return bool(failures)


if __name__ == "__main__":
    sys.exit(main())
