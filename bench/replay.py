"""Replay known unsafe proposals on fresh checkouts; no model inference.

Run: uv run python -m bench.replay
This is a regression benchmark, not independent held-out evidence.
"""

import json
import tempfile
from pathlib import Path

from less_code.corpus import load_corpus, run_corpus, write_corpus_report
from less_code.ml_shrink import ProposedEdit


class Replay:
    name = "recorded-unsafe-proposals-regression-only"

    def __init__(self, rows):
        self.records = [
            record
            for row in rows
            for record in row["ml_records"]
            if record["accepted"] and row["name"] in {"boltons", "fastq"}
        ]
        self.matched = set()

    def propose(self, lang, symbol):
        for record in self.records:
            if (
                record["symbol"] == symbol["name"]
                and record["prompt"]["source"] == symbol["text"]
            ):
                self.matched.add(record["symbol"])
                return ProposedEdit(symbol["id"], record["replacement"])
        return None


def main():
    directory = Path(__file__).resolve().parent
    historical = json.loads((directory / "model-experiment.json").read_text())
    backend = Replay(historical["projects"])
    projects = [
        p
        for p in load_corpus(directory / "corpus.toml")
        if p.name in {"boltons", "fastq"}
    ]
    with tempfile.TemporaryDirectory(prefix="less-code-replay-") as scratch:
        manifest = Path(scratch) / "corpus.toml"
        manifest.write_text(
            "\n".join(
                "[[project]]\n"
                + "\n".join(
                    f"{key} = {json.dumps(getattr(project, key))}"
                    for key in (
                        "name",
                        "repo",
                        "commit",
                        "lang",
                        "test",
                        "prepare",
                        "audit",
                        "cohort",
                    )
                )
                for project in projects
            )
        )
        result = run_corpus(manifest, ml_backend=backend, ml_attempts=1, ml_symbols=64)
    result["experiment"]["kind"] = (
        "historical unsafe proposal replay; not new inference"
    )
    result["experiment"]["matched_symbols"] = sorted(backend.matched)
    write_corpus_report(result, directory / "replay.json", directory / "REPLAY.md")
    assert len(backend.matched) == 2, "Both historical unsafe edits must be exercised"
    assert all(row["valid"] and row["audit_ok"] for row in result["projects"])
    assert all(row["ml_stats"].get("rolled_back") == 1 for row in result["projects"])
    print(
        "Both known unsafe edits rolled back; final suites and regression audits pass."
    )


if __name__ == "__main__":
    main()
