"""简化协议槽位与真实键位之间的映射表。

映射表规范见 `docs/standard-input-action-protocol.md` 的映射表规范一节。同一张表承担
两个方向：顺读把协议槽位翻成真实键位（在线执行），逆读把真实键位翻回协议槽位（离线数据
适配）。因此不存在两份需要同步的表。

书写形式为 `<槽位> <值> -> <真实键位>`，字段之间及箭头两侧单个空格。位串键与 `Slot0`
不写值，连续槽位必须写值。真实键位是相对位移设备时在键名后追加灵敏度常量。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from shared_tools.action_conversion import (
    AXIS_NAMES,
    V2_BUTTON_NAMES,
)

#: 绝对坐标点击专用槽位。键鼠只有一个光标，固定用它。
ABSOLUTE_SLOT_NAME = "Slot0"


@dataclass(frozen=True, slots=True)
class MappingRule:
    """映射表中的一条规则。

    Attributes:
        slot: 左侧槽位名。位串键取 `V2_BUTTON_NAMES` 之一，连续键位取 `AXIS_NAMES`
            之一或 `LS`/`RS`，绝对坐标取 `Slot0`。
        value: 左侧取值。位串键与 `Slot0` 为 `None`，单轴为一个浮点，`LS`/`RS` 为两个。
        keys: 右侧真实键位，空格分隔的组合键按顺序保留。
        sensitivity: 相对位移设备的灵敏度常量，表示轴值满值对应的每 tick 位移量。
            非相对位移设备为 `None`。
    """

    slot: str
    value: tuple[float, ...] | None
    keys: tuple[str, ...]
    sensitivity: float | None = None

    @property
    def is_axis(self) -> bool:
        """该规则的左侧是否为连续键位。"""
        return self.value is not None


@dataclass(frozen=True, slots=True)
class KeyMapping:
    """一张已加载的映射表。

    Attributes:
        rules: 按文件顺序排列的全部规则。
        unmapped: 映射表显式记录的无对应槽位的键名。协议要求状态切换键多于协议位数时
            必须显式记录，不得挪用语义不符的空位。
    """

    rules: tuple[MappingRule, ...]
    unmapped: tuple[str, ...] = ()

    def button_rule(self, name: str) -> MappingRule | None:
        """取位串键 `name` 的规则，不存在时返回 `None`。"""
        for rule in self.rules:
            if rule.slot == name and not rule.is_axis:
                return rule
        return None

    def axis_rules(self, name: str) -> tuple[MappingRule, ...]:
        """取轴 `name` 的全部规则。同一槽位可有多条不同取值的规则。"""
        return tuple(rule for rule in self.rules if rule.slot == name and rule.is_axis)

    def nearest_axis_rule(self, name: str, value: float) -> MappingRule | None:
        """顺读取最接近 `value` 的轴规则。

        协议规定「槽位取值未精确匹配任何规则时，顺读取最接近的规则」，近似只发生在
        顺读。回中值（轴为 0）不查表，由调用方直接跳过。

        Args:
            name: 轴名，取 `AXIS_NAMES` 之一。
            value: 当前轴值。

        Returns:
            距离最近的规则；该轴没有任何规则时返回 `None`。
        """
        candidates = self.axis_rules(name)
        if not candidates:
            return None
        return min(candidates, key=lambda rule: abs((rule.value or (0.0,))[0] - value))

    def reverse_lookup(self, key: str) -> tuple[MappingRule, ...]:
        """逆读：按真实键名取全部命中的规则。只做精确匹配。"""
        return tuple(rule for rule in self.rules if key in rule.keys)


def _parse_left(fields: list[str]) -> tuple[str, tuple[float, ...] | None]:
    """解析规则左侧，返回 `(槽位名, 取值)`。"""
    slot = fields[0]
    if slot in ("LS", "RS"):
        if len(fields) == 1:
            # `LS -> Shift` 这类写法把摇杆整体当开关用，视为位串式规则。
            return slot, None
        if len(fields) != 3:
            raise ValueError(f"{slot} 需要两个轴值，实际 {len(fields) - 1} 个")
        return slot, (float(fields[1]), float(fields[2]))
    if slot in AXIS_NAMES:
        if len(fields) != 2:
            raise ValueError(f"连续槽位 {slot} 必须写一个值")
        return slot, (float(fields[1]),)
    if slot in V2_BUTTON_NAMES or slot == ABSOLUTE_SLOT_NAME:
        if len(fields) != 1:
            raise ValueError(f"位串键与 {ABSOLUTE_SLOT_NAME} 不写值，实际 {fields}")
        return slot, None
    raise ValueError(f"未知槽位 {slot!r}")


def _parse_right(text: str) -> tuple[tuple[str, ...], float | None]:
    """解析规则右侧，返回 `(键名序列, 灵敏度常量)`。"""
    fields = text.split()
    if not fields:
        raise ValueError("规则右侧不能为空")
    sensitivity: float | None = None
    try:
        sensitivity = float(fields[-1])
    except ValueError:
        return tuple(fields), None
    if len(fields) == 1:
        raise ValueError("规则右侧不能只有灵敏度常量")
    return tuple(fields[:-1]), sensitivity


def parse_mapping(text: str) -> KeyMapping:
    """解析映射表文本。

    Args:
        text: 映射表文本。每行 `<槽位> <值> -> <真实键位>`，空行与 `#` 注释行忽略。
            `# unmapped: <键名>` 注释用于显式记录无对应槽位的键。

    Returns:
        解析后的映射表。

    Raises:
        ValueError: 某行缺少箭头、槽位未知、取值个数不符，或左列取值重复。左列取值
            重复时顺读无法判定，因此在加载期报错而非留到运行期。
    """
    rules: list[MappingRule] = []
    unmapped: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            comment = stripped.lstrip("#").strip()
            if comment.startswith("unmapped:"):
                unmapped.extend(comment.removeprefix("unmapped:").split())
            continue
        if " -> " not in stripped:
            raise ValueError(f"第 {number} 行缺少 ' -> '：{line!r}")
        left, _, right = stripped.partition(" -> ")
        try:
            slot, value = _parse_left(left.split())
            keys, sensitivity = _parse_right(right)
        except ValueError as error:
            raise ValueError(f"第 {number} 行：{error}") from error
        rules.append(MappingRule(slot=slot, value=value, keys=keys, sensitivity=sensitivity))

    seen: set[tuple[str, tuple[float, ...] | None]] = set()
    for rule in rules:
        identity = (rule.slot, rule.value)
        if identity in seen:
            raise ValueError(f"左列取值重复，顺读无法判定：{rule.slot} {rule.value}")
        seen.add(identity)
    return KeyMapping(rules=tuple(rules), unmapped=tuple(unmapped))


def load_mapping(path: Path | str) -> KeyMapping:
    """从文件加载映射表。"""
    return parse_mapping(Path(path).read_text(encoding="utf-8"))


def format_mapping(mapping: KeyMapping) -> str:
    """把映射表写回规范文本形式。"""
    lines: list[str] = []
    for rule in mapping.rules:
        left = rule.slot
        if rule.value is not None:
            left = f"{rule.slot} " + " ".join(f"{value:.2f}" for value in rule.value)
        right = " ".join(rule.keys)
        if rule.sensitivity is not None:
            right = f"{right} {rule.sensitivity:g}"
        lines.append(f"{left} -> {right}")
    if mapping.unmapped:
        lines.append(f"# unmapped: {' '.join(mapping.unmapped)}")
    return "\n".join(lines)


def missing_slots(mapping: KeyMapping, names: Iterable[str]) -> tuple[str, ...]:
    """返回 `names` 中没有任何规则的槽位。

    协议把协议侧放在左列，正是为了让缺失的槽位可以直接看出。
    """
    covered = {rule.slot for rule in mapping.rules}
    return tuple(name for name in names if name not in covered)
