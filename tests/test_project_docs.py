"""Executable checks for the open-source delivery documentation."""

from pathlib import Path

from evals.readme_benchmark import update_readmes

ROOT = Path(__file__).resolve().parent.parent


def test_readme_benchmark_blocks_match_checked_in_reports():
    assert update_readmes(check=True) == []


def test_delivery_docs_and_attribution_are_present():
    required = [
        "docs/design/01-state-machine.md",
        "docs/design/02-tool-contract.md",
        "docs/design/03-verification.md",
        "docs/design/04-checkpoint-resume.md",
        "docs/design/05-evaluation.md",
        "docs/adr/0001-explicit-loop-over-langgraph.md",
        "docs/adr/0002-sqlite-run-store.md",
        "docs/adr/0003-external-evidence-final-judge.md",
        "CONTRIBUTING.md",
        "CHANGELOG.md",
        "RELEASE_NOTES.md",
    ]
    assert all((ROOT / path).is_file() for path in required)

    for readme_name in ("README.md", "README_CN.md"):
        text = (ROOT / readme_name).read_text(encoding="utf-8")
        assert "https://github.com/he-yufeng/CoreCoder" in text
        assert "bd64cfe7ad2789fa0ab1b3a689ad160567b5cdb0" in text
        assert "Inherited" in text or "inherited work" in text
        assert "Added" in text or "personal work" in text
        assert "python -m examples.reliablecoder_demo" in text
