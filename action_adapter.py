"""简化协议 tick 到 CraftGround V2 动作字典的转换。

顺读方向的唯一实现：`Tick`（抽象手柄语义槽位）经映射表查表得到真实键位，再按 CraftGround
的字段名装配动作字典。适配器不认识任何键名字符串——键名全部来自映射表，因此换键位不需要
改代码。

CraftGround 的 `V2_MINERL_HUMAN` 动作字典有 22 个字段：11 个布尔动作、9 个快捷栏布尔、
两个浮点视角增量。字段清单由 `no_op_v2()` 在运行期给出，适配器只校验自己写入的键存在。

协议侧的目标版本是 v1。CraftGround 的动作字典没有任何绝对坐标字段，只有 `camera_yaw` 与
`camera_pitch` 两个相对增量，而 v2 的存在理由正是那 8 个绝对坐标槽位。因此 GUI 里的光标由
右摇杆的相对位移驱动：`MouseInfo.moveMouseBy` 逐像素调用 Minecraft 真实的 `cursorPosCallback`，
`MouseInfo.onAction` 按状态变化调真实 `mouseButtonCallback` 且做了按下与松开的边沿检测，
因此「右摇杆移光标 + 扳机长按左键」就是拖拽。收到 v2 tick 时槽位段被忽略并记账。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from shared_tools.action_conversion import AXIS_NAMES, V1_BUTTON_NAMES, V2_BUTTON_NAMES, Tick

from .key_mapping import ABSOLUTE_SLOT_NAME, KeyMapping

#: CraftGround 动作字典里视角增量的字段名，单位是度。
CAMERA_YAW_FIELD = "camera_yaw"
CAMERA_PITCH_FIELD = "camera_pitch"
HOTBAR_SLOT_COUNT = 9

#: 真实键名到 CraftGround 动作字典字段的对应。这是 CraftGround 后端的事实，不是协议的
#: 一部分——协议侧的键名由映射表决定，这张表只负责把映射表右侧的键名落到后端字段。
KEY_TO_FIELD = {
    "W": "forward",
    "S": "back",
    "A": "left",
    "D": "right",
    "Space": "jump",
    "Shift": "sneak",
    "Ctrl": "sprint",
    "LeftMouse": "attack",
    "RightMouse": "use",
    "Q": "drop",
    "E": "inventory",
    **{f"Hotbar{slot}": f"hotbar.{slot}" for slot in range(1, HOTBAR_SLOT_COUNT + 1)},
}

#: 相对位移设备的键名到视角字段的对应。映射表在这些键名后追加灵敏度常量。
MOUSE_MOVE_FIELDS = {
    "MouseMoveX": CAMERA_YAW_FIELD,
    "MouseMoveY": CAMERA_PITCH_FIELD,
}


def _default_action_factory() -> dict[str, bool | float]:
    """取 CraftGround 的空动作字典。"""
    from craftground.environment.action_space import no_op_v2

    return no_op_v2()


@dataclass
class CraftGroundActionAdapter:
    """把简化协议 tick 转成 CraftGround 动作字典。

    Attributes:
        mapping: 顺读所依据的映射表。
        action_factory: 空动作字典工厂。默认取 CraftGround 的 `no_op_v2`；注入点保留
            给纯逻辑测试，使转换逻辑可以在没有 CraftGround 的环境里验证。
        selected_hotbar: 当前选中的快捷栏槽位，`1` 到 `9`。只作为设备状态供审核读取，
            不参与转换：映射表走绝对的 `Hotbar1` 到 `Hotbar4`，不做相对切换。观察侧
            没有「当前选中第几格」字段，一旦用它推算目标就会产生观察不到的隐藏状态，
            倒档后与真值分叉且无法发现。
    """

    mapping: KeyMapping
    action_factory: Callable[[], dict[str, bool | float]] = _default_action_factory
    selected_hotbar: int = 1
    _unmapped_seen: set[str] = field(default_factory=set, repr=False)

    def reset(self, selected_hotbar: int = 1) -> None:
        """重置设备状态。倒档与新回合都要调用，使记录的选中格与环境重新对齐。"""
        if not 1 <= selected_hotbar <= HOTBAR_SLOT_COUNT:
            raise ValueError(f"selected_hotbar 必须位于 1 到 9，实际 {selected_hotbar}")
        self.selected_hotbar = selected_hotbar

    def convert(self, tick: Tick, *, hotbar_slot: int | None = None) -> dict[str, bool | float]:
        """把一个 tick 转成 CraftGround 动作字典。

        位串按位查映射表，取到键名后置对应布尔字段为真。轴值非零时取最接近的规则；
        相对位移设备按 `轴值 * 灵敏度` 换算成视角增量，其余轴按开关处理。

        Args:
            tick: 待转换的 tick。版本由段结构自动推断。

        Returns:
            CraftGround V2 动作字典。未被本 tick 触及的字段保持 `no_op_v2` 的默认值。
            ``hotbar_slot`` 是给 VPT 使用的受限后端扩展，用于保留 v1 无法表达的第 5
            到 9 格；它不改变协议位串。

        Raises:
            ValueError: 映射表把某个槽位指向了 CraftGround 没有的字段。
        """
        action = self.action_factory()
        if hotbar_slot is not None and not 1 <= hotbar_slot <= HOTBAR_SLOT_COUNT:
            raise ValueError(f"hotbar_slot 必须位于 1 到 9，实际 {hotbar_slot}")
        names = V1_BUTTON_NAMES if tick.version == 1 else V2_BUTTON_NAMES
        overrides: dict[str, bool | float] = {}
        if any(on for on, _, _ in tick.slots):
            # CraftGround 没有绝对坐标字段，接触中的槽位无处落。记账而不静默丢弃，
            # 使「模型输出了 GUI 点击但环境根本收不到」在验收时可见。
            self._unmapped_seen.add(ABSOLUTE_SLOT_NAME)

        for index, bit in enumerate(tick.buttons):
            if not bit:
                continue
            rule = self.mapping.button_rule(names[index])
            if rule is None:
                self._unmapped_seen.add(names[index])
                continue
            self._apply_keys(rule.keys, overrides)

        yaw = 0.0
        pitch = 0.0
        for index, value in enumerate(tick.axes):
            if value == 0.0:
                continue
            name = AXIS_NAMES[index]
            rule = self.mapping.nearest_axis_rule(name, value)
            if rule is None:
                self._unmapped_seen.add(name)
                continue
            if rule.sensitivity is not None:
                # 摇杆轴值是速率，视角位移是度数增量，两者之间必须有换算常量。
                for key in rule.keys:
                    field_name = MOUSE_MOVE_FIELDS.get(key)
                    if field_name == CAMERA_YAW_FIELD:
                        yaw += value * rule.sensitivity
                    elif field_name == CAMERA_PITCH_FIELD:
                        pitch += value * rule.sensitivity
                continue
            self._apply_keys(rule.keys, overrides)

        overrides[CAMERA_YAW_FIELD] = yaw
        overrides[CAMERA_PITCH_FIELD] = pitch
        if hotbar_slot is not None:
            overrides[f"hotbar.{hotbar_slot}"] = True
            self.selected_hotbar = hotbar_slot

        unknown = sorted(set(overrides).difference(action))
        if unknown:
            raise ValueError(f"CraftGround V2 动作字典缺少字段：{unknown}")
        action.update(overrides)
        return action

    @property
    def unmapped_slots(self) -> tuple[str, ...]:
        """转换过程中遇到过、但映射表没有覆盖的槽位。

        协议要求映射表显式记录无对应槽位的键，不得挪用语义不符的空位。因此这里不报错，
        只记账，供验收时确认缺口是有意留下的而不是漏写。
        """
        return tuple(sorted(self._unmapped_seen))

    def _apply_keys(self, keys: tuple[str, ...], overrides: dict[str, bool | float]) -> None:
        """把一组真实键名落到动作字典字段上。"""
        for key in keys:
            field_name = KEY_TO_FIELD.get(key)
            if field_name is None:
                self._unmapped_seen.add(key)
                continue
            overrides[field_name] = True
            if field_name.startswith("hotbar."):
                self.selected_hotbar = int(field_name.removeprefix("hotbar."))
