from collections import Counter

from evals.knowledge_runner import load_cases


def test_fixed_knowledge_dataset_preserves_strata_and_resolvable_gold_sources():
    cases = load_cases("all")
    assert len(cases) == 40
    assert Counter(case["category"] for case in cases) == {"exact": 10, "cross_module": 10, "design": 8, "no_answer": 6, "adversarial": 6}
    assert len(load_cases("dev")) == 24 and len(load_cases("holdout")) == 16
