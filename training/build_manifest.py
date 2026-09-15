"""Mechanically select the training cohort (PLAN.md §6.1).

Fixed criteria, applied in order, BEFORE any reduction is measured on any
crate: crates.io top-by-downloads scrape -> permissive license -> not a
benchmark crate (name + repo blocklist, checked in code) -> 200 <= src LOC
<= 6000 (canonical measure) -> `cargo test` green on a fresh clone within
300s. The first TARGET crates that pass become the cohort; 10 of them are
reserved `role = "eval"` and never fine-tuned on.

Usage:
  uv run python training/build_manifest.py --target 60
  uv run python training/build_manifest.py --validate-config   # CPU only
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from less_code.langdetect import map_project
from less_code.loc import measure
from training.anticontamination import BENCHMARK_CRATES

MANIFEST = Path(__file__).resolve().parent / "manifest.toml"
PERMISSIVE = (
    "MIT",
    "Apache-2.0",
    "BSD-3-Clause",
    "BSD-2-Clause",
    "ISC",
    "Unicode-3.0",
    "Unicode-DFS-2016",
)
MIN_LOC, MAX_LOC = 200, 6000
TEST_TIMEOUT = 300
USER_AGENT = "less-code-research (RL training cohort selection)"


def validate_config() -> list[str]:
    errors = []
    try:
        top = _scrape(1, 10)
        if not top:
            errors.append("crates.io scrape returned nothing")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"crates.io unreachable: {exc}")
    if not shutil_which("cargo"):
        errors.append("cargo not on PATH")
    return errors


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


def _scrape(pages: int, per_page: int = 100) -> list[dict]:
    out: list[dict] = []
    for page in range(1, pages + 1):
        url = (
            "https://crates.io/api/v1/crates"
            f"?page={page}&per_page={per_page}&sort=downloads"
        )
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.load(response)
        out.extend(data.get("crates", []))
        time.sleep(1.0)
    return out


def _crate_detail(name: str) -> dict:
    """License now lives on the newest version, not the crate object."""
    request = urllib.request.Request(
        f"https://crates.io/api/v1/crates/{name}",
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.load(response)
    crate = data.get("crate", {})
    versions = data.get("versions") or []
    crate["license"] = next(
        (v.get("license") for v in versions if v.get("license")), None
    )
    return crate


def _license_ok(license_text: str | None) -> bool:
    if not license_text:
        return False
    parts = {part.strip(" ()ORANDor+") for part in license_text.split()}
    return any(p in PERMISSIVE for p in parts if p)


def _blocked(name: str, repo: str) -> bool:
    for crate in BENCHMARK_CRATES:
        if name == crate["name"] or name == crate["name"].replace("-rs", ""):
            return True
        if repo and crate["repo"].rsplit("/", 1)[-1].removesuffix(".git") in repo:
            return True
    return False


def _head_commit(repo_url: str, workdir: Path) -> tuple[str | None, Path | None]:
    dest = workdir / repo_url.rsplit("/", 1)[-1].removesuffix(".git")
    if dest.exists():
        subprocess.run(["rm", "-rf", str(dest)], check=True)
    clone = subprocess.run(
        ["git", "clone", "--quiet", repo_url, str(dest)],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    if clone.returncode:
        return None, None
    rev = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=dest,
        capture_output=True,
        text=True,
        check=False,
    )
    return (rev.stdout.strip() if rev.returncode == 0 else None), dest


def _src_loc(crate_dir: Path) -> int | None:
    try:
        project = map_project(crate_dir, "rust")
    except SystemExit:
        return None
    if not (crate_dir / "Cargo.toml").is_file() or not project.source_files:
        return None
    if any(
        "target" not in str(p.relative_to(crate_dir)) and p.stat().st_size > 1_000_000
        for p in project.source_files
    ):
        return None
    return sum(
        measure(p.read_text(encoding="utf-8", errors="replace"), "rust").code
        for p in project.source_files
    )


def _tests_green(crate_dir: Path) -> bool:
    result = subprocess.run(
        ["cargo", "test", "--quiet"],
        cwd=crate_dir,
        capture_output=True,
        text=True,
        timeout=TEST_TIMEOUT,
        check=False,
    )
    return result.returncode == 0


def build(target: int, pages: int = 12) -> dict:
    selected: list[dict] = []
    seen_repos: set[str] = set()
    candidates = _scrape(pages)
    print(f"scraped {len(candidates)} candidate crates", file=sys.stderr)
    with tempfile.TemporaryDirectory(prefix="lc-manifest-") as scratch:
        workdir = Path(scratch)
        for entry in candidates:
            if len(selected) >= target:
                break
            name = entry.get("id", "")
            repository = entry.get("repository") or ""
            license_text = entry.get("license") or _crate_detail(name).get("license")
            if not _license_ok(license_text):
                continue
            if not repository.startswith("https://"):
                continue
            if repository in seen_repos:
                continue
            if _blocked(name, repository):
                print(f"skip {name}: benchmark blocklist", file=sys.stderr)
                continue
            repo_url = repository.rstrip("/") + ".git"
            seen_repos.add(repository)
            try:
                commit, crate_dir = _head_commit(repo_url, workdir)
            except (subprocess.SubprocessError, OSError):
                continue
            if commit is None or crate_dir is None:
                continue
            loc = _src_loc(crate_dir)
            if loc is None or not MIN_LOC <= loc <= MAX_LOC:
                continue
            try:
                green = _tests_green(crate_dir)
            except subprocess.TimeoutExpired:
                green = False
            if not green:
                continue
            selected.append(
                {
                    "name": name,
                    "repo": repository.rstrip("/"),
                    "commit": commit,
                    "license": license_text,
                    "loc": loc,
                    "role": "train",
                }
            )
            print(
                f"selected {name} ({loc} LOC, {license_text}) "
                f"[{len(selected)}/{target}]",
                file=sys.stderr,
            )
    for i in range(min(10, len(selected))):
        selected[-1 - i]["role"] = "eval"
    return {"crates": selected}


def write_manifest(cohort: dict) -> Path:
    lines = [
        "# Training cohort (PLAN.md 6.1). Mechanically selected from the crates.io",
        "# top-downloads ranking BEFORE any reduction was measured; blocklist-checked",
        "# against bench/corpus.toml by training/anticontamination.py, which is unit",
        '# tested. role = "eval" rows are held out from every fine-tune.',
        "",
    ]
    for crate in cohort["crates"]:
        lines.append("[[crate]]")
        for key in ("name", "repo", "commit", "license", "loc", "role"):
            lines.append(
                f'{key} = "{crate[key]}"' if key != "loc" else f"loc = {crate[key]}"
            )
        lines.append("")
    MANIFEST.write_text("\n".join(lines) + "\n")
    return MANIFEST


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=int, default=60)
    parser.add_argument("--pages", type=int, default=12)
    parser.add_argument("--validate-config", action="store_true")
    args = parser.parse_args()
    if args.validate_config:
        errors = validate_config()
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        print("config ok: crates.io reachable, cargo present")
        return 0
    cohort = build(args.target, args.pages)
    if len(cohort["crates"]) < 40:
        print(
            f"only {len(cohort['crates'])} crates passed the mechanical"
            " criteria (need >= 40); manifest NOT written",
            file=sys.stderr,
        )
        return 1
    print(f"manifest: {write_manifest(cohort)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
