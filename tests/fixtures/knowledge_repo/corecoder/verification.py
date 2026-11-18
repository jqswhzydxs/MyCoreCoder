"""Verification fixture with a same-named helper in another module."""


class Verifier:
    def verify(self, evidence):
        """只有真实证据才可确认完成。"""
        return bool(evidence)


def reconcile_file(expected_hash, current_hash):
    """同名符号不代表同一个来源片段。"""
    return expected_hash == current_hash
