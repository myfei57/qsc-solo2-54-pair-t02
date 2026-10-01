"""放散质量流量计（按段累计）。

放散量要"每段排掉多少都对得上"，流量计按段记账：一段（大流量）与二段
（小流量）各自一个真值累计器，段不结清、账不串。切段成功的那一刻把一段
真值冻结成段边界（:meth:`close_stage1`），二段从零重新累计；切段失败
退回大流量时一段继续往上加，不会把退回期间放掉的量错记成二段。

现场流量计可能带偏差（``bias``）并在冻结排查时被重新标定（``cal_factor``），
对外的"显示读数"按 ``真值 × (1+bias) × cal_factor`` 给；核对、审计与
检查点都使用这个读数，因此恢复账册时先落真值再乘一次系数，标定修正永远
只作用一次。
"""

from __future__ import annotations

STAGES = (1, 2)


class VentFlowMeter:
    """两段放散的质量流量累计（真值 + 现场显示系数）。"""

    def __init__(self) -> None:
        # 不套任何现场系数的真值账（与压力折算账同口径）。
        self._true_by_stage = {1: 0.0, 2: 0.0}
        self._stage1_closed_true_kg: float | None = None
        self._bias = 0.0
        self._cal_factor = 1.0

    # ---- 系数 -----------------------------------------------------------

    def set_bias(self, bias: float) -> None:
        """设置流量计相对偏差（如 0.3 表示多计 30%，验证用）。"""

        if bias <= -1.0:
            raise ValueError("流量计偏差必须大于 -1.0")
        self._bias = float(bias)

    def bias(self) -> float:
        """返回当前流量计相对偏差。"""

        return self._bias

    def calibrate(self, factor: float) -> None:
        """叠加重标定系数（现场冻结排查时调用）。

        真值账不动，只改显示系数；恢复账册时按"真值 × 系数"重建，标定
        不会被重复作用。
        """

        if factor <= 0.0:
            raise ValueError("标定系数必须为正")
        self._cal_factor *= float(factor)

    def cal_factor(self) -> float:
        """返回当前累计的重标定系数。"""

        return self._cal_factor

    def _factor(self) -> float:
        return (1.0 + self._bias) * self._cal_factor

    # ---- 记账 -----------------------------------------------------------

    def commit(self, true_mass_kg: float, stage: int) -> float:
        """把一个步长的放散真值质量记到指定段，返回该段显示累计量。"""

        if stage not in STAGES:
            raise ValueError("放散段号只能是 1 或 2")
        mass = float(true_mass_kg)
        if mass < 0:
            raise ValueError("放散增量不能为负")
        self._true_by_stage[stage] += mass
        return self.stage_kg(stage)

    def close_stage1(self) -> float:
        """切段成功：冻结一段真值边界，二段真值清零，返回一段显示段量。"""

        self._stage1_closed_true_kg = self._true_by_stage[1]
        self._true_by_stage[2] = 0.0
        return self.stage_kg(1)

    def stage1_closed_kg(self) -> float | None:
        """返回一段已冻结的边界显示段量（未切段为 None）。"""

        if self._stage1_closed_true_kg is None:
            return None
        return self._stage1_closed_true_kg * self._factor()

    def stage_true_kg(self, stage: int) -> float:
        """返回指定段的真值累计质量（不套现场系数）。"""

        if stage not in STAGES:
            raise ValueError("放散段号只能是 1 或 2")
        return self._true_by_stage[stage]

    def stage_kg(self, stage: int) -> float:
        """返回指定段的显示累计质量（真值 × 偏差 × 标定）。"""

        return self.stage_true_kg(stage) * self._factor()

    def total_kg(self) -> float:
        """返回两段合计的显示放散质量。"""

        return (self._true_by_stage[1] + self._true_by_stage[2]) * self._factor()

    def restore(
        self,
        stage1_true_kg: float,
        stage2_true_kg: float,
        *,
        stage1_closed: bool,
        bias: float = 0.0,
        cal_factor: float = 1.0,
    ) -> None:
        """按真值账与现场系数重建流量计（续放/跨重启用）。"""

        self._true_by_stage = {1: float(stage1_true_kg), 2: float(stage2_true_kg)}
        self._stage1_closed_true_kg = float(stage1_true_kg) if stage1_closed else None
        self._bias = float(bias)
        self._cal_factor = float(cal_factor)
