"""Small repository fixture: journal, recovery and evidence are separate."""


class RunStore:
    """持久化工具执行事实，不从模型文字推断任务成功。"""

    def resume_run(self, receipts):
        """复用已成功回执；未知副作用需要处理。"""
        return [receipt for receipt in receipts if receipt["status"] == "succeeded"]


def reconcile_file(expected_hash, current_hash):
    """文件内容哈希匹配时，恢复不再重复写入。"""
    return expected_hash == current_hash
