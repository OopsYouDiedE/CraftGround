"""CraftGround 的 Agent 级实现。

一个 `CraftGroundAgent` 对应一个 CraftGround 实例里的单个玩家。CraftGround 是单人环境
（`MemorySnapshotStore` 明确要求 `players.size == 1`），因此一个实例只有一个 agent，
多实例并行由控制器负责。

观察通道与 harness 通道分离：`agent_observe()` 只给 `rgb` 与 `state`，坐标、生命、
reward 这类真值走 `agent_info()`。CraftGround 把两者混在同一个观察字典里，这里做拆分。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import numpy as np

from shared_tools.action_conversion import Tick, format_tick
from shared_tools.environment_protocol import (
    AgentState,
    Observation,
    SubmitResult,
    UnderflowPolicy,
)

from .action_adapter import CraftGroundActionAdapter
from .action_queue import ActionQueue


class CraftGroundAgent:
    """一个 CraftGround 实例里的 agent。

    Attributes:
        name: 实例名，与控制器名单一致。
    """

    def __init__(
        self,
        name: str,
        environment: Any,
        adapter: CraftGroundActionAdapter,
        *,
        version: int = 2,
        underflow: UnderflowPolicy = UnderflowPolicy.NOOP,
    ) -> None:
        """初始化。

        Args:
            name: 实例名。
            environment: CraftGround 环境对象。
            adapter: 动作适配器。
            version: 协议版本，`1` 或 `2`。
            underflow: 动作缓存为空时的策略。
        """
        self.name = name
        self._environment = environment
        self._adapter = adapter
        self._queue = ActionQueue(version=version, underflow=underflow)  # type: ignore[arg-type]
        self._latest_raw: dict[str, Any] | None = None
        self._latest_info: dict[str, Any] = {}
        self._state = AgentState.WAIT
        self._observation_callbacks: list[Callable[[dict[str, Any]], None]] = []
        self._done_callbacks: list[Callable[[], None]] = []
        # 同步窗口标记。不能用 `_state is WAIT` 代替：初始状态本身就是 WAIT，那样会让
        # 状态永远无法离开 WAIT，记录器也就永远等不到第一个 alive。
        self._settling = False
        self._last_action_text = ""
        self._last_backend_action: dict[str, bool | float] = {}
        self._episode_reward = 0.0

    # --- 协议：Agent 级 ---

    def upload(self, actions: Sequence[Tick]) -> int:
        """上传多个 tick 的动作作为未来缓存。

        Args:
            actions: 待执行的 tick 序列，下标即相对首 tick 的偏移。

        Returns:
            该段首 tick 在本回合内的相对序号。完整结果见 `last_submission()`。

        Raises:
            ValueError: `actions` 为空或版本与控制器声明不一致。
        """
        result = self._queue.submit(list(actions))
        return result.start_tick

    def last_submission(self) -> SubmitResult | None:
        """取最近一次 `upload` 的完整结果。

        协议的 `upload` 只返回 `int`，无法表达覆盖与过期；这个补充入口不改变协议签名，
        供优化器判断提交是否被削减。
        """
        return self._queue.last_submission

    def stop_action(self) -> None:
        """清空未来动作缓存，不回退已执行动作。"""
        self._queue.stop()

    def agent_observe(self) -> dict[str, Any]:
        """返回最新观察，只含 `rgb` 与 `state`。

        Raises:
            RuntimeError: 环境尚未产生观察。
        """
        return self.observation().as_dict()

    def observation(self) -> Observation:
        """返回结构化的最新观察。

        Raises:
            RuntimeError: 环境尚未产生观察。
        """
        if self._latest_raw is None:
            raise RuntimeError(f"agent {self.name!r} 尚未产生观察，需要先 reset")
        return Observation(
            rgb=self._extract_rgb(self._latest_raw),
            tick_index=self.current_tick,
            state=self._state,
        )

    def reborn(self) -> None:
        """重置 agent 自身状态并重生，不改变世界状态。

        CraftGround 是单人环境，没有独立的玩家重生通道，因此用特权命令让玩家复活并回到
        重生点。这不改变世界方块，符合协议对 `reborn` 的界定。
        """
        for command in ("gamemode survival", "effect clear", "kill @s"):
            self._environment.add_command(command)
        self._state = AgentState.WAIT

    def agent_info(self) -> dict[str, Any]:
        """返回全部真值。harness 通道，reward、done、坐标都在这里。"""
        return dict(self._latest_info)

    def on_observation(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """订阅 `observation` 信号。每次观察更新时同步调用。"""
        self._observation_callbacks.append(callback)

    def on_done(self, callback: Callable[[], None]) -> None:
        """订阅 `done` 信号。每次 done 时调用一次。"""
        self._done_callbacks.append(callback)

    # --- 内部：由 Env 与控制器驱动 ---

    @property
    def current_tick(self) -> int:
        """下一个将被执行的相对 tick 序号。"""
        return self._queue.current_tick

    @property
    def state(self) -> AgentState:
        """当前生存状态。"""
        return self._state

    @property
    def last_action_text(self) -> str:
        """最近一个已执行 tick 的定长文本。"""
        return self._last_action_text

    @property
    def last_backend_action(self) -> dict[str, bool | float]:
        """返回最近一个已执行 tick 的后端扩展动作。"""
        return dict(self._last_backend_action)

    def advance(self, *, hotbar_slot: int | None = None) -> Observation:
        """执行一个 tick 并刷新观察。

        Returns:
            该 tick 的观察。

        Raises:
            RuntimeError: 动作缓存为空且下溢策略为 `WAIT`。
        """
        tick = self._queue.pull()
        self._last_action_text = format_tick(tick)
        self._last_backend_action = (
            {f"hotbar.{hotbar_slot}": True} if hotbar_slot is not None else {}
        )
        action = self._adapter.convert(tick, hotbar_slot=hotbar_slot)
        observation, reward, terminated, truncated, info = self._environment.step(action)
        self._episode_reward += float(reward)
        self._ingest(
            observation, info, reward=float(reward), terminated=terminated, truncated=truncated
        )
        return self.observation()

    def settle(self, ticks: int) -> None:
        """空跑若干 tick 且不消耗动作缓存，用于同步命令与加载地形。

        期间状态标记为 `wait`：这些帧是重置或命令落定期间的无用帧，协议规定记录器直接
        跳过、不补回。
        """
        from .action_queue import idle_tick

        self._settling = True
        self._state = AgentState.WAIT
        try:
            action = self._adapter.convert(idle_tick(self._queue.version))
            for _ in range(ticks):
                observation, reward, terminated, truncated, info = self._environment.step(action)
                self._ingest(
                    observation,
                    info,
                    reward=float(reward),
                    terminated=terminated,
                    truncated=truncated,
                )
        finally:
            self._settling = False
        # 同步窗口结束后立刻按最后一帧定状态，使调用方不必再多推一个 tick 才看到 alive。
        if self._latest_raw is not None:
            full = self._latest_raw.get("full") if isinstance(self._latest_raw, dict) else None
            info = self._latest_info
            self._state = self._resolve_state(
                full,
                terminated=bool(info.get("terminated")),
                truncated=bool(info.get("truncated")),
            )

    def begin_episode(self, observation: Any, info: Any) -> None:
        """接住一次 `reset` 的结果，开始新回合。"""
        self._queue.reset()
        self._adapter.reset()
        self._last_backend_action = {}
        self._episode_reward = 0.0
        self._ingest(observation, info, reward=0.0, terminated=False, truncated=False)

    def rewind_to(self, tick_index: int) -> None:
        """把动作缓存退回 `tick_index`，供 checkpoint 倒档使用。"""
        self._queue.rewind_to(tick_index)
        self._adapter.reset()

    def _ingest(
        self,
        observation: Any,
        info: Any,
        *,
        reward: float,
        terminated: bool,
        truncated: bool,
    ) -> None:
        """吸收一次环境返回，更新状态并发信号。

        同步窗口内不改状态（`_settling` 为真），但仍然发 `observation` 信号：协议规定
        wait 帧由记录器负责跳过，环境不替它做过滤。
        """
        self._latest_raw = observation
        full = observation.get("full") if isinstance(observation, dict) else None
        self._latest_info = {
            "reward": reward,
            "episode_reward": self._episode_reward,
            "terminated": bool(terminated),
            "truncated": bool(truncated),
            "tick_index": self._queue.current_tick,
            "raw_info": info,
        }
        if full is not None:
            self._latest_info.update(
                {
                    "x": getattr(full, "x", None),
                    "y": getattr(full, "y", None),
                    "z": getattr(full, "z", None),
                    "yaw": getattr(full, "yaw", None),
                    "pitch": getattr(full, "pitch", None),
                    "health": getattr(full, "health", None),
                    "is_dead": getattr(full, "is_dead", None),
                    # 协议规定 agent_info 是「全部真值」，因此把整个观察消息挂上来。
                    # 只摘几个标量会让背包、视线方块、周围方块这些字段对调用方不可见——
                    # 任务判据往往正落在这些字段上。`raw_info` 是 gym 的 info，与它不同。
                    "full": full,
                    "inventory": [
                        {
                            "translation_key": getattr(item, "translation_key", ""),
                            "count": int(getattr(item, "count", 0) or 0),
                            "raw_id": int(getattr(item, "raw_id", 0) or 0),
                        }
                        for item in (getattr(full, "inventory", None) or [])
                        if int(getattr(item, "count", 0) or 0) > 0
                    ],
                }
            )

        if not self._settling:
            self._state = self._resolve_state(full, terminated=terminated, truncated=truncated)
        payload = {
            "rgb": self._extract_rgb(observation),
            "state": self._state.value,
        }
        for callback in self._observation_callbacks:
            callback(payload)
        if self._state.ends_episode:
            for callback in self._done_callbacks:
                callback()

    def _resolve_state(self, full: Any, *, terminated: bool, truncated: bool) -> AgentState:
        """按环境返回判定生存状态。

        判定顺序是 truncated、terminated、死亡、存活。`die` 取自玩家 `is_dead`：协议
        规定 die 不结束序列、只跳过死亡期间，因此它不能与 terminated 混同。
        """
        if truncated:
            return AgentState.TRUNCATED
        if terminated:
            return AgentState.DONE
        if bool(getattr(full, "is_dead", False)):
            return AgentState.DIE
        return AgentState.ALIVE

    @staticmethod
    def _extract_rgb(observation: Any) -> np.ndarray:
        """抽出协议唯一允许的单个 ``(H, W, 3)`` RGB 画面。"""
        if not isinstance(observation, dict) or observation.get("rgb") is None:
            raise RuntimeError("CraftGround 观察里没有 rgb 字段")
        frame = np.asarray(observation["rgb"], dtype=np.uint8)
        if frame.ndim != 3 or frame.shape[-1] != 3:
            raise RuntimeError(f"CraftGround rgb 形状非法：{frame.shape}")
        return frame
