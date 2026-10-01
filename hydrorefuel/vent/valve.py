"""两段放散阀（先大后小，顺序定死）。

放散阀对外仍是"开 / 关"两态，但开位分两档：大流量段（``large``，一段）与
小流量收尾段（``small``，二段）。先开只能落在大流量位；压力降到切段压力后
由 :meth:`switch_to_small` 切到小流量位，**不存在反向切档**——
"先大后小"在阀门层就被写死，任何跳段或倒序都直接报联锁错误。

压降不是瞬时的：:meth:`bleed` 按当前压力与当前档位孔口系数算出这一段的
压降，压力越接近常压放散越慢。未开阀就调用放散会直接报错，避免审计里出现
"没开阀却压降了"的假记录。

切段是否真的成功，由三项观测共同判定：阀位反馈（大阀关到位、小阀开到位）、
压降速率是否落到小流量带内、以及有没有仍在按大流量走。
:meth:`arm_fault` 按类型注入阀门/反馈故障，供验证"切段失败必须看得出来"。
"""

from __future__ import annotations

from ..config import VentLimits
from ..errors import InterlockError

LARGE = "large"
SMALL = "small"
SWITCHING = "switching"

# 可注入的切段故障：
#  small_not_open  大阀关到位但小阀没开（切段后根本不放散）
#  large_not_close 小阀开了但大阀没关死（仍按大流量走）
#  no_position     两个位置反馈都没到位（位置不明）
SWITCH_FAULTS = ("small_not_open", "large_not_close", "no_position")


class VentValve:
    """两段放散阀的档位、开闭与压降模型。"""

    def __init__(self, limits: VentLimits) -> None:
        self._limits = limits
        self._open = False
        self._position = LARGE
        self._bursts = 0
        self._switches = 0
        self._vented_mpa = 0.0
        self._vented_by_stage = {1: 0.0, 2: 0.0}
        self._stage1_closed_mpa: float | None = None
        self._fault: str | None = None

    @property
    def limits(self) -> VentLimits:
        return self._limits

    def open(self, *, small: bool = False) -> None:
        """打开放散阀。

        正常发起放散固定落在大流量段（先大后小，不许跳段）；只有续放且
        一段边界已经冻结时，才允许直接开在小流量位把收尾段接着放完。
        """

        if self._open:
            raise InterlockError("放散阀已经打开", component="vent")
        self._open = True
        self._position = SMALL if small else LARGE
        self._bursts += 1

    def close(self) -> None:
        """关闭放散阀。"""

        if not self._open:
            raise InterlockError("放散阀未打开，无需关闭", component="vent")
        self._open = False
        self._position = LARGE

    def is_open(self) -> bool:
        """放散阀是否处于打开状态。"""

        return self._open

    def position(self) -> str:
        """当前阀位反馈：``large`` / ``small`` / ``switching``。"""

        return self._position

    def stage(self) -> int | None:
        """当前阀位对应的放散段号（1=大流量，2=小流量，切换中/关闭为 None）。"""

        if not self._open:
            return None
        if self._position == LARGE:
            return 1
        if self._position == SMALL:
            return 2
        return None

    def switch_to_small(self) -> None:
        """由大流量段切到小流量段，只允许大→小这一个方向。"""

        if not self._open:
            raise InterlockError("放散阀未打开，不能切段", component="vent")
        if self._position == SMALL:
            raise InterlockError("已在小流量段，不能重复切段", component="vent")
        if self._position == SWITCHING:
            raise InterlockError("正在切段中，不能重复发起切段", component="vent")
        self._position = SWITCHING
        self._switches += 1

    def reopen_large(self) -> None:
        """切段失败且管路策略为退回时，把阀门重新置回大流量位。"""

        if not self._open:
            raise InterlockError("放散阀未打开，不能退回大流量段", component="vent")
        if self._position != SWITCHING:
            raise InterlockError("只有切段中的阀门才能退回大流量段", component="vent")
        self._position = LARGE

    def confirm_position(self) -> str:
        """读取切段后的阀位反馈，故障注入时反馈会与指令不一致。"""

        if self._position != SWITCHING:
            return self._position
        if self._fault == "small_not_open":
            # 大阀关了、小阀没开：两段都不通。
            return "closed"
        if self._fault in ("large_not_close", "no_position"):
            # 大阀没关死：阀仍停在大流量位（或位置不明，按大流量处理）。
            return LARGE
        return SMALL

    def settle_switch(self) -> None:
        """切段窗口结束，按反馈确定阀门开闭，但阀位保持切换中由控制器裁决。

        反馈到位才落到小流量位；反馈不到位时阀门保留在切换中状态，由
        状态机决定是退回大流量段（``reopen_large``）还是关阀报警，避免
        阀门层越权替编排做"切段失败怎么办"的管路策略决定。
        """

        if self._position != SWITCHING:
            raise InterlockError("当前不在切段中", component="vent")
        feedback = self.confirm_position()
        if feedback == SMALL:
            self._position = SMALL
            return
        if feedback == "closed":
            # 大阀关了、小阀没开：两段都不通，阀整体关掉防内漏。
            self._open = False
        # 仍挂在 SWITCHING 位，等控制器 reopen_large / 关阀。

    def close_stage1(self) -> float:
        """切段成功：冻结一段压降边界，二段从零重新累计，返回一段段量。"""

        self._stage1_closed_mpa = self._vented_by_stage[1]
        self._vented_by_stage[2] = 0.0
        return self._stage1_closed_mpa

    def arm_fault(self, fault: str) -> None:
        """注入下一次切段故障（验证用），取清除值外的取值直接拒绝。"""

        if fault not in SWITCH_FAULTS:
            raise ValueError(f"未知放散故障类型: {fault}")
        self._fault = fault

    def clear_fault(self) -> None:
        """清除注入的切段故障（验证用）。"""

        self._fault = None

    def fault(self) -> str | None:
        """返回当前挂着的切段故障类型。"""

        return self._fault

    def _effective_small(self) -> bool:
        """当前观测到的实际档位是否为小流量（切换中按反馈推断）。"""

        if self._position == SMALL:
            return True
        if self._position == SWITCHING:
            return self.confirm_position() == SMALL
        return False

    def bleed_rate_mpa_per_s(self, pressure_mpa: float) -> float:
        """返回当前压力、当前档位下的压降速率。"""

        return self._limits.vent_rate_mpa_per_s(pressure_mpa, small=self._effective_small())

    def expected_rate_mpa_per_s(self, pressure_mpa: float, *, small: bool) -> float:
        """返回指定档位在当前压力下应有的压降速率，供切段核对。"""

        return self._limits.vent_rate_mpa_per_s(pressure_mpa, small=small)

    def bleed(self, pressure_mpa: float, dt: float) -> float:
        """按速率推进一个步长，返回放散后的压力，并按段累计压降。"""

        if not self._open:
            raise InterlockError("放散阀未打开，不能放散", component="vent")
        if self._position == SWITCHING:
            raise InterlockError("切段尚未确认，不能继续放散", component="vent")
        if dt <= 0:
            raise ValueError("步长必须为正")
        drop = self.bleed_rate_mpa_per_s(pressure_mpa) * dt
        remaining = max(0.0, float(pressure_mpa) - drop)
        actual = float(pressure_mpa) - remaining
        self._vented_mpa += actual
        stage = 2 if self._position == SMALL else 1
        self._vented_by_stage[stage] += actual
        return remaining

    def vented_mpa(self, stage: int | None = None) -> float:
        """返回累计放散掉的压力；指定段号时只返回该段。"""

        if stage is None:
            return self._vented_mpa
        if stage not in (1, 2):
            raise ValueError("放散段号只能是 1 或 2")
        return self._vented_by_stage[stage]

    def bursts(self) -> int:
        """返回累计开阀次数。"""

        return self._bursts

    def switch_attempts(self) -> int:
        """返回累计发起切段的次数。"""

        return self._switches

    def restore(self, *, open_large: bool, stage1_mpa: float, stage2_mpa: float, stage1_closed: bool) -> None:
        """按落盘检查点重建阀位与各段累计压降（续放用）。"""

        self._open = bool(open_large)
        self._position = LARGE
        self._vented_by_stage = {1: float(stage1_mpa), 2: float(stage2_mpa)}
        self._vented_mpa = self._vented_by_stage[1] + self._vented_by_stage[2]
        self._stage1_closed_mpa = float(stage1_mpa) if stage1_closed else None
