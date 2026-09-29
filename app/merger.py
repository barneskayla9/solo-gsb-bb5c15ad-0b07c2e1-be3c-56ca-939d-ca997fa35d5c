"""确定性三方合并：``POST /schema-merge`` 的合并内核。

多人基于同一基版 JSON Schema 各自修改后，发布前需要把两份结果合并：

1. 三个版本（基版 / 甲版 / 乙版）各自先做 schema 合法性检查（接口层负责，
   非法时指出版本、位置与原因）；
2. 以基版为参照做三方合并，按对象成员递归比较：

   - 仅一方修改（另一方与基版一致）：取修改方；
   - 双方改成相同值（含双方都未改、双方都删除）：保留一次；
   - 双方都改了且值不同：三方都是对象则按成员递归合并，其余情况
     （数组视为整体、标量、类型变化）整体冲突；
   - 一方修改、另一方删除：修改与删除相撞，冲突；
   - 基版不存在该成员、双方各自新增：值相同保留一次，都是对象可递归，
     其余冲突。

3. 冲突只列出 JSON Pointer 位置、冲突类型与双方取值，**不输出部分合并结果**；
4. 无冲突时合并结果还需通过 schema 合法性检查（接口层负责），
   不合法同样不输出合并结果。

数组成员不做递归：双方对同一数组的任何不同修改都视为整体冲突。
全程无随机源，相同输入必然得到相同结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .minimizer import pointer_from_path
from .mutator import _json_equal

# 冲突类型。
KIND_BOTH_MODIFIED = "both_modified"  # 同一位置被双方改成不同的值
KIND_BOTH_ADDED = "both_added"  # 基版没有该成员，双方新增了不同的值
KIND_MODIFY_VS_DELETE = "modify_vs_delete"  # 一方修改、另一方删除

# 哨兵：基版中不存在该成员（与 JSON null 区分，null 是合法取值）。
_MISSING: Any = object()


@dataclass
class Conflict:
    """一处合并冲突。

    不同 ``kind`` 携带的字段不同（见 :meth:`to_dict`）：
    ``both_modified`` 带 ``base`` / ``a`` / ``b``；``both_added`` 不带 ``base``；
    ``modify_vs_delete`` 带 ``deleted_by`` 与修改方保留的 ``value``。
    """

    kind: str
    path: tuple
    base: Any = None
    a: Any = None
    b: Any = None
    deleted_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "pointer": pointer_from_path(self.path),
            "kind": self.kind,
        }
        if self.kind == KIND_MODIFY_VS_DELETE:
            # 删除方不携带取值；value 为修改方保留的值。
            entry["deleted_by"] = self.deleted_by
            entry["base"] = self.base
            entry["value"] = self.b if self.deleted_by == "a" else self.a
        else:
            if self.kind == KIND_BOTH_MODIFIED:
                entry["base"] = self.base
            entry["a"] = self.a
            entry["b"] = self.b
        return entry


@dataclass
class MergeResult:
    """合并结果：``merged`` 为 False 时 ``merged_schema`` 恒为 None。"""

    merged: bool
    merged_schema: Any
    conflicts: list[Conflict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "merged": self.merged,
            "merged_schema": self.merged_schema,
            "conflicts": [c.to_dict() for c in self.conflicts],
        }


class SchemaMerger:
    """以基版为参照，把甲版、乙版的修改合并成一份 schema。"""

    def __init__(self, base: Any, a: Any, b: Any):
        self.base = base
        self.a = a
        self.b = b
        self.conflicts: list[Conflict] = []

    def merge(self) -> MergeResult:
        merged = self._merge(self.base, self.a, self.b, ())
        if self.conflicts:
            # 有冲突：只报告冲突位置，不输出部分合并结果。
            return MergeResult(merged=False, merged_schema=None, conflicts=self.conflicts)
        return MergeResult(merged=True, merged_schema=merged)

    # ---- 递归合并 ---------------------------------------------------------

    def _merge(self, base: Any, a: Any, b: Any, path: tuple) -> Any:
        # 双方一致（都未改 / 改成相同值 / 都删除）：保留一次。
        if _json_equal(a, b):
            return a
        # 仅乙版修改：取乙版。
        if _json_equal(a, base):
            return b
        # 仅甲版修改：取甲版。
        if _json_equal(b, base):
            return a
        # 双方都改了且不一样：对象按成员递归；数组 / 标量 / 类型变化整体冲突。
        if (
            isinstance(a, dict)
            and isinstance(b, dict)
            and (isinstance(base, dict) or base is _MISSING)
        ):
            return self._merge_object(base, a, b, path)
        if base is _MISSING:
            self.conflicts.append(Conflict(KIND_BOTH_ADDED, path, a=a, b=b))
        else:
            self.conflicts.append(Conflict(KIND_BOTH_MODIFIED, path, base=base, a=a, b=b))
        return None  # 占位：存在冲突时合并结果不会输出

    def _merge_object(self, base: Any, a: dict, b: dict, path: tuple) -> dict:
        base_obj = base if isinstance(base, dict) else {}
        result: dict[str, Any] = {}
        for key in self._ordered_keys(base_obj, a, b):
            in_base = key in base_obj
            in_a = key in a
            in_b = key in b
            child = path + (key,)
            if in_a and in_b:
                result[key] = self._merge(
                    base_obj.get(key, _MISSING), a[key], b[key], child
                )
            elif in_a:
                if not in_base:
                    result[key] = a[key]  # 仅甲版新增
                elif _json_equal(a[key], base_obj[key]):
                    pass  # 甲版未改、乙版删除：删除
                else:
                    # 甲版修改、乙版删除：修改与删除相撞。
                    self.conflicts.append(
                        Conflict(
                            KIND_MODIFY_VS_DELETE,
                            child,
                            base=base_obj[key],
                            a=a[key],
                            deleted_by="b",
                        )
                    )
            elif in_b:
                if not in_base:
                    result[key] = b[key]  # 仅乙版新增
                elif _json_equal(b[key], base_obj[key]):
                    pass  # 乙版未改、甲版删除：删除
                else:
                    # 乙版修改、甲版删除：修改与删除相撞。
                    self.conflicts.append(
                        Conflict(
                            KIND_MODIFY_VS_DELETE,
                            child,
                            base=base_obj[key],
                            b=b[key],
                            deleted_by="a",
                        )
                    )
            # 双方都没有（含都删除）：不写入结果。
        return result

    @staticmethod
    def _ordered_keys(base: dict, a: dict, b: dict) -> list:
        """确定性键序：基版键按基版顺序，随后是仅甲版、仅乙版新增的键。"""
        keys = list(base.keys())
        keys += [k for k in a if k not in base]
        keys += [k for k in b if k not in base and k not in a]
        return keys
