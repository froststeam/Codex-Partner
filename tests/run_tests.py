from __future__ import annotations

import contextlib
import os
from pathlib import Path
import sys
import unittest


def main() -> int:
    output = Path("test-results.txt")
    with output.open("w", encoding="utf-8") as stream:
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            result = unittest.TextTestRunner(verbosity=2).run(
                unittest.defaultTestLoader.discover("tests")
            )
    summary = Path(os.environ["GITHUB_STEP_SUMMARY"]) if os.getenv("GITHUB_STEP_SUMMARY") else None
    if summary:
        summary.write_text(output.read_text(encoding="utf-8")[-12000:], encoding="utf-8")
    print(output.read_text(encoding="utf-8"), end="")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
