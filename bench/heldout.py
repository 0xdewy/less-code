"""Post-search regression probes derived from the first model experiment.

This file stays outside cloned target repositories and model prompts.
"""

import json
import re
import subprocess
import sys
from pathlib import Path


def check_table(Table):
    inner = Table([[1]], headers=["inner"])
    outer = Table([[inner]], headers=["outer"])
    assert outer.to_html(orientation="horizontal", max_depth=1).count("<table") == 1


def check_fastq(source):
    program = """
const vm = require('node:vm');
const assert = require('node:assert/strict');
const fn = vm.runInNewContext(process.argv[1] + '; benchFastQPromise', {
  qPromise: {push: value => Promise.resolve(value)}
});
let observed;
fn((...args) => { observed = args; });
setImmediate(() => assert.deepEqual(observed, []));
"""
    return subprocess.run(
        ["node", "-e", program, source],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def js_function(source, name):
    """Extract one top-level function without importing the reducer package.

    The probe runs from a freshly cloned JavaScript project, where the host
    package is not necessarily importable. Top-level declarations delimit the
    benchmark functions cleanly; the returned slice includes the declaration
    and stops before the next one (or end of file).
    """
    match = re.search(rf"(?m)^function\s+{re.escape(name)}\s*\(", source)
    if match is None:
        raise ValueError(f"function not found: {name}")
    following = re.search(r"(?m)^function\s+\w+\s*\(", source[match.end() :])
    end = match.end() + following.start() if following is not None else len(source)
    return source[match.start() : end].rstrip()


if __name__ == "__main__":
    if sys.argv[1] == "boltons":
        sys.path.insert(0, str(Path.cwd()))
        from boltons.tableutils import Table

        check_table(Table)
    elif sys.argv[1] == "fastq":
        source = Path("bench.js").read_text()
        result = check_fastq(js_function(source, "benchFastQPromise"))
        if result.returncode:
            print(result.stderr, file=sys.stderr)
            raise SystemExit(result.returncode)
    else:
        raise SystemExit("expected boltons or fastq")
    print(json.dumps({"regression_probe": sys.argv[1], "passed": True}))
