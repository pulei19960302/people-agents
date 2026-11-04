"""执行流程可明确识别的领域异常。"""


class RunBudgetExceeded(RuntimeError):
    """持久化额度已用尽；需要调整额度后恢复，不能当成方案不可行。"""
