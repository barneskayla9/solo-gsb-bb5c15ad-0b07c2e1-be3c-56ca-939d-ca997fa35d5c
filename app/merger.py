"""确定性 JSON Schema 三路合并：``POST /schema-merge`` 的合并内核。

多人分别基于同一版 schema 修改规则后发布前合并：输入**基版 base、甲版 a、
乙版 b**，按对象成员递归比较——

- 双方都没碰的位置：保留基版值；
- 只有一方修改的位置：采用该方的值；
- 双方在同一位置改成相同值：只保留一次；
- 双方在同一位置改成不同值（``modify_modify``）、删除与修改相撞
  （``delete_modify``）、双方各自新增同名键但值不同（``add_add``）：报告冲突；
- **数组视为整体**：双方对同一数组作任何不同修改都算冲突，不做逐元素合并；
  标量（含布尔 schema ``true`` / ``false``）同样按整体值比较。

只要存在冲突，就不产生任何（部分）合并结果；调用方负责在无冲突时再对合并结果
做一次完整的 schema 合法性校验。全程无随机源，相同输入必然得到相同结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jsonschema.exceptions import SchemaError

from .minimizer import build_validator, pointer_from_path

# 同一位置改成不同值（含双方对同一数组的不同修改）。
KIND_MODIFY_MODIFY = "modify_modify"
# 一方删除成员、另一方修改该成员的值。
KIND_DELETE_MODIFY = "delete_modify"
# 双方各自新增同名成员但新增值不同。
KIND_ADD_ADD = "add_add"

_MISSING = object()


@dataclass
class Conflict:
    """单个合并冲突。"""

    pointer: str
    kind: str
    # 三方在该位置的取值；成员不存在（删除或从未有过）时为 MISSING。
    base: Any = _MISSING
    a: Any = _MISSING
    b: Any = _MISSING

    def to_dict(self) -> dict[str, Any]:
        return {
            "pointer": self.pointer,
            "kind": self.kind,
            "base": None if self.base is _MISSING else self.base,
            "a": None if self.a is _MISSING else self.a,
            "b": None if self.b is _MISSING else self.b,
            "base_present": self.base is not _MISSING,
            "a_present": self.a is not _MISSING,
            "b_present": self.b is not _MISSING,
            "message": _describe_conflict(self),
        }


@dataclass
class MergeOutcome:
    merged: Any
    conflicts: list[Conflict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.conflicts

    def to_dict(self) -> dict[str, Any]:
        return {
            "merged": self.merged if self.ok else None,
            "conflicts": [c.to_dict() for c in self.conflicts],
        }


def merge_schemas(base: Any, a: Any, b: Any) -> MergeOutcome:
    """合并 base / a / b 三版 schema，收集全部冲突（不输出部分合并结果）。"""
    conflicts: list[Conflict] = []
    merged = _merge_nodes(base, a, b, (), conflicts)
    # 按 (pointer, kind) 排序：递归遍历顺序依赖键序，排序后输出与键序无关。
    conflicts.sort(key=lambda c: (c.pointer, c.kind))
    return MergeOutcome(None if conflicts else merged, conflicts)


def _merge_nodes(base: Any, a: Any, b: Any, path: tuple, conflicts: list[Conflict]) -> Any:
    # 三方同值：保留一次（含都是 dict / list / 标量的情形，整棵子树未改）。
    if json_equal(a, b):
        return a
    # 只有一方相对基版修改：采用该方。
    if json_equal(a, base):
        return b
    if json_equal(b, base):
        return a

    # 双方都改了且改成不同值：仅在三方同为对象时递归合并对象成员，
    # 否则标量/数组（以及类型变了的节点）按整体报“同位置不同值”。
    if isinstance(base, dict) and isinstance(a, dict) and isinstance(b, dict):
        return _merge_objects(base, a, b, path, conflicts)
    conflicts.append(
        Conflict(pointer_from_path(path), KIND_MODIFY_MODIFY, base, a, b)
    )
    return _MISSING


def _merge_objects(
    base: dict, a: dict, b: dict, path: tuple, conflicts: list[Conflict]
) -> dict:
    merged: dict = {}
    # 键序确定：基版键序优先，其次甲、乙各自的插入序（与 dict 遍历一致）。
    keys = list(base.keys())
    for key in (*a.keys(), *b.keys()):
        if key not in keys:
            keys.append(key)

    for key in keys:
        child_path = path + (key,)
        in_base, in_a, in_b = key in base, key in a, key in b
        bv = base.get(key, _MISSING)
        av = a.get(key, _MISSING)
        bvv = b.get(key, _MISSING)

        if in_base:
            if not in_a and not in_b:
                # 双方都删除：删除只发生一次。
                continue
            if not in_a:
                if json_equal(bvv, bv):
                    continue  # 甲删除、乙未动：删除生效。
                # 甲删除、乙修改：删除与修改相撞。
                conflicts.append(
                    Conflict(pointer_from_path(child_path), KIND_DELETE_MODIFY, bv, _MISSING, bvv)
                )
                continue
            if not in_b:
                if json_equal(av, bv):
                    continue  # 乙删除、甲未动：删除生效。
                conflicts.append(
                    Conflict(pointer_from_path(child_path), KIND_DELETE_MODIFY, bv, av, _MISSING)
                )
                continue
            # 三方都有该键：交给通用递归（其中含“同值/单方改/同改/冲突”判定）。
            child = _merge_nodes(bv, av, bvv, child_path, conflicts)
            if child is not _MISSING:
                merged[key] = child
        else:
            if in_a and in_b:
                if json_equal(av, bvv):
                    merged[key] = av  # 双方新增相同值：只保留一次。
                else:
                    # 双方各自新增同名键但值不同（新增位置没有基版可递归）。
                    conflicts.append(
                        Conflict(pointer_from_path(child_path), KIND_ADD_ADD, _MISSING, av, bvv)
                    )
            elif in_a:
                merged[key] = av  # 仅甲新增。
            else:
                merged[key] = bvv  # 仅乙新增。

    return merged


def json_equal(left: Any, right: Any) -> bool:
    """JSON 语义下的深度相等。

    与直接用 ``==`` 的区别：``True`` 不等于 ``1``、``False`` 不等于 ``0``
    （Python 把 bool 当 int 子类，但 JSON 中二者类型不同）；dict / list
    递归比较；其余标量按值比较。
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        if left.keys() != right.keys():
            return False
        return all(json_equal(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            json_equal(x, y) for x, y in zip(left, right)
        )
    if isinstance(left, (dict, list)) or isinstance(right, (dict, list)):
        return False
    return left == right


def _describe_conflict(conflict: Conflict) -> str:
    where = conflict.pointer if conflict.pointer else ""
    location = where or "（根）"
    if conflict.kind == KIND_MODIFY_MODIFY:
        if isinstance(conflict.a, list) or isinstance(conflict.b, list):
            return f"位置 {location} 双方对同一数组作了不同修改，数组按整体比较，不逐元素合并"
        return f"位置 {location} 双方都作了修改但取值不同"
    if conflict.kind == KIND_DELETE_MODIFY:
        deleted_side = "甲" if conflict.a is _MISSING else "乙"
        modified_side = "乙" if deleted_side == "甲" else "甲"
        return f"位置 {location} {deleted_side}删除了成员，{modified_side}修改了其值，删除与修改相撞"
    # add_add
    return f"位置 {location} 双方都新增了同名成员但取值不同"


def check_schema_version(which: str, schema: Any) -> SchemaError | None:
    """校验一版输入 schema 是否为合法且可用于校验的 JSON Schema。

    返回 ``None`` 表示合法；否则返回 :class:`SchemaError`，其 ``message`` 与
    ``absolute_path`` 可供接口层报告版本、位置和原因。除 ``check_schema`` 的
    静态元模式检查外，再用哨兵实例探测一次，让根级坏 ``$ref``（静态检查通过、
    实际无法解析）同样按非法 schema 处理。
    """
    try:
        validator = build_validator(schema)
    except SchemaError as exc:
        return exc
    try:
        # 空对象是所有 draft 下最宽松的实例形状之一；本探测只用于触发根级
        # $ref 解析等“schema 自身无法运行”的问题，不判断实例通过与否。
        next(iter(validator.iter_errors({})), None)
    except Exception as exc:  # 坏 $ref 等：与 /schema-diff、/mutations 同样兜底。
        return SchemaError(f"schema 无法用于校验: {exc}")
    return None
