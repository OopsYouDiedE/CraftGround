"""CraftGround 维护版 runtime 的准备与环境创建。

模板与实例分离：模板是构建一次的只读目录（缓存键含源码 SHA-256，改源码重装即触发重建），
实例是从模板复制出的可独立运行副本。复制时必须剪掉 CMake 缓存——`CMakeCache.txt` 记录了
生成它时的绝对路径，换位置后 `configureCppProject` 会因源目录不符而失败。这一点本机实测
确认过。

启动耗时的实测事实（RTX 3070，Windows 11，640x360）：

| 阶段 | 耗时 | 受参数影响 |
| --- | --- | --- |
| 首次 Gradle 构建 | 约 100 s | 一次性，模板级 |
| JVM + Fabric + 资源加载 | 约 15 s | 否 |
| 世界生成（DEFAULT） | 约 5 s | `world_type` |
| 世界生成（SUPERFLAT） | 约 1 s | `world_type` |
| 稳态单 tick | 7 ms | `render_distance` |

`SUPERFLAT` 比 `DEFAULT` 省约 4.7 s（两轮配对实测），但只作为显式性能调试选项。
正式默认使用正常的 `DEFAULT` 世界。`render_distance` 只影响每帧渲染与地形加载，不影响
启动——实测 `render_distance=2` 与 `12` 的首帧耗时差异在噪声内。
"""

from __future__ import annotations

import hashlib
import importlib
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from threading import Lock
from typing import Any

_PREPARE_LOCK = Lock()
CRAFTGROUND_ACTION_SPACE = "V2_MINERL_HUMAN"
CRAFTGROUND_RUNTIME_VERSION = "0.1.0+tao.2"
CRAFTGROUND_REPOSITORY_ROOT = Path(__file__).resolve().parent
CRAFTGROUND_RUNTIME_SOURCE = CRAFTGROUND_REPOSITORY_ROOT / "minecraft" / "mc121"
_INSTANCE_MARKER = ".tao-runtime-instance"
_RUNTIME_BUILD_MARKER = ".tao-runtime-build"

#: 实例目录不需要的模板产物。
#:
#: CMake 缓存必须剪掉：`CMakeCache.txt` 记录生成它时的源目录绝对路径，换位置后
#: `configureCppProject` 会报「source does not match」并失败。存档与日志剪掉是为了让
#: 实例从干净状态起跑。
_INSTANCE_PRUNED_PATHS = (
    "CMakeCache.txt",
    "CMakeFiles",
    "_deps/glm-build",
    "_deps/glm-subbuild",
    "run/saves",
    "run/logs",
    "run/crash-reports",
)

#: `render_distance` 的最小合法值。
#:
#: MC 1.21 的 `GameOptions.viewDistance` 由 `ValidatingIntSliderCallbacks(2, 32)` 门控，
#: 下限是 2。CraftGround 的 `EnvironmentInitializer.setRenderDistance` 直接赋值不做
#: clamp，因此传 1 或 0 会被 Minecraft 静默丢弃并回落到默认值 12 —— 比传 2 慢得多。
MINIMUM_RENDER_DISTANCE = 2
#: `simulation_distance` 的最小合法值，由 `ValidatingIntSliderCallbacks(5, 32)` 门控。
MINIMUM_SIMULATION_DISTANCE = 5


def directory_sha256(path: Path | str) -> str:
    """计算目录内容的稳定 SHA-256，忽略 Minecraft 运行锁。"""
    root = Path(path).expanduser().resolve()
    digest = hashlib.sha256()
    for file_path in sorted(value for value in root.rglob("*") if value.is_file()):
        relative = file_path.relative_to(root)
        if relative.as_posix() == "session.lock":
            continue
        digest.update(relative.as_posix().encode("utf-8") + b"\0")
        digest.update(file_path.read_bytes())
    return digest.hexdigest()


def validate_maintained_runtime(runtime_path: Path | str) -> None:
    """校验 runtime 含自维护分支承诺的源码能力。

    把分支承诺的能力钉成可执行断言：装错包立刻报错，而不是跑到一半行为诡异。加新能力时
    同步加一条断言。

    Args:
        runtime_path: `craftground_runtime_mc121` 包目录或其副本。

    Raises:
        FileNotFoundError: 缺少维护版要求的文件。
        RuntimeError: 文件存在但关键实现不是维护版合同。
    """
    root = Path(runtime_path).expanduser().resolve()
    required = (
        "src/main/java/com/kyhsgeekcode/minecraftenv/MemorySnapshotStore.kt",
        "src/main/java/com/kyhsgeekcode/minecraftenv/MinecraftEnv.kt",
        "src/main/cpp/noboost_ipc.cpp",
        "src/main/cpp/CMakeLists.txt",
    )
    missing = [relative for relative in required if not (root / relative).is_file()]
    if missing:
        raise FileNotFoundError(f"CraftGround 维护版 runtime 缺少文件：{missing}")

    minecraft_env = (
        root / "src/main/java/com/kyhsgeekcode/minecraftenv/MinecraftEnv.kt"
    ).read_text(encoding="utf-8")
    native_ipc = (root / "src/main/cpp/noboost_ipc.cpp").read_text(encoding="utf-8")
    cmake = (root / "src/main/cpp/CMakeLists.txt").read_text(encoding="utf-8")
    contracts = {
        "内存快照命令分发": "MemorySnapshotStore.handle(command, client)" in minecraft_env,
        "观察共享内存按帧扩容": (
            "j2pRequiredSize" in native_ipc and "ftruncate(j2pFd" in native_ipc
        ),
        "runtime native 源码自包含": "CMAKE_CURRENT_LIST_DIR" in cmake,
    }
    failed = [name for name, satisfied in contracts.items() if not satisfied]
    if failed:
        raise RuntimeError(f"CraftGround runtime 不满足维护版合同：{failed}")


def prepare_runtime_template(target: Path | None = None, *, build: bool = True) -> Path:
    """复制并构建仓库内的维护版 runtime，不修改其中源码。

    Args:
        target: 模板目录。`None` 表示按版本与源码摘要放到 `~/.cache/tao/` 下。
        build: 是否执行 Gradle 构建。

    Returns:
        模板目录。

    Raises:
        FileNotFoundError: 找不到 runtime 源目录，或缺少 Gradle wrapper。
    """
    source = CRAFTGROUND_RUNTIME_SOURCE.resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"仓库内找不到 CraftGround runtime 源目录：{source}")
    validate_maintained_runtime(source)
    source_digest = directory_sha256(source)

    if target is None:
        safe_version = re.sub(r"[^A-Za-z0-9_.-]", "-", CRAFTGROUND_RUNTIME_VERSION)
        target = (
            Path.home()
            / ".cache"
            / "tao"
            / f"craftground-runtime-mc121-{safe_version}-{source_digest[:12]}"
        )
    target = target.expanduser().resolve()
    build_identity = f"{CRAFTGROUND_RUNTIME_VERSION}\n{source_digest}\n"

    with _PREPARE_LOCK:
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target)
            _prune_instance(target)
        validate_maintained_runtime(target)
        marker = target / _RUNTIME_BUILD_MARKER
        built = marker.is_file() and marker.read_text(encoding="ascii") == build_identity
        if build and not built:
            gradle = target / ("gradlew.bat" if sys.platform == "win32" else "gradlew")
            if not gradle.is_file():
                raise FileNotFoundError(f"runtime 缺少 Gradle wrapper：{gradle}")
            gradle.chmod(0o755)
            subprocess.run([str(gradle), "build", "--no-daemon"], cwd=target, check=True)
            marker.write_text(build_identity, encoding="ascii")
    return target


def _prune_instance(target: Path) -> None:
    """剪掉实例不需要的模板产物。"""
    for relative in _INSTANCE_PRUNED_PATHS:
        path = target / relative
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    # 嵌套目录里也可能残留 CMake 缓存。
    for stale in target.rglob("CMakeCache.txt"):
        stale.unlink()


def prepare_runtime_instance(
    instance_id: str,
    *,
    template: Path | None = None,
    instances_root: Path | None = None,
) -> Path:
    """从只读构建模板创建一个可独立运行的工作目录。

    Args:
        instance_id: 实例标识，用于目录名。非安全字符替换为连字符。
        template: 模板目录。`None` 表示自动准备。
        instances_root: 实例根目录。`None` 表示 `~/.cache/tao/`。

    Returns:
        实例目录。

    Raises:
        ValueError: `instance_id` 不含任何安全字符。
        FileNotFoundError: 模板不存在。
    """
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "-", instance_id).strip(".-")
    if not safe_id:
        raise ValueError("instance_id 必须包含字母、数字或安全分隔符")
    resolved = prepare_runtime_template() if template is None else template.expanduser().resolve()
    if not resolved.is_dir():
        raise FileNotFoundError(f"CraftGround runtime 模板不存在：{resolved}")
    root = (
        Path.home() / ".cache" / "tao" / "craftground-runtime-instances"
        if instances_root is None
        else instances_root.expanduser().resolve()
    )
    target = root / safe_id
    template_marker = resolved / _RUNTIME_BUILD_MARKER
    identity = hashlib.sha256(
        (str(resolved) + "\n").encode("utf-8")
        + (template_marker.read_bytes() if template_marker.is_file() else b"unbuilt")
    ).hexdigest()

    with _PREPARE_LOCK:
        marker = target / _INSTANCE_MARKER
        current = marker.read_text(encoding="ascii").strip() if marker.is_file() else None
        if current != identity:
            if target.exists():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                shutil.copytree(resolved, temporary)
                _prune_instance(temporary)
                (temporary / _INSTANCE_MARKER).write_text(identity + "\n", encoding="ascii")
                temporary.replace(target)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        gradle = target / ("gradlew.bat" if sys.platform == "win32" else "gradlew")
        if gradle.is_file():
            gradle.chmod(0o755)
    return target.resolve()


def create_environment(
    runtime_path: Path | str,
    *,
    image_width: int = 640,
    image_height: int = 360,
    seed: str = "424242",
    render_distance: int = MINIMUM_RENDER_DISTANCE,
    simulation_distance: int = MINIMUM_SIMULATION_DISTANCE,
    world_type: str = "DEFAULT",
    port: int = 18300,
    use_shared_memory: bool = True,
    hud_hidden: bool = True,
    initial_extra_commands: tuple[str, ...] | list[str] | None = None,
    level_display_name_to_play: str = "",
    request_raycast: bool = False,
    requires_surrounding_blocks: bool = False,
    verbose: bool = False,
    control_mode: str = "agent",
) -> Any:
    """创建一个 CraftGround 环境实例。

    默认取正常的 `DEFAULT` 世界与最小视野。`SUPERFLAT` 仅供调用方显式性能调试。
    `render_distance` 与 `simulation_distance` 低于合法下限时报错而不静默——传 1 会被
    Minecraft 丢弃并回落到默认 12，反而更慢，静默接受会让调用方以为自己在省时间。

    Args:
        runtime_path: 实例目录。
        image_width: 观察宽度，像素。
        image_height: 观察高度，像素。
        seed: 世界种子。
        render_distance: 客户端视距，区块。最小 2。
        simulation_distance: 模拟距离，区块。最小 5。
        world_type: 世界类型。mc121 只实现了 `SUPERFLAT` 与 `DEFAULT`。
        port: IPC 端口。
        use_shared_memory: 是否用共享内存 IPC。观察不经 socket 序列化。
        hud_hidden: 是否隐藏 HUD。
        initial_extra_commands: 进世界后立即执行的命令。用于钉死 gamerule。
        level_display_name_to_play: 要进入的存档目录名。空串表示新建世界。
        request_raycast: 是否在观察里带视线方块。**默认关闭**，关闭时
            `raycast_result` 恒为空——任务判据若依赖视线方块必须显式打开。
        requires_surrounding_blocks: 是否在观察里带玩家周围 27 个方块。**默认关闭**，
            关闭时 `surrounding_blocks` 恒为空列表。
        verbose: 是否打开 CraftGround 日志。
        control_mode: `agent` 使用锁步动作控制；`human` 完整透传窗口键鼠。

    Returns:
        `CraftGroundEnvironment` 实例，附带 `tao_runtime_path` 属性。

    Raises:
        ValueError: 视距低于下限。
        FileNotFoundError: 实例目录不存在。
    """
    if render_distance < MINIMUM_RENDER_DISTANCE:
        raise ValueError(
            f"render_distance 最小为 {MINIMUM_RENDER_DISTANCE}，实际 {render_distance}；"
            "更小的值会被 Minecraft 静默丢弃并回落到默认 12"
        )
    if simulation_distance < MINIMUM_SIMULATION_DISTANCE:
        raise ValueError(
            f"simulation_distance 最小为 {MINIMUM_SIMULATION_DISTANCE}，实际 {simulation_distance}"
        )
    resolved = Path(runtime_path).expanduser().resolve()
    if not resolved.is_dir():
        raise FileNotFoundError(f"CraftGround runtime 不存在：{resolved}")

    craftground = importlib.import_module("craftground")
    CraftGroundEnvironment = craftground.CraftGroundEnvironment
    InitialEnvironmentConfig = craftground.InitialEnvironmentConfig
    from craftground.environment.action_space import ActionSpaceVersion
    from craftground.initial_environment_config import WorldType
    from craftground.screen_encoding_modes import ScreenEncodingMode

    config = InitialEnvironmentConfig(
        image_width=image_width,
        image_height=image_height,
        seed=seed,
        render_distance=render_distance,
        simulation_distance=simulation_distance,
        screen_encoding_mode=ScreenEncodingMode.RAW,
        hud_hidden=hud_hidden,
        world_type=getattr(WorldType, world_type),
        initial_extra_commands=list(initial_extra_commands) if initial_extra_commands else None,
        level_display_name_to_play=level_display_name_to_play,
        request_raycast=request_raycast,
        requires_surrounding_blocks=requires_surrounding_blocks,
    )
    environment = CraftGroundEnvironment(
        config,
        action_space_version=getattr(ActionSpaceVersion, CRAFTGROUND_ACTION_SPACE),
        env_path=str(resolved),
        port=port,
        use_shared_memory=use_shared_memory,
        cleanup_world=not level_display_name_to_play,
        verbose=verbose,
        control_mode=control_mode,
    )
    environment.tao_runtime_path = str(resolved)
    return environment
