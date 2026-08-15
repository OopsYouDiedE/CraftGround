"""未来动作缓存。

`upload` 把多个 tick 的动作塞进缓存，`step` 每次取出一个 tick 执行。缓存按绝对 tick
索引而非队列顺序，因为协议的 `upload` 返回「该段首 tick 在本回合内的相对序号」，意味着
提交是带锚点的时间轴写入，后提交的段可以覆盖先提交的段。

缓存为空时的行为由 `UnderflowPolicy` 决定，默认 `NOOP`：`step` 承诺全实例同步，任一
实例阻塞会拖住整批。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shared_tools.action_conversion import (
    AXIS_COUNT,
    SLOT_COUNT,
    V1_BUTTON_COUNT,
    V2_BUTTON_COUNT,
    Tick,
    Version,
)
from shared_tools.environment_protocol import SubmitResult, UnderflowPolicy


def idle_tick(version: Version) -> Tick:
    """构造一个全零 tick，即协议的终止帧。

    Args:
        version: 协议版本，`1` 或 `2`。

    Returns:
        位串全 `0`、轴全 `0.00`、v2 槽位全 `0 0 0` 的 tick。
    """
    if version == 1:
        return Tick(buttons=(0,) * V1_BUTTON_COUNT, axes=(0.0,) * AXIS_COUNT)
    return Tick(
        buttons=(0,) * V2_BUTTON_COUNT,
        axes=(0.0,) * AXIS_COUNT,
        slots=((0, 0, 0),) * SLOT_COUNT,
    )


@dataclass
class ActionQueue:
    """一个 agent 的未来动作缓存。

    Attributes:
        version: 协议版本。`upload` 会校验入参版本与此一致——协议规定整个控制器统一，
            不得混编，而 `Tick.version` 由段结构自动推断，因此校验点必须在这里。
        underflow: 缓存为空时的策略。
        current_tick: 下一个将被执行的绝对 tick 序号，相对本回合起点。
    """

    version: Version = 2
    underflow: UnderflowPolicy = UnderflowPolicy.NOOP
    current_tick: int = 0
    _scheduled: dict[int, Tick] = field(default_factory=dict, repr=False)
    _last_executed: Tick | None = field(default=None, repr=False)
    _last_submission: SubmitResult | None = field(default=None, repr=False)

    @property
    def buffered_ticks(self) -> int:
        """尚未执行的已缓存 tick 数。"""
        return sum(1 for tick in self._scheduled if tick >= self.current_tick)

    @property
    def last_submission(self) -> SubmitResult | None:
        """最近一次 `submit` 的完整结果；协议的 `upload` 只返回其 `start_tick`。"""
        return self._last_submission

    def submit(self, actions: list[Tick], *, start_tick: int | None = None) -> SubmitResult:
        """把一段动作写入缓存。

        Args:
            actions: 待执行的 tick 序列，下标即相对 `start_tick` 的偏移。
            start_tick: 该段的绝对锚点。`None` 表示接在当前 tick 处，即立即生效。

        Returns:
            提交结果。锚点早于 `current_tick` 的 tick 被丢弃并计入 `expired_ticks`，
            覆盖已有 tick 的计入 `overwritten_ticks`。

        Raises:
            ValueError: `actions` 为空、版本与队列不一致，或 `start_tick` 为负。
        """
        if not actions:
            raise ValueError("actions 不能为空")
        if start_tick is not None and start_tick < 0:
            raise ValueError(f"start_tick 不能为负，实际 {start_tick}")
        versions = {tick.version for tick in actions}
        if versions != {self.version}:
            raise ValueError(f"动作版本必须为 v{self.version}，实际包含 {sorted(versions)}")

        anchor = self.current_tick if start_tick is None else start_tick
        expired = 0
        overwritten = 0
        accepted = 0
        for offset, tick in enumerate(actions):
            target = anchor + offset
            if target < self.current_tick:
                expired += 1
                continue
            if target in self._scheduled:
                overwritten += 1
            self._scheduled[target] = tick
            accepted += 1
        result = SubmitResult(
            start_tick=anchor,
            accepted_ticks=accepted,
            expired_ticks=expired,
            overwritten_ticks=overwritten,
        )
        self._last_submission = result
        return result

    def pull(self) -> Tick:
        """取出当前 tick 应执行的动作并推进指针。

        Returns:
            该 tick 的动作。缓存中没有该 tick 时按 `underflow` 策略产生一个。

        Raises:
            RuntimeError: `underflow` 为 `WAIT` 且缓存中没有当前 tick。
        """
        tick = self._scheduled.pop(self.current_tick, None)
        if tick is None:
            tick = self._underflow_tick()
        self.current_tick += 1
        self._last_executed = tick
        return tick

    def _underflow_tick(self) -> Tick:
        """按策略产生一个下溢 tick。"""
        if self.underflow is UnderflowPolicy.WAIT:
            raise RuntimeError(f"tick {self.current_tick} 没有已缓存的动作，且下溢策略为 WAIT")
        if self.underflow is UnderflowPolicy.REPEAT_LAST and self._last_executed is not None:
            return self._last_executed
        return idle_tick(self.version)

    def reset(self) -> None:
        """清空缓存并把 tick 指针归零。用于新回合或倒档。

        倒档也要清空：协议的 `rewind_checkpoint` 回到上一个 checkpoint，此后的未来动作
        缓存对应的是被丢弃的那条分支，留下会让重跑执行到错误的动作。
        """
        self._scheduled.clear()
        self.current_tick = 0
        self._last_executed = None
        self._last_submission = None

    def stop(self) -> None:
        """清空当前待执行帧及其后的未来动作，不回退已执行动作。"""
        self._scheduled = {
            tick: action for tick, action in self._scheduled.items() if tick < self.current_tick
        }

    def rewind_to(self, tick_index: int) -> None:
        """把 tick 指针退回 `tick_index`，并丢弃该 tick 之后的缓存。

        用于 checkpoint 倒档到回合中途而非回合起点。

        Raises:
            ValueError: `tick_index` 为负或晚于当前 tick。
        """
        if tick_index < 0:
            raise ValueError(f"tick_index 不能为负，实际 {tick_index}")
        if tick_index > self.current_tick:
            raise ValueError(f"tick_index {tick_index} 晚于当前 tick {self.current_tick}，无法倒档")
        self._scheduled = {
            tick: action for tick, action in self._scheduled.items() if tick < tick_index
        }
        self.current_tick = tick_index
        self._last_executed = None
