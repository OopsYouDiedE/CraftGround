"""CraftGround 的两级 checkpoint。

协议要求两种强度不同的保存：`set_checkpoint`/`rewind_checkpoint` 是回合内的高频热回档，
`save_and_checkpoint`/`restore` 要求「能够在任何实例中恢复」。两者能力边界正交，因此用
两种载体实现：

| 协议函数 | 载体 | 实测代价 |
| --- | --- | --- |
| `set_checkpoint` / `rewind_checkpoint` | `memorysnapshot` 内存快照 | 约 160 ms |
| `save_and_checkpoint` / `restore` | 磁盘存档目录 | 需重启 JVM |

内存快照是 JVM 内的 `StructureTemplate` 加玩家 NBT，跨进程不可移植，但免重启、免 GUI；
磁盘存档能跨实例，但 CraftGround 只在启动期通过 `level_display_name_to_play` 进世界，
运行期换存档要重建 JVM。

**内存快照的不完整性**（本机实测与源码确认）：只覆盖指定长方体区域内的方块与该区域内的
实体，加上玩家自身 NBT 与状态效果。不回滚世界时间、天气、随机数序列、区域外实体与掉落物、
维度。这对「同一 checkpoint 下各版本即兄弟分支」的可比性构成直接威胁，因此建世界时必须用
gamerule 把昼夜、天气、随机 tick 与生物生成钉死，见 `DETERMINISM_COMMANDS`。
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

_SNAPSHOT_ID = re.compile(r"^[A-Za-z0-9_.-]+$")

#: 倒档命令落定所需的同步 tick 数。
#:
#: runtime 的 `memorysnapshot load` 把结构放置、玩家 NBT 读回和 `requestTeleport` 全部
#: 排进 `server.execute {}`，因此命令返回时世界还没恢复。本机实测：1 与 2 个 tick 会拿到
#: 恢复途中的中间状态（玩家坐标漂移 12 格以上），4 个及以上才逐位精确还原。取 6 留一倍
#: 余量——单 tick 约 7 ms，多两个 tick 的代价远小于拿到错误快照画面的代价。
DEFAULT_SETTLE_TICKS = 6

#: `reset` 返回后 Minecraft 仍停在加载地形，需要继续 tick 才推进到可玩画面。
#: 本机实测：`reset` 返回后首个 step 耗时 3.3 s（地形加载），30 个 tick 后稳定在 7 ms。
DEFAULT_WARMUP_TICKS = 30

#: 保存快照前静置玩家所需的最多 tick 数。
#:
#: `memorysnapshot save` 把 `saveFromWorld` 与玩家 NBT 读取排进 `server.execute {}`，
#: 因此实际捕获发生在命令后的第一两个 tick。若此时玩家还带着移动惯性，捕获到的状态与
#: `set_checkpoint` 返回时的状态就不是同一个——本机实测走路后直接存档，倒档回到的位置比
#: 存档返回时的位置差 1.44 格。先静置到位置稳定再存，两者才一致。
DEFAULT_QUIESCE_TICKS = 12
#: 判定「已静置」的位置变化阈值，格。Minecraft 的移动摩擦是指数衰减，不会精确归零。
QUIESCE_EPSILON = 1e-4

#: 让同一 checkpoint 的多次重跑可比所必需的 gamerule。
#:
#: 内存快照不回滚世界时间、天气与随机 tick，因此必须先把这些自由度钉死，否则同一
#: checkpoint 的两次重跑落在不同的世界条件下，评估器打的分不能横向比较。这不是优化项，
#: 是可比性的前置条件。
DETERMINISM_COMMANDS = (
    "gamerule doDaylightCycle false",
    "gamerule doWeatherCycle false",
    "gamerule randomTickSpeed 0",
    "gamerule doMobSpawning false",
    "gamerule doFireTick false",
    "gamerule mobGriefing false",
    "gamerule doPatrolSpawning false",
    "gamerule doTraderSpawning false",
    "gamerule doInsomnia false",
    "time set noon",
    "weather clear 1000000",
)


class CommandEnvironment(Protocol):
    """checkpoint 机制所需的最小 CraftGround 接口。"""

    def add_command(self, command: str) -> None: ...

    def step(self, action: Any) -> tuple[Any, ...]: ...


@dataclass(frozen=True, slots=True)
class SnapshotRegion:
    """内存快照覆盖的长方体区域。

    Attributes:
        minimum: 区域最小角，含端点。
        maximum: 区域最大角，含端点。
    """

    minimum: tuple[int, int, int]
    maximum: tuple[int, int, int]

    def __post_init__(self) -> None:
        """校验区域非空。"""
        if any(low > high for low, high in zip(self.minimum, self.maximum, strict=True)):
            raise ValueError("快照区域 minimum 不能大于 maximum")

    def command_coordinates(self) -> str:
        """写成 `memorysnapshot save` 命令所需的六个坐标。"""
        return " ".join(str(value) for value in (*self.minimum, *self.maximum))

    @property
    def block_count(self) -> int:
        """区域内的方块数。区域越大保存越慢，用它来判断代价。"""
        return _volume(self.minimum, self.maximum)

    @classmethod
    def around_player(
        cls,
        position: tuple[float, float, float],
        *,
        horizontal_radius: int = 24,
        minimum_y: int = -64,
        maximum_y: int = 319,
    ) -> SnapshotRegion:
        """按玩家坐标计算快照区域。

        `horizontal_radius` 默认 24，比 `render_distance=2` 对应的 32 格视距略小，
        保证快照区域完全落在已加载区块内。

        Args:
            position: 玩家的 `(x, y, z)`。
            horizontal_radius: 水平半径，格。
            minimum_y: 区域底部高度。默认取 1.21 的世界底。
            maximum_y: 区域顶部高度。默认取 1.21 的世界顶。

        Raises:
            ValueError: `horizontal_radius` 不为正。
        """
        if horizontal_radius < 1:
            raise ValueError(f"horizontal_radius 必须为正，实际 {horizontal_radius}")
        center_x = int(position[0] // 1)
        center_z = int(position[2] // 1)
        return cls(
            (center_x - horizontal_radius, minimum_y, center_z - horizontal_radius),
            (center_x + horizontal_radius, maximum_y, center_z + horizontal_radius),
        )


def _volume(minimum: tuple[int, int, int], maximum: tuple[int, int, int]) -> int:
    """返回闭区间长方体的方块数。"""
    return (
        (maximum[0] - minimum[0] + 1)
        * (maximum[1] - minimum[1] + 1)
        * (maximum[2] - minimum[2] + 1)
    )


@dataclass(frozen=True, slots=True)
class MemoryCheckpoint:
    """一个已保存的内存快照。

    Attributes:
        snapshot_id: 快照 ID。只允许字母、数字、点、下划线与连字符——它直接拼进
            `memorysnapshot` 命令文本，含空格会被命令解析切碎。
        region: 快照覆盖的区域。
        tick_index: 保存时的相对 tick，供倒档时同步回退动作队列指针。
    """

    snapshot_id: str
    region: SnapshotRegion
    tick_index: int = 0

    def __post_init__(self) -> None:
        """校验快照 ID 的字符集。"""
        if not _SNAPSHOT_ID.fullmatch(self.snapshot_id):
            raise ValueError(
                f"snapshot_id 只能包含字母、数字、点、下划线和连字符，实际 {self.snapshot_id!r}"
            )


class MemoryCheckpointStore:
    """驱动单个 CraftGround 实例的内存快照存取。

    命令是异步的：`add_command` 只入队，真正执行发生在后续的 `step` 里，而 runtime 又把
    实际工作排进 `server.execute `。因此每条命令后都要空跑若干 tick 让它落定。
    """

    def __init__(
        self,
        environment: CommandEnvironment,
        settle: Callable[[int], None],
        *,
        settle_ticks: int = DEFAULT_SETTLE_TICKS,
    ) -> None:
        """初始化。

        Args:
            environment: 目标 CraftGround 实例。
            settle: 同步 tick 的执行者，入参是 tick 数。**必须**是会更新调用方观察缓存的
                那条路径（即 agent 的 settle），不能直接 `environment.step`：命令的同步
                tick 若走裸环境，agent 缓存的观察与真值仍是命令生效之前那一帧，倒档后
                `agent_info()` 会返回旧坐标，表现为「倒档看起来没生效」。
            settle_ticks: 每条命令后的同步 tick 数。

        Raises:
            ValueError: `settle_ticks` 不为正。
        """
        if settle_ticks < 1:
            raise ValueError(f"settle_ticks 必须为正，实际 {settle_ticks}")
        self._environment = environment
        self._settle = settle
        self._settle_ticks = settle_ticks

    def save(self, checkpoint: MemoryCheckpoint) -> float:
        """保存内存快照，返回耗时毫秒。"""
        return self._run(
            f"memorysnapshot save {checkpoint.snapshot_id} "
            f"{checkpoint.region.command_coordinates()}"
        )

    def load(self, checkpoint: MemoryCheckpoint) -> float:
        """恢复内存快照，返回耗时毫秒。"""
        return self._run(f"memorysnapshot load {checkpoint.snapshot_id}")

    def run_commands(self, commands: tuple[str, ...] | list[str]) -> float:
        """批量执行特权命令，返回总耗时毫秒。"""
        started = time.perf_counter()
        for command in commands:
            self._environment.add_command(command)
        self._settle(self._settle_ticks)
        return (time.perf_counter() - started) * 1000.0

    def _run(self, command: str) -> float:
        started = time.perf_counter()
        self._environment.add_command(command)
        self._settle(self._settle_ticks)
        return (time.perf_counter() - started) * 1000.0


def set_level_display_name(level_dat: Path | str, display_name: str) -> str:
    """改写 `level.dat` 里的 `LevelName`，返回原来的值。

    这一步是 `restore` 能工作的前提。runtime 的 `enterExistingWorldUsingGUI` 在世界选择
    界面上按 **显示名** 匹配（`WorldEntry.levelDisplayName == levelDisplayName`），而显示名
    存在 `level.dat` 的 `LevelName` 里，与目录名无关。直接把存档复制成新目录名不改显示名，
    会让 runtime 永远匹配不到，表现为在世界选择界面无限循环打印
    `Level display name: New World != TaoWorld`。

    实现上只在 gzip 解压后的 NBT 字节流里替换那一个字符串标签，不引入 NBT 库：`LevelName`
    是 `TAG_String`，布局为 `0x08` + 名字长度 + `"LevelName"` + 值长度 + 值，就地替换长度
    与值即可，其余字节不动。

    Args:
        level_dat: `level.dat` 路径。
        display_name: 新的显示名。

    Returns:
        原来的显示名。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 文件里找不到 `LevelName` 标签，或新名字超出 NBT 字符串长度上限。
    """
    import gzip

    path = Path(level_dat).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"level.dat 不存在：{path}")
    encoded = display_name.encode("utf-8")
    if len(encoded) > 0xFFFF:
        raise ValueError(f"显示名过长：{len(encoded)} 字节")

    raw = gzip.decompress(path.read_bytes())
    key = b"LevelName"
    index = raw.find(b"\x08" + len(key).to_bytes(2, "big") + key)
    if index < 0:
        raise ValueError(f"level.dat 里找不到 LevelName 标签：{path}")
    value_start = index + 1 + 2 + len(key)
    value_length = int.from_bytes(raw[value_start : value_start + 2], "big")
    previous = raw[value_start + 2 : value_start + 2 + value_length].decode(
        "utf-8", errors="replace"
    )
    patched = (
        raw[:value_start]
        + len(encoded).to_bytes(2, "big")
        + encoded
        + raw[value_start + 2 + value_length :]
    )
    path.write_bytes(gzip.compress(patched))
    return previous


def install_world_save(
    source: Path | str,
    runtime_path: Path | str,
    *,
    world_directory_name: str = "New World",
    overwrite: bool = False,
    rename_display: bool = True,
) -> dict[str, str]:
    """把磁盘存档复制到 CraftGround 实例的存档目录。

    这是 `restore(path)` 的落地方式：CraftGround 只在启动期通过
    `level_display_name_to_play` 选择世界，因此换存档必须在 JVM 启动前把目录放好。

    Args:
        source: 源存档目录，必须含 `level.dat`。
        runtime_path: CraftGround 实例根目录。
        world_directory_name: 目标存档目录名，同时也是 `level_display_name_to_play`。
        overwrite: 目标已存在时是否覆盖。
        rename_display: 是否把 `level.dat` 的显示名改成 `world_directory_name`。
            runtime 按显示名而非目录名匹配世界，因此默认为真；为假时调用方必须自己
            保证显示名与传给 runtime 的名字一致。

    Returns:
        安装事实，含源路径、目标路径、目录名与原显示名，供 `env_info` 引用。

    Raises:
        FileNotFoundError: 源目录缺少 `level.dat`。
        ValueError: `world_directory_name` 含不安全字符。
        FileExistsError: 目标已存在且 `overwrite` 为假。
    """
    import shutil

    source_path = Path(source).expanduser().resolve()
    if not (source_path / "level.dat").is_file():
        raise FileNotFoundError(f"存档缺少 level.dat：{source_path}")
    if not re.fullmatch(r"[A-Za-z0-9_. -]+", world_directory_name):
        raise ValueError(f"world_directory_name 含不安全字符：{world_directory_name!r}")
    destination = Path(runtime_path).expanduser().resolve() / "run" / "saves"
    destination = destination / world_directory_name
    if destination.exists():
        if not overwrite:
            raise FileExistsError(f"实例存档目录已存在：{destination}")
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_path, destination, ignore=shutil.ignore_patterns("session.lock"))
    previous = ""
    if rename_display:
        previous = set_level_display_name(destination / "level.dat", world_directory_name)
    return {
        "source_path": str(source_path),
        "instance_world_path": str(destination),
        "world_directory_name": world_directory_name,
        "previous_display_name": previous,
    }


def export_world_save(
    runtime_path: Path | str,
    destination: Path | str,
    *,
    world_directory_name: str = "New World",
    overwrite: bool = False,
) -> dict[str, str]:
    """把实例的存档目录复制出去，作为 `save_and_checkpoint` 的落盘产物。

    调用方必须先关闭 JVM——Minecraft 只在退出世界时把区块与 `level.dat` 完整落盘。
    本机实测确认 `save-all` 命令透传不生效（`saves/` 目录字节数无变化），因为
    `SaveAllCommand` 在单人整合服里不注册。

    Args:
        runtime_path: CraftGround 实例根目录。
        destination: 目标存档目录。
        world_directory_name: 实例内的存档目录名。
        overwrite: 目标已存在时是否覆盖。

    Returns:
        导出事实，含源路径、目标路径与文件数。

    Raises:
        FileNotFoundError: 实例内找不到该存档目录，或其中缺少 `level.dat`。
        FileExistsError: 目标已存在且 `overwrite` 为假。
    """
    import shutil

    source = Path(runtime_path).expanduser().resolve() / "run" / "saves"
    source = source / world_directory_name
    if not source.is_dir():
        raise FileNotFoundError(f"实例存档目录不存在：{source}")
    if not (source / "level.dat").is_file():
        raise FileNotFoundError(f"实例存档缺少 level.dat：{source}")
    target = Path(destination).expanduser().resolve()
    if target.exists():
        if not overwrite:
            raise FileExistsError(f"导出目标已存在：{target}")
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("session.lock"))
    files = [path for path in target.rglob("*") if path.is_file()]
    return {
        "source_path": str(source),
        "export_path": str(target),
        "file_count": str(len(files)),
        "byte_count": str(sum(path.stat().st_size for path in files)),
    }
