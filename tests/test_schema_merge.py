"""POST /schema-merge 的端到端 / 单元测试。运行：python -m pytest -q"""

from fastapi.testclient import TestClient

from app.main import app
from app.merger import (
    KIND_BOTH_ADDED,
    KIND_BOTH_MODIFIED,
    KIND_MODIFY_VS_DELETE,
    SchemaMerger,
)

client = TestClient(app)


def run(base, a, b):
    return client.post(
        "/schema-merge",
        json={"base_schema": base, "a_schema": a, "b_schema": b},
    )


# ---- HTTP 层：请求与输入 schema 校验 -----------------------------------------


def test_healthz_still_ok():
    assert client.get("/healthz").json() == {"status": "ok"}


def test_missing_fields_returns_422():
    resp = client.post("/schema-merge", json={"base_schema": {}})
    assert resp.status_code == 422


def test_invalid_base_schema_returns_422():
    resp = run({"type": "not-a-type"}, {}, {})
    assert resp.status_code == 422
    data = resp.json()
    assert data["error"] == "invalid_schema"
    assert data["which"] == "base_schema"
    assert data["path"] == ["type"]


def test_invalid_a_schema_returns_422():
    resp = run({}, {"type": "not-a-type"}, {})
    assert resp.status_code == 422
    assert resp.json()["which"] == "a_schema"


def test_invalid_b_schema_returns_422():
    resp = run({}, {}, {"properties": []})
    assert resp.status_code == 422
    data = resp.json()
    assert data["which"] == "b_schema"
    assert data["path"] == ["properties"]


def test_invalid_merged_schema_returns_422():
    # 三个版本各自合法：甲把 items 改成 draft-07 的元组形式，乙删掉 $schema
    # 声明；合并结果在默认 draft 2020-12 下 items 不允许数组，整体非法。
    base = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "items": {"type": "string"},
    }
    a = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "items": [{"type": "string"}],
    }
    b = {"items": {"type": "string"}}
    resp = run(base, a, b)
    assert resp.status_code == 422
    data = resp.json()
    assert data["error"] == "invalid_merged_schema"
    assert data["path"] == ["items"]
    assert "merged_schema" not in data


# ---- 无冲突：互不相交的修改合并 ----------------------------------------------


def test_disjoint_changes_merge():
    base = {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "name": {"type": "string"},
        },
    }
    a = {
        "type": "object",
        "properties": {
            "id": {"type": "integer", "minimum": 1},  # 甲：id 增加 minimum
            "name": {"type": "string"},
        },
    }
    b = {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "name": {"type": "string", "minLength": 3},  # 乙：name 增加 minLength
        },
    }
    resp = run(base, a, b)
    assert resp.status_code == 200
    data = resp.json()
    assert data["merged"] is True
    assert data["conflicts"] == []
    assert data["merged_schema"] == {
        "type": "object",
        "properties": {
            "id": {"type": "integer", "minimum": 1},
            "name": {"type": "string", "minLength": 3},
        },
    }


def test_disjoint_additions_merge():
    base = {"type": "object"}
    a = {"type": "object", "minProperties": 1}  # 甲新增关键字
    b = {"type": "object", "additionalProperties": False}  # 乙新增关键字
    data = run(base, a, b).json()
    assert data["merged"] is True
    assert data["merged_schema"] == {
        "type": "object",
        "minProperties": 1,
        "additionalProperties": False,
    }


def test_same_change_kept_once():
    # 双方在同一位置改成相同值：只保留一次，不算冲突。
    base = {"type": "object", "description": "old"}
    a = {"type": "object", "description": "v2"}
    b = {"type": "object", "description": "v2"}
    data = run(base, a, b).json()
    assert data["merged"] is True
    assert data["merged_schema"] == {"type": "object", "description": "v2"}


def test_no_change_returns_base():
    base = {"type": "integer", "minimum": 1}
    data = run(base, base, base).json()
    assert data["merged"] is True
    assert data["merged_schema"] == base


def test_delete_vs_unmodified_is_deleted():
    # 乙删除、甲未改：删除生效，不算冲突。
    base = {"type": "object", "description": "x", "minProperties": 1}
    a = {"type": "object", "description": "x", "minProperties": 1}
    b = {"type": "object", "minProperties": 1}
    data = run(base, a, b).json()
    assert data["merged"] is True
    assert data["merged_schema"] == {"type": "object", "minProperties": 1}


def test_both_delete_removes_key():
    base = {"type": "object", "description": "x"}
    a = {"type": "object"}
    b = {"type": "object"}
    data = run(base, a, b).json()
    assert data["merged"] is True
    assert data["merged_schema"] == {"type": "object"}


def test_nested_recursion_merges_deeper_levels():
    base = {
        "properties": {"addr": {"properties": {"zip": {"type": "string"}}}},
        "required": ["addr"],
    }
    a = {
        "properties": {
            "addr": {"properties": {"zip": {"type": "string", "maxLength": 6}}}
        },
        "required": ["addr"],
    }
    b = {
        "properties": {
            "addr": {
                "properties": {"zip": {"type": "string"}},
                "additionalProperties": False,
            }
        },
        "required": ["addr"],
    }
    data = run(base, a, b).json()
    assert data["merged"] is True
    assert data["merged_schema"] == {
        "properties": {
            "addr": {
                "properties": {"zip": {"type": "string", "maxLength": 6}},
                "additionalProperties": False,
            }
        },
        "required": ["addr"],
    }


def test_both_added_same_key_objects_merge_recursively():
    # 基版没有 properties，双方都新增了 properties 对象：按成员递归合并。
    base = {"type": "object"}
    a = {"type": "object", "properties": {"x": {"type": "string"}}}
    b = {"type": "object", "properties": {"y": {"type": "integer"}}}
    data = run(base, a, b).json()
    assert data["merged"] is True
    assert data["merged_schema"] == {
        "type": "object",
        "properties": {"x": {"type": "string"}, "y": {"type": "integer"}},
    }


def test_array_changed_by_one_side_is_taken():
    base = {"required": ["id"], "type": "object"}
    a = {"required": ["id", "name"], "type": "object"}  # 甲改了数组
    b = {"required": ["id"], "type": "object"}
    data = run(base, a, b).json()
    assert data["merged"] is True
    assert data["merged_schema"] == {"required": ["id", "name"], "type": "object"}


def test_boolean_schemas_merge():
    assert run(True, True, {"type": "integer"}).json()["merged_schema"] == {
        "type": "integer"
    }
    assert run(False, False, False).json()["merged_schema"] is False


# ---- 冲突：列出 JSON Pointer，不输出部分合并结果 ------------------------------


def test_both_modified_scalar_conflict():
    base = {"properties": {"age": {"minimum": 0}}}
    a = {"properties": {"age": {"minimum": 1}}}
    b = {"properties": {"age": {"minimum": 2}}}
    resp = run(base, a, b)
    assert resp.status_code == 200
    data = resp.json()
    assert data["merged"] is False
    assert data["merged_schema"] is None
    assert data["conflicts"] == [
        {
            "pointer": "/properties/age/minimum",
            "kind": KIND_BOTH_MODIFIED,
            "base": 0,
            "a": 1,
            "b": 2,
        }
    ]


def test_root_level_conflict_has_empty_pointer():
    # 根节点整体被双方改成不同的非对象值：冲突位置是根（空 pointer）。
    resp = run({"type": "object"}, False, {"type": "string"})
    data = resp.json()
    assert data["merged"] is False
    assert data["conflicts"][0]["pointer"] == ""
    assert data["conflicts"][0]["kind"] == KIND_BOTH_MODIFIED


def test_modify_vs_delete_conflict():
    base = {"properties": {"name": {"type": "string", "minLength": 1}}}
    a = {"properties": {"name": {"type": "string", "minLength": 3}}}  # 甲改
    b = {"properties": {}}  # 乙删
    data = run(base, a, b).json()
    assert data["merged"] is False
    assert data["conflicts"] == [
        {
            "pointer": "/properties/name",
            "kind": KIND_MODIFY_VS_DELETE,
            "deleted_by": "b",
            "base": {"type": "string", "minLength": 1},
            "value": {"type": "string", "minLength": 3},
        }
    ]


def test_delete_vs_modify_conflict_symmetric():
    base = {"properties": {"name": {"type": "string"}}}
    a = {"properties": {}}  # 甲删
    b = {"properties": {"name": {"type": "string", "maxLength": 8}}}  # 乙改
    data = run(base, a, b).json()
    assert data["merged"] is False
    conflict = data["conflicts"][0]
    assert conflict["pointer"] == "/properties/name"
    assert conflict["kind"] == KIND_MODIFY_VS_DELETE
    assert conflict["deleted_by"] == "a"
    assert conflict["value"] == {"type": "string", "maxLength": 8}


def test_array_is_atomic_conflict():
    # 数组视为整体：双方对同一数组作不同修改即冲突，不做数组成员级合并。
    base = {"required": ["id", "name"]}
    a = {"required": ["id", "name", "age"]}
    b = {"required": ["id"]}
    data = run(base, a, b).json()
    assert data["merged"] is False
    assert data["conflicts"] == [
        {
            "pointer": "/required",
            "kind": KIND_BOTH_MODIFIED,
            "base": ["id", "name"],
            "a": ["id", "name", "age"],
            "b": ["id"],
        }
    ]


def test_both_added_different_values_conflict():
    base = {"type": "object"}
    a = {"type": "object", "description": "甲的描述"}
    b = {"type": "object", "description": "乙的描述"}
    data = run(base, a, b).json()
    assert data["merged"] is False
    conflict = data["conflicts"][0]
    assert conflict["pointer"] == "/description"
    assert conflict["kind"] == KIND_BOTH_ADDED
    assert "base" not in conflict
    assert conflict["a"] == "甲的描述"
    assert conflict["b"] == "乙的描述"


def test_all_conflicts_are_listed():
    base = {"properties": {"x": {"minimum": 0}, "y": {"minLength": 1}}}
    a = {"properties": {"x": {"minimum": 1}, "y": {"minLength": 2}}}
    b = {"properties": {"x": {"minimum": 3}, "y": {"minLength": 4}}}
    data = run(base, a, b).json()
    assert data["merged"] is False
    assert data["merged_schema"] is None
    pointers = [c["pointer"] for c in data["conflicts"]]
    assert pointers == ["/properties/x/minimum", "/properties/y/minLength"]


def test_conflict_subtree_not_recursed():
    # 同一节点整体冲突时只报告一次（不深入子树逐个报告）。
    base = {"properties": {"p": {"minimum": 0, "maximum": 9}}}
    a = {"properties": {"p": False}}
    b = {"properties": {"p": True}}
    data = run(base, a, b).json()
    assert [c["pointer"] for c in data["conflicts"]] == ["/properties/p"]


def test_determinism():
    base = {"properties": {"x": {"minimum": 0}}, "required": ["x"]}
    a = {"properties": {"x": {"minimum": 1}, "y": {"type": "string"}}, "required": ["x", "y"]}
    b = {"properties": {"x": {"minimum": 0}, "z": {"type": "integer"}}, "required": ["x"]}
    first = run(base, a, b).json()
    second = run(base, a, b).json()
    assert first == second


# ---- 直接测试 SchemaMerger ---------------------------------------------------


def test_schema_merger_direct_use():
    merger = SchemaMerger({"a": 1}, {"a": 2}, {"a": 1})
    result = merger.merge()
    assert result.merged is True
    assert result.merged_schema == {"a": 2}
    assert result.conflicts == []


def test_schema_merger_conflict_result_shape():
    merger = SchemaMerger({"a": 1}, {"a": 2}, {"a": 3})
    result = merger.merge()
    assert result.merged is False
    assert result.merged_schema is None
    assert [c.kind for c in result.conflicts] == [KIND_BOTH_MODIFIED]
    assert result.to_dict()["conflicts"][0]["pointer"] == "/a"


def test_json_equality_bool_not_equal_to_int():
    # true 与 1 是不同取值：甲改成 true、乙保持 1 应视为仅甲修改。
    merger = SchemaMerger({"const": 1}, {"const": True}, {"const": 1})
    result = merger.merge()
    assert result.merged is True
    assert result.merged_schema == {"const": True}
