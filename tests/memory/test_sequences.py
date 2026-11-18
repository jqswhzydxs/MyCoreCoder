"""The fixed twenty histories are independently runnable evaluation inputs."""

import json

from evals.memory_runner import CASES


def test_twenty_frozen_sequences_cover_lifecycle_boundaries():
    data = json.loads(CASES.read_text(encoding="utf-8"))
    cases = data["cases"]
    assert len(cases) == len({case["id"] for case in cases}) == 20
    assert {"cross_session", "conflict", "source_change", "ttl", "delete", "isolation", "unverified", "candidate",
            "priority", "sensitive", "authority", "budget", "operation"}.issubset({case["category"] for case in cases})
    for case in cases:
        assert case["steps"][-1]["action"] == "recall"
