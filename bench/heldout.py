"""Post-search regression probes derived from the first model experiment.

This file stays outside cloned target repositories and model prompts.
"""

import json
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


if __name__ == "__main__":
    if sys.argv[1] == "boltons":
        sys.path.insert(0, str(Path.cwd()))
        from boltons.tableutils import Table

        check_table(Table)
    elif sys.argv[1] == "fastq":
        from less_code.ml_shrink import extract_symbols

        source = Path("bench.js").read_text()
        symbol = next(
            s
            for s in extract_symbols(source, "javascript")
            if s["name"] == "benchFastQPromise"
        )
        result = check_fastq(symbol["text"])
        if result.returncode:
            print(result.stderr, file=sys.stderr)
            raise SystemExit(result.returncode)
    else:
        raise SystemExit("expected boltons or fastq")
    print(json.dumps({"regression_probe": sys.argv[1], "passed": True}))
