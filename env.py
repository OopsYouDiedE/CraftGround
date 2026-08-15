"""CraftGround 的 Env 级实现。

一个 `CraftGroundEnv` 持有一个 CraftGround 实例与其上的单个 agent。checkpoint 分两级，
分工见 `checkpoints` 模块：`set_checkpoint`/`rewind_checkpoint` 走内存快照，
`save_and_checkpoint`/`restore` 走磁盘存档。

`restore(path)` 必须重建 JVM：CraftGround 只在启动期通过 `level_display_name_to_play`
选择世界，运行期没有换存档的通道。因此它是本类里唯一昂贵的操作，实测约 20 s。
"""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from shared_tools.action_conversion import Tick
from shared_tools.environment_protocol import AgentState, UnderflowPolicy

from .action_adapter import CraftGroundActionAdapter
from .agent import CraftGroundAgent
from .checkpoints import (
    DEFAULT_QUIESCE_TICKS,
    DEFAULT_SETTLE_TICKS,
    DEFAULT_WARMUP_TICKS,
    DETERMINISM_COMMANDS,
    QUIESCE_EPSILON,
    MemoryCheckpoint,
    MemoryCheckpointStore,
    SnapshotRegion,
    export_world_save,
    install_world_save,
)
from .key_mapping import KeyMapping
from .runtime import create_environment, prepare_runtime_instance

#: 内存快照的默认水平半径，格。
DEFAULT_SNAPSHOT_RADIUS = 24
MAX_INITIAL_FRAME_ATTEMPTS = 120
#: 磁盘存档在实例内的目录名。同时也是 `level_display_name_to_play` 的取值。
WORLD_DIRECTORY_NAME = "TaoWorld"


class CraftGroundEnv:
    """一个 CraftGround 环境实例。

    Attributes:
        name: 实例名，与 agent 同名。
    """

    def __init__(
        self,
        name: str,
        mapping: KeyMapping,
        *,
        runtime_path: Path,
        port: int,
        version: int = 2,
        underflow: UnderflowPolicy = UnderflowPolicy.NOOP,
        settle_ticks: int = DEFAULT_SETTLE_TICKS,
        warmup_ticks: int = DEFAULT_WARMUP_TICKS,
        snapshot_radius: int = DEFAULT_SNAPSHOT_RADIUS,
        world_type: str = "SUPERFLAT",
        determinism_commands: tuple[str, ...] = DETERMINISM_COMMANDS,
        environment_options: dict[str, Any] | None = None,
        archive_root: Path | str = "trajectories/environments",
    ) -> None:
        """初始化并启动 JVM。

        Args:
            name: 实例名。
            mapping: 键位映射表。
            runtime_path: CraftGround 实例目录。
            port: IPC 端口。
            version: 协议版本。
            underflow: 动作缓存为空时的策略。
            settle_ticks: 特权命令后的同步 tick 数。
            warmup_ticks: `reset` 后让地形加载完成的 tick 数。
            snapshot_radius: 内存快照的水平半径。
            world_type: 世界类型。默认 `SUPERFLAT`，实测省约 4.7 s。
            determinism_commands: 进世界后钉死随机性的命令。内存快照不回滚世界时间与
                天气，不钉死会让同一 checkpoint 的多次重跑不可比。
            environment_options: 传给 `create_environment` 的其余参数。
        """
        self.name = name
        self._runtime_path = runtime_path
        self._port = port
        self._version = version
        self._underflow = underflow
        self._settle_ticks = settle_ticks
        self._warmup_ticks = warmup_ticks
        self._snapshot_radius = snapshot_radius
        self._world_type = world_type
        self._determinism_commands = tuple(determinism_commands)
        self._environment_options = dict(environment_options or {})
        self._archive_root = Path(archive_root).expanduser().resolve()
        self._save_sequence = 0
        self._restore_source: Path | None = None
        self._closed = False
        self._checkpoint: MemoryCheckpoint | None = None
        self._checkpoints: list[MemoryCheckpoint] = []
        self._disk_checkpoint: Path | None = None
        self._timings: dict[str, float] = {}
        self._subscribers: dict[str, list[Callable[..., None]]] = {
            name: [] for name in ("observation", "record", "checkpoint", "rewind", "end_record")
        }
        self._ended = False

        self._environment = self._launch()
        self._adapter = CraftGroundActionAdapter(mapping=mapping)
        self.agent = CraftGroundAgent(
            name, self._environment, self._adapter, version=version, underflow=underflow
        )
        self._store = MemoryCheckpointStore(
            self._environment,
            self._settle_through_agent,
            settle_ticks=settle_ticks,
        )

    def _quiesce(self) -> int:
        """空跑直到玩家位置不再变化，返回实际用掉的 tick 数。

        Minecraft 的移动摩擦是指数衰减，位置不会精确归零，因此用阈值判定而非等于。
        超过 `DEFAULT_QUIESCE_TICKS` 仍在动就放弃等待——可能是在下落或被水推动，此时
        继续等也不会稳定，让调用方拿到一个位置一致但仍在运动的 checkpoint 更有用。
        """
        previous: tuple[float, float, float] | None = None
        for used in range(DEFAULT_QUIESCE_TICKS):
            info = self.agent.agent_info()
            current = (info.get("x") or 0.0, info.get("y") or 0.0, info.get("z") or 0.0)
            if previous is not None:
                moved = sum(
                    abs(now - before) for now, before in zip(current, previous, strict=True)
                )
                if moved < QUIESCE_EPSILON:
                    return used
            previous = current
            self.agent.settle(1)
        return DEFAULT_QUIESCE_TICKS

    def _settle_through_agent(self, ticks: int) -> None:
        """走 agent 的同步路径推进若干 tick。

        必须经过 agent 而不是直接 `environment.step`：命令的同步 tick 若走裸环境，agent
        缓存的观察与 `agent_info()` 仍是命令生效之前那一帧，倒档后读到的坐标是旧值，
        表现为「倒档看起来没生效」。
        """
        self.agent.settle(ticks)

    def _launch(self) -> Any:
        """按当前基点启动一个 CraftGround 实例。"""
        level_name = WORLD_DIRECTORY_NAME if self._restore_source is not None else ""
        return create_environment(
            self._runtime_path,
            port=self._port,
            world_type=self._world_type,
            initial_extra_commands=self._determinism_commands,
            level_display_name_to_play=level_name,
            **self._environment_options,
        )

    # --- 协议：Env 级 ---

    def id(self) -> str:
        """返回当前 Env 的稳定名称。"""
        return self.name

    def get_agent_id(self) -> list[str]:
        """返回固定 Agent 槽位名称。"""
        return [f"{self.name}/{self.agent.name}"]

    def get_action_protocol(self) -> str:
        """返回简化动作协议版本。"""
        if self._version == 1:
            return "tao-gamepad"
        return "tao-touch"

    def get_supported_action_protocols(self) -> tuple[str, ...]:
        """返回环境可执行的逐 tick 协议集合。"""
        return (self.get_action_protocol(),)

    def reset(self) -> list[dict[str, Any]]:
        """重置环境并恢复角色状态，开始新回合。

        有磁盘基点时回到该基点，否则让 CraftGround 重开世界。两种情况都在返回前空跑
        `warmup_ticks`——`reset` 返回时 Minecraft 仍停在加载地形，实测首个 step 耗时
        3.3 s，不预热会把加载界面当成第一帧交给调用方。
        """
        self._require_open()
        started = time.perf_counter()
        observation, info = self._reset_until_image()
        self.agent.begin_episode(observation, info)
        if self._determinism_commands:
            self._store.run_commands(self._determinism_commands)
        self.agent.settle(self._warmup_ticks)
        self._checkpoints.clear()
        self._ended = False
        checkpoint_index = self.checkpoint()
        if checkpoint_index != 0:
            raise RuntimeError("reset 后初始 checkpoint 编号必须为 0")
        self._timings["reset_ms"] = (time.perf_counter() - started) * 1000.0
        observation_dict = self.agent.agent_observe()
        self._emit_observation(observation_dict)
        return [observation_dict]

    def _reset_until_image(self) -> tuple[Any, Any]:
        """跳过 CraftGround 启动握手中的空 dummy observation，取得首个真实 RGB 帧。"""
        try:
            return self._environment.reset(options={"fast_reset": True})
        except ValueError as error:
            if "cannot reshape array of size 0" not in str(error):
                raise
        from craftground.environment.action_space import no_op_v2

        for _ in range(MAX_INITIAL_FRAME_ATTEMPTS):
            try:
                observation, _, _, _, info = self._environment.step(no_op_v2())
            except ValueError as error:
                if "cannot reshape array of size 0" in str(error):
                    continue
                raise
            return observation, info
        raise RuntimeError(
            f"CraftGround 连续 {MAX_INITIAL_FRAME_ATTEMPTS} 次只返回空 dummy observation"
        )

    def observe(self) -> list[dict[str, Any]]:
        """不推进 tick，返回最新 Observation。"""
        self._require_open()
        return [self.agent.agent_observe()]

    def step(self, actions: list[Tick | None]) -> list[dict[str, Any]]:
        """覆盖下一帧动作、推进一个 tick 并发送统一信号。"""
        return self._step(actions, hotbar_slot=None)

    def _step(self, actions: list[Tick | None], *, hotbar_slot: int | None) -> list[dict[str, Any]]:
        """按统一时序执行一次动作，可附带受限的后端快捷栏扩展。"""
        self._require_open()
        if len(actions) != 1:
            raise ValueError(
                f"CraftGround 只有 1 个 Agent，actions 长度必须为 1，实际 {len(actions)}"
            )
        if self._ended:
            return self.observe()
        if actions[0] is not None:
            self.agent.upload([actions[0]])
        self.agent.advance(hotbar_slot=hotbar_slot)
        observation = self.agent.agent_observe()
        self._emit_observation(observation)
        return [observation]

    def step_with_hotbar(
        self, actions: list[Tick | None], *, hotbar_slot: int | None
    ) -> list[dict[str, Any]]:
        """推进单步，并把 VPT 的 1 到 9 格快捷栏选择直通到 CraftGround。

        这是 v1 协议之外的受限后端扩展。动作仍按 ``upload -> advance -> observation``
        的标准顺序执行；仅快捷栏槽号绕过无法表达第 5 到 9 格的位串。
        """
        return self._step(actions, hotbar_slot=hotbar_slot)

    def upload(self, agent_index: int, actions: Sequence[Tick]) -> int:
        """从当前待执行帧上传动作序列。"""
        self._require_agent(agent_index)
        if self.agent.state is AgentState.DIE:
            return self.agent.current_tick
        return self.agent.upload(actions)

    def stop_action(self, agent_index: int) -> None:
        """从当前待执行帧开始清空指定 Agent 的未来动作。"""
        self._require_agent(agent_index)
        self.agent.stop_action()

    def reborn(self, agent_index: int) -> None:
        """使指定 Agent 重生。"""
        self._require_agent(agent_index)
        self.agent.reborn()

    def subscribe(self, signal_name: str, callback: Callable[..., None]) -> None:
        """订阅同步环境信号。"""
        if signal_name not in self._subscribers:
            raise ValueError(f"未知信号 {signal_name!r}")
        if callback not in self._subscribers[signal_name]:
            self._subscribers[signal_name].append(callback)

    def unsubscribe(self, signal_name: str, callback: Callable[..., None]) -> None:
        """取消环境信号订阅。"""
        if signal_name not in self._subscribers:
            raise ValueError(f"未知信号 {signal_name!r}")
        with contextlib.suppress(ValueError):
            self._subscribers[signal_name].remove(callback)

    def _emit(self, signal_name: str, *payload: Any) -> None:
        """按订阅顺序通知全部回调，并在通知完后报告异常。"""
        errors: list[str] = []
        for callback in tuple(self._subscribers[signal_name]):
            try:
                callback(*payload)
            except Exception as error:
                errors.append(f"{callback!r}: {error}")
        if errors:
            raise RuntimeError("environment_error：信号回调失败；" + "；".join(errors))

    def _emit_observation(self, observation: dict[str, Any]) -> None:
        """按状态发送 observation、record 和唯一 end_record。"""
        state = AgentState(observation["state"])
        tick_index = int(observation["tick_index"])
        full_info = {
            **self.agent.agent_info(),
            "observation": observation,
            "action": self.agent.last_action_text,
            "backend_action": self.agent.last_backend_action,
        }
        if state.records_observation:
            self._emit("observation", 0, tick_index, observation)
        if state.records_frame:
            self._emit("record", 0, tick_index, full_info)
        if state.ends_episode and not self._ended:
            self._ended = True
            self._emit("end_record", 0, tick_index, state.value)

    def checkpoint(self) -> int:
        """将当前世界状态保存为 checkpoint。

        用内存快照，实测约 160 ms。快照区域按玩家当前坐标计算，覆盖水平半径
        `snapshot_radius`、垂直整个世界高度。

        存档前先静置玩家。`memorysnapshot save` 把捕获排进 `server.execute {}`，实际
        捕获发生在命令后的第一两个 tick；若此时玩家还带着移动惯性，捕获到的状态与本函数
        返回时的状态不是同一个，倒档后就会落在一个「既不是存档点也不是当前点」的位置上。
        本机实测这个偏差是 1.44 格。
        """
        self._require_open()
        self._quiesce()
        info = self.agent.agent_info()
        position = (info.get("x") or 0.0, info.get("y") or 0.0, info.get("z") or 0.0)
        region = SnapshotRegion.around_player(position, horizontal_radius=self._snapshot_radius)
        checkpoint = MemoryCheckpoint(
            snapshot_id=f"ckpt-{uuid.uuid4().hex[:12]}",
            region=region,
            tick_index=self.agent.current_tick,
        )
        self._timings["set_checkpoint_ms"] = self._store.save(checkpoint)
        self._checkpoint = checkpoint
        self._checkpoints.append(checkpoint)
        checkpoint_index = len(self._checkpoints) - 1
        self._emit("checkpoint", 0, checkpoint_index, checkpoint.tick_index)
        return checkpoint_index

    def set_checkpoint(self) -> None:
        """旧名称兼容入口；新代码使用 ``checkpoint``。"""
        self.checkpoint()

    def rewind_checkpoint(self, checkpoint_index: int = -1) -> None:
        """回到上一个 checkpoint。

        Raises:
            RuntimeError: 尚未设置过 checkpoint。
        """
        self._require_open()
        if not self._checkpoints:
            raise RuntimeError("尚未设置 checkpoint，无法倒档")
        if checkpoint_index == -1:
            checkpoint_index = len(self._checkpoints) - 1
        if not 0 <= checkpoint_index < len(self._checkpoints):
            raise IndexError(f"checkpoint_index 越界：{checkpoint_index}")
        checkpoint = self._checkpoints[checkpoint_index]
        self._timings["rewind_checkpoint_ms"] = self._store.load(checkpoint)
        self.agent.rewind_to(checkpoint.tick_index)
        self._checkpoint = checkpoint
        self._ended = False
        self._emit("rewind", 0, checkpoint_index)

    def save(self, path: str | None = None) -> str:
        """保存当前状态到 `path`，并把默认 checkpoint 设到这里。

        协议要求该状态能在任何实例中恢复，因此必须落盘。Minecraft 只在退出世界时把区块
        与 `level.dat` 完整落盘（实测 `save-all` 命令透传不生效，因为 `SaveAllCommand`
        在单人整合服里不注册），所以这里先关 JVM、导出目录、再重启并回到该基点。

        Args:
            path: 导出目标目录。

        Raises:
            FileExistsError: 目标已存在。
        """
        self._require_open()
        started = time.perf_counter()
        if path is None:
            timestamp = time.strftime("%Y%m%dT%H%M%S", time.localtime())
            while True:
                target = self._archive_root / self.name / f"{timestamp}-{self._save_sequence:04d}"
                self._save_sequence += 1
                if not target.exists():
                    break
        else:
            target = Path(path).expanduser().resolve()
        world_name = self._current_world_directory()
        self._environment.close()
        try:
            export_world_save(
                self._runtime_path,
                target,
                world_directory_name=world_name,
                overwrite=False,
            )
        finally:
            self._restore_source = target
            self._reopen_from_disk(target)
        self._disk_checkpoint = target
        self._timings["save_and_checkpoint_ms"] = (time.perf_counter() - started) * 1000.0
        return str(target)

    def save_and_checkpoint(self, path: str) -> None:
        """旧名称兼容入口。"""
        self.save(path)

    def load(self, path: str) -> list[dict[str, Any]]:
        """加载 `path` 存档，并以此为 reset 基点。

        必须重建 JVM：CraftGround 只在启动期通过 `level_display_name_to_play` 进世界。

        Args:
            path: 存档目录，须含 `level.dat`。

        Raises:
            FileNotFoundError: 存档不存在或缺少 `level.dat`。
        """
        self._require_open()
        started = time.perf_counter()
        source = Path(path).expanduser().resolve()
        if not (source / "level.dat").is_file():
            raise FileNotFoundError(f"存档缺少 level.dat：{source}")
        self._environment.close()
        self._restore_source = source
        self._reopen_from_disk(source)
        self._disk_checkpoint = source
        self._timings["restore_ms"] = (time.perf_counter() - started) * 1000.0
        self._checkpoints.clear()
        self._ended = False
        self.checkpoint()
        observation = self.agent.agent_observe()
        self._emit_observation(observation)
        return [observation]

    def restore(self, path: str) -> None:
        """旧名称兼容入口。"""
        self.load(path)

    def send_command(self, agent_index: int, name: str, *args: str) -> str:
        """特权命令。返回后端回显，无回显为空串。

        CraftGround 的命令通道是单向的（`add_command` 只入队，runtime 不回传结果），
        因此回显恒为空串。协议允许这一点：「无回显为空串」。
        """
        self._require_open()
        self._require_agent(agent_index)
        command = " ".join((name, *args)).strip()
        if not command:
            raise ValueError("命令不能为空")
        self._store.run_commands((command,))
        return ""

    def env_info(self) -> dict[str, Any]:
        """返回本环境的运行状态。"""
        return {
            "name": self.name,
            "closed": self._closed,
            "port": self._port,
            "runtime_path": str(self._runtime_path),
            "world_type": self._world_type,
            "action_protocol": self.get_action_protocol(),
            "underflow_policy": self._underflow.value,
            "settle_ticks": self._settle_ticks,
            "warmup_ticks": self._warmup_ticks,
            "snapshot_radius": self._snapshot_radius,
            "current_tick": self.agent.current_tick,
            "agent_state": self.agent.state.value,
            "checkpoint": None if self._checkpoint is None else self._checkpoint.snapshot_id,
            "checkpoint_tick": None if self._checkpoint is None else self._checkpoint.tick_index,
            "disk_checkpoint": (
                None if self._disk_checkpoint is None else str(self._disk_checkpoint)
            ),
            "determinism_commands": list(self._determinism_commands),
            "unmapped_slots": list(self._adapter.unmapped_slots),
            "timings_ms": dict(self._timings),
        }

    def close(self) -> None:
        """关闭 JVM。可重复调用。"""
        if self._closed:
            return
        self._closed = True
        # 批量关闭路径不能因单个 JVM 关闭失败而中断其余实例。
        with contextlib.suppress(Exception):
            self._environment.close()

    def truncate(self) -> None:
        """为活动回合生成唯一 ``truncated`` 终止帧。"""
        if self._closed or self._ended:
            return
        try:
            observation = self.agent.agent_observe()
        except RuntimeError as error:
            if "尚未产生观察" in str(error):
                self._ended = True
                return
            raise
        observation["state"] = AgentState.TRUNCATED.value
        self._emit_observation(observation)

    # --- 内部 ---

    def _current_world_directory(self) -> str:
        """返回实例内当前使用的存档目录名。"""
        saves = self._runtime_path / "run" / "saves"
        if self._restore_source is not None:
            return WORLD_DIRECTORY_NAME
        if not saves.is_dir():
            raise FileNotFoundError(f"实例没有存档目录：{saves}")
        candidates = [child for child in saves.iterdir() if (child / "level.dat").is_file()]
        if not candidates:
            raise FileNotFoundError(f"实例存档目录里没有 level.dat：{saves}")
        # 取最近修改的那个：CraftGround 新建世界时会生成 "New World"、"New World (1)"…
        return max(candidates, key=lambda path: path.stat().st_mtime).name

    def _reopen_from_disk(self, source: Path) -> None:
        """按磁盘存档重建 JVM 并回到可玩状态。

        先清掉实例里其余存档：runtime 在世界选择界面按显示名匹配，遍历到第一个可选条目
        就进入。留着旧的 `New World` 会让匹配结果取决于列表顺序（按最后游玩时间排序），
        表现为偶发进错世界。
        """
        saves = self._runtime_path / "run" / "saves"
        if saves.is_dir():
            import shutil

            for child in saves.iterdir():
                if child.is_dir() and child.name != WORLD_DIRECTORY_NAME:
                    shutil.rmtree(child)
        install_world_save(
            source,
            self._runtime_path,
            world_directory_name=WORLD_DIRECTORY_NAME,
            overwrite=True,
        )
        self._environment = self._launch()
        self._adapter.reset()
        self.agent = CraftGroundAgent(
            self.name,
            self._environment,
            self._adapter,
            version=self._version,
            underflow=self._underflow,
        )
        self._store = MemoryCheckpointStore(
            self._environment, self._settle_through_agent, settle_ticks=self._settle_ticks
        )
        observation, info = self._environment.reset(options={"fast_reset": False})
        self.agent.begin_episode(observation, info)
        if self._determinism_commands:
            self._store.run_commands(self._determinism_commands)
        self.agent.settle(self._warmup_ticks)
        # 换基点后旧的内存快照 ID 属于已销毁的 JVM，必须失效。
        self._checkpoint = None

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError(f"环境 {self.name!r} 已关闭")

    @staticmethod
    def _require_agent(agent_index: int) -> None:
        if agent_index != 0:
            raise IndexError(f"CraftGround 只有 agent_index 0，实际 {agent_index}")


def build_env(
    name: str,
    mapping: KeyMapping,
    *,
    instance_id: str,
    port: int,
    template: Path | None = None,
    instances_root: Path | None = None,
    **options: Any,
) -> CraftGroundEnv:
    """准备实例目录并构造一个环境。

    Args:
        name: 实例名。
        mapping: 键位映射表。
        instance_id: 实例目录标识。
        port: IPC 端口。
        template: runtime 模板目录。
        instances_root: 实例根目录。
        **options: 传给 `CraftGroundEnv` 的其余参数。

    Returns:
        已启动的环境。
    """
    runtime_path = prepare_runtime_instance(
        instance_id, template=template, instances_root=instances_root
    )
    return CraftGroundEnv(name, mapping, runtime_path=runtime_path, port=port, **options)
