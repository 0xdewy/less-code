"""Compare only matching pinned projects; never count corpus growth as progress."""

import argparse
import json
from collections import Counter
from pathlib import Path
from statistics import median


def compare(before, after):
    previous = {row["name"]: row for row in before["projects"]}
    lines = [
        "# Benchmark experiment",
        "",
        "| Project | Cohort | Before % | After static % | After model % | Comparable |",
        "|---|---|---:|---:|---:|---|",
    ]
    matched = []
    for row in after["projects"]:
        old = previous.get(row["name"], {})
        comparable = bool(
            old.get("valid")
            and row["valid"]
            and old.get("commit") == row.get("commit")
            and old.get("loc_start") == row["loc_start"]
            and "test_command" in old
            and "test_command" in row
            and old["test_command"] == row["test_command"]
            and old.get("audit_command") == row.get("audit_command")
        )
        if comparable:
            matched.append((old, row))
        lines.append(
            f"| {row['name']} | {row.get('cohort', 'original')} | {old.get('raw_pct', 0)} | {row.get('static_pct', 0)} | {row.get('raw_pct', 0)} | {'yes' if comparable else 'NO: baseline/gate/measurement mismatch'} |"
        )
    baseline = sum(old["loc_start"] for old, _ in matched)
    before_final = sum(old["loc_final"] for old, _ in matched)
    after_static = sum(row["loc_after_static"] for _, row in matched)
    after_final = sum(row["loc_final"] for _, row in matched)
    lines += [
        "",
        f"Comparable projects: {len(matched)}/{len(after['projects'])}.",
        f"On the same {baseline} starting canonical LOC: before {before_final}, after static {after_static}, after model {after_final}.",
        f"Additional accepted lines: {before_final - after_final} ({before_final - after_static} static, {after_static - after_final} model).",
        "",
    ]
    for cohort in ("original", "expansion"):
        group = [
            row for row in after["projects"] if row.get("cohort", "original") == cohort
        ]
        start = sum(row["loc_start"] for row in group)
        final = sum(row["loc_final"] for row in group)
        pct = 100 * (start - final) / start if start else 0
        middle = median(row.get("raw_pct", 0) for row in group) if group else 0
        lines.append(
            f"{cohort}: {sum(row['valid'] for row in group)}/{len(group)} valid; {start} → {final} LOC ({pct:.2f}% weighted); median project {middle:.2f}%."
        )
    records = [
        record for row in after["projects"] for record in row.get("ml_records", [])
    ]
    if "review_filtered_aggregate" in after:
        reviewed_final = sum(row["reviewed_loc_final"] for _, row in matched)
        lines[2:2] = [
            f"**Review-filtered gain: {before_final - reviewed_final} additional lines.** The table retains raw test-gated scores, including edits subsequently rejected in review.",
            "",
        ]
        lines += [
            "",
            f"Review-filtered comparable result: {baseline} → {reviewed_final} LOC; {before_final - reviewed_final} additional lines ({before_final - after_static} static, {after_static - reviewed_final} model).",
            "Rejected model layers fall back to their previously verified static stage. This is a post-run review policy, not an automatic semantic proof. See MODEL_REVIEW.md for counterexamples.",
        ]
    lines += [
        "",
        f"Model outcomes: {dict(Counter(record['category'] for record in records))}.",
        f"Recorded model-attempt and gate time: {sum(record['duration_s'] for record in records):.1f}s.",
        "",
        "These are gate-accepted reductions, not a proof of semantic equivalence. Failed projects remain visible and score zero. Unmeasured projects cannot contribute a known LOC denominator. Corpus expansion was selected before measuring yield; it is still mostly small libraries, not a representative sample of all software. Zero reduction can reflect missing candidate support (for example JavaScript arrow functions). Rust inline tests are frozen but still included in source-file LOC, so cross-language absolute LOC is not strictly production-only.",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--out", type=Path, default=Path("bench/EXPERIMENT.md"))
    args = parser.parse_args()
    report = compare(
        json.loads(args.before.read_text()), json.loads(args.after.read_text())
    )
    args.out.write_text(report)
    print(report)
