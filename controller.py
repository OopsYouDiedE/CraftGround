"""CraftGround 标准环境控制器。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shared_tools.environment_protocol import UnderflowPolicy

from .env import CraftGroundEnv, build_env
from .key_mapping import KeyMapping

DEFAULT_PORT_BASE = 18300


class CraftGroundController:
    """创建、取得、枚举、扩缩和退出独立 CraftGround Env。"""

    def __init__(
        self,
        mapping: KeyMapping,
        *,
        slots: int = 1,
        version: int = 1,
        underflow: UnderflowPolicy = UnderflowPolicy.NOOP,
        port_base: int = DEFAULT_PORT_BASE,
        instance_prefix: str = "tao",
        template: Path | None = None,
        instances_root: Path | None = None,
        archive_root: Path | str = "trajectories/environments",
        env_options: dict[str, Any] | None = None,
    ) -> None:
        if slots < 0:
            raise ValueError("slots 不能为负")
        if version not in (1, 2):
            raise ValueError("version 必须为 1 或 2")
        self._mapping = mapping
        self._version = version
        self._underflow = underflow
        self._port_base = port_base
        self._instance_prefix = instance_prefix
        self._template = template
        self._instances_root = instances_root
        self._archive_root = Path(archive_root).expanduser().resolve()
        self._env_options = dict(env_options or {})
        self._envs: dict[int, CraftGroundEnv] = {}
        self._closed = False
        self.set_env_to_num(slots)

    def create_env(self) -> int:
        """创建一个 Env 并返回固定整数槽位。"""
        self._require_open()
        env_index = 0
        while env_index in self._envs:
            env_index += 1
        self._envs[env_index] = build_env(
            f"craftground-{env_index}",
            self._mapping,
            instance_id=f"{self._instance_prefix}-{env_index}",
            port=self._port_base + env_index,
            template=self._template,
            instances_root=self._instances_root,
            version=self._version,
            underflow=self._underflow,
            archive_root=self._archive_root,
            **self._env_options,
        )
        return env_index

    def get_env(self, env_index: int) -> CraftGroundEnv:
        """取得固定槽位中的 Env。"""
        self._require_open()
        try:
            return self._envs[env_index]
        except KeyError as error:
            raise IndexError(f"没有 env_index {env_index}") from error

    def list_envs(self) -> list[int]:
        """按槽位升序枚举 Env。"""
        self._require_open()
        return sorted(self._envs)

    def set_env_to_num(self, n: int) -> None:
        """扩缩 Env 数量；缩容前封存活动回合。"""
        self._require_open()
        if n < 0:
            raise ValueError("Env 数量不能为负")
        while len(self._envs) < n:
            self.create_env()
        while len(self._envs) > n:
            env_index = max(self._envs)
            environment = self._envs.pop(env_index)
            environment.truncate()
            environment.close()

    def quit(self) -> None:
        """阻塞封存全部活动回合并释放 JVM。"""
        if self._closed:
            return
        for environment in self._envs.values():
            environment.truncate()
            environment.close()
        self._envs.clear()
        self._closed = True

    # 旧调用名仅保留为迁移入口。
    def change_env_num_to(self, n: int) -> list[str]:
        self.set_env_to_num(n)
        return [self._envs[index].id() for index in self.list_envs()]

    def all_agent_names(self) -> list[str]:
        return [self._envs[index].get_agent_id()[0] for index in self.list_envs()]

    def get_action_protocol(self) -> str:
        if self._version == 1:
            return "tao-gamepad"
        return "tao-touch"

    def get_supported_action_protocols(self) -> tuple[str, ...]:
        """返回控制器创建环境时使用的协议集合。"""
        return (self.get_action_protocol(),)

    def exit(self) -> None:
        self.quit()

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("控制器已退出")

    def __enter__(self) -> CraftGroundController:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.quit()
