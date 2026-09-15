"""Phase 6: exam/homework separation is enforced, not promised."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from training.anticontamination import (
    BENCHMARK_CRATES,
    find_contamination,
    protected_tokens,
    scan_file,
)
from training.build_manifest import _blocked


def test_benchmark_crate_names_are_protected():
    tokens = protected_tokens()
    for name in ("itoa", "humantime", "shell-words", "strsim", "strsim-rs"):
        assert name in tokens


def test_find_contamination_detects_names_repos_and_commits():
    crate = BENCHMARK_CRATES[0]
    contaminated = {
        "messages": [
            {"role": "user", "content": f"rewrite {crate['name']} source"},
            {"role": "assistant", "content": "fn x() {}"},
        ],
        "meta": {"repo": crate["repo"], "commit": crate["commit"]},
    }
    expected = sorted(
        {
            crate["name"],
            crate["repo"],
            crate["repo"].removesuffix(".git"),
            crate["commit"],
        }
    )
    assert find_contamination(contaminated) == expected
    clean = {
        "messages": [
            {"role": "user", "content": "rewrite this small parser crate"},
            {"role": "assistant", "content": "fn squeeze(x: u64) -> u64 { x }"},
        ],
        "meta": {"repo": "https://github.com/example/other", "commit": "abc123"},
    }
    assert find_contamination(clean) == []


def test_scan_file_over_jsonl(tmp_path):
    crate = BENCHMARK_CRATES[1]
    dirty = tmp_path / "dirty.jsonl"
    dirty.write_text(
        json.dumps({"messages": [{"content": f"about {crate['name']}"}]})
        + "\n"
        + json.dumps({"messages": [{"content": "clean row"}]})
        + "\n"
    )
    assert scan_file(dirty) == [crate["name"]]
    clean = tmp_path / "clean.jsonl"
    clean.write_text(json.dumps({"messages": [{"content": "clean row"}]}) + "\n")
    assert scan_file(clean) == []


def test_manifest_blocklist_rejects_benchmark_names_and_repos():
    assert _blocked("itoa", "https://github.com/dtolnay/itoa")
    assert _blocked("strsim", "https://github.com/rapidfuzz/strsim-rs")
    assert _blocked("humantime", "https://github.com/tailhook/humantime")
    assert _blocked("shell-words", "https://github.com/tmiasko/shell-words")
    assert not _blocked("anyhow", "https://github.com/dtolnay/anyhow")
    assert not _blocked("getrandom", "https://github.com/rust-random/getrandom")


def test_pair_for_rejects_contaminated_content():
    """Mining-side guard: a pair whose content mentions a benchmark crate is
    skipped, so the dataset files can never grow exam material."""
    from training.mine import pair_for

    crate = BENCHMARK_CRATES[0]
    record = {
        "path": "src/lib.rs",
        "proposals": [{"attempt": 1, "accepted": True}],
        "loc_before": 100,
        "loc_after": 90,
        "digest": "d",
    }
    capture = {
        "src/lib.rs": [
            {
                "messages": {
                    "system": "s",
                    "user": {
                        "path": "lib.rs",
                        "source": f"// faster than {crate['name']}",
                        "context": "",
                        "feedback": None,
                    },
                },
                "reply": "fn x() {}",
                "duration_s": 1.0,
            }
        ]
    }
    pair = pair_for("somecrate", capture, record["proposals"][0], record)
    assert pair is not None
    assert find_contamination(pair), "the fixture itself must be contaminated"
