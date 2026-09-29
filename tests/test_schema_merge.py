"""POST /schema-merge 的端到端 / 单元测试。运行：python -m pytest -q"""

from fastapi.testclient import TestClient

from app.main import app
from app.merger import (
    KIND_ADD_ADD,
    KIND_DELETE_MODIFY,
    KIND_MODIFY_MODIFY,
    json_equal,
    merge_schemas,
)

client = TestClient(app)

DRAFT7 = "http://json-schema.org/draft-07/schema#"
DRAFT4 = "http://json-schema.org/draft-04/schema#"


def run(base, a, b):
    return client.post(
        "/schema-merge",
        json={"base_schema": base, "schema_a": a, "schema_b": b},
    )


# ---- HTTP 层：输入合法性 ----------------------------------------------------


def test_healthz_still_ok():
    assert client.get("/healthz").json() == {"status": "ok"}


def test_missing_fields_returns_422():
    resp = client.post("/schema-merge", json={"base_schema": {}})
    assert resp.status_code == 422


def test_invalid_base_reported_first():
    resp = run({"type": "nope"}, {"type": "integer"}, {"type": "string"})
    assert resp.status_code == 422
    data = resp.json()
    assert data["error"] == "invalid_schema"
    assert data["which"] == "base_schema"
    assert data["path"] == ["type"]
    assert data["pointer"] == "/type"
    assert data["message"]


def test_invalid_a_reported_with_version_and_location():
    resp = run({}, {"properties": {"x": {"type": "nope"}}}, {})
    assert resp.status_code == 422
    data = resp.json()
    assert data["which"] == "schema_a"
    assert data["path"] == ["properties", "x", "type"]
    assert data["pointer"] == "/properties/x/type"


def test_invalid_b_reported():
    resp = run({}, {}, {"required": "not-an-array"})
    assert resp.status_code == 422
    assert resp.json()["which"] == "schema_b"


def test_root_bad_ref_treated_as_invalid():
    # check_schema 通过，但根级 $ref 无法解析：探测阶段按非法处理。
    resp = run({"$ref": "#/$defs/missing"}, {"$ref": "#/$defs/missing"}, True)
    assert resp.status_code == 422
    data = resp.json()
    assert data["which"] == "base_schema"
    assert data["path"] == []
    assert "无法用于校验" in data["message"]


def test_base_must_be_valid_even_when_others_touch_it():
    resp = run({"type": 1}, {}, {})
    assert resp.status_code == 422
    assert resp.json()["which"] == "base_schema"


def test_non_object_non_boolean_schema_rejected():
    for bad in ("not-a-schema", 42, ["not-a-schema"], None):
        resp = run(bad, {}, {})
        assert resp.status_code == 422, resp.json()
        assert resp.json()["which"] == "base_schema"


# ---- HTTP 层：无冲突合并 ----------------------------------------------------


def test_disjoint_member_edits_merge():
    base = {"type": "object", "properties": {"id": {"type": "integer"}}}
    a = {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}
    b = {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
    }
    resp = run(base, a, b)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "merged"
    assert data["conflicts"] == []
    assert data["conflict_count"] == 0
    assert data["merged"] == {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
        "required": ["id"],
    }


def test_same_edit_kept_once():
    base = {"type": "integer"}
    a = b = {"type": "integer", "minimum": 1}
    data = run(base, a, b).json()
    assert data["status"] == "merged"
    assert data["merged"] == {"type": "integer", "minimum": 1}


def test_same_draft_switch_kept_once_with_disjoint_edits():
    base = {"type": "object"}
    a = {"type": "object", "$schema": DRAFT7, "required": ["id"]}
    b = {"type": "object", "$schema": DRAFT7, "title": "t"}
    data = run(base, a, b).json()
    assert data["status"] == "merged"
    assert data["merged"] == {
        "type": "object",
        "$schema": DRAFT7,
        "required": ["id"],
        "title": "t",
    }


def test_one_side_only_edit_taken():
    base = {"type": "object"}
    a = {"type": "object", "additionalProperties": False}
    data = run(base, a, base).json()
    assert data["merged"] == {"type": "object", "additionalProperties": False}


def test_both_delete_same_member():
    base = {"type": "object", "title": "t"}
    a = {"type": "object"}
    b = {"type": "object"}
    data = run(base, a, b).json()
    assert data["merged"] == {"type": "object"}


def test_delete_one_side_modify_other_side_no_touch():
    # 甲删除 title，乙没碰 title：删除生效。
    base = {"type": "object", "title": "t"}
    a = {"type": "object"}
    b = {"type": "object", "title": "t", "description": "d"}
    data = run(base, a, b).json()
    assert data["merged"] == {"type": "object", "description": "d"}


def test_identical_branches_pass_through_unchanged():
    base = {"type": "object", "properties": {"a": {"type": "integer", "minimum": 0}}}
    a = {"type": "object", "properties": {"a": {"type": "integer", "minimum": 0}}}
    b = {"type": "object", "properties": {"a": {"type": "integer", "minimum": 0}}, "title": "x"}
    data = run(base, a, b).json()
    # properties 子树两边相同（深比较，非同对象），整体保留，不因引用相同而误判。
    assert data["merged"]["properties"] == base["properties"]
    assert data["merged"]["title"] == "x"


def test_boolean_schema_inputs():
    # 基版 true，甲改成 false，乙保持 true：采用甲。
    data = run(True, False, True).json()
    assert data["status"] == "merged"
    assert data["merged"] is False


def test_key_order_is_deterministic_base_first():
    base = {"z": 1}
    a = {"z": 1, "a": 2}
    b = {"z": 1, "m": 3}
    data = run(base, a, b).json()
    assert list(data["merged"].keys()) == ["z", "a", "m"]


# ---- HTTP 层：冲突 ----------------------------------------------------------


def test_modify_modify_scalar_conflict():
    base = {"type": "integer"}
    a = {"type": "string"}
    b = {"type": "number"}
    resp = run(base, a, b)
    data = resp.json()
    assert data["status"] == "conflict"
    assert data["merged"] is None
    assert data["conflict_count"] == 1
    c = data["conflicts"][0]
    assert c["pointer"] == "/type"
    assert c["kind"] == KIND_MODIFY_MODIFY
    assert c["base"] == "integer"
    assert c["a"] == "string"
    assert c["b"] == "number"
    assert c["a_present"] and c["b_present"] and c["base_present"]
    assert c["message"]


def test_array_modified_differently_is_whole_array_conflict():
    # required 是数组：甲、乙各自追加不同元素，不逐元素合并，直接整数组冲突。
    base = {"type": "object", "required": ["id"]}
    a = {"type": "object", "required": ["id", "name"]}
    b = {"type": "object", "required": ["id", "age"]}
    data = run(base, a, b).json()
    assert data["status"] == "conflict"
    assert data["merged"] is None
    assert [(c["pointer"], c["kind"]) for c in data["conflicts"]] == [
        ("/required", KIND_MODIFY_MODIFY)
    ]
    assert "数组" in data["conflicts"][0]["message"]


def test_scalar_modify_modify_message_does_not_mention_array():
    data = run({"v": 1}, {"v": 2}, {"v": 3}).json()
    msg = data["conflicts"][0]["message"]
    assert "数组" not in msg
    assert "取值不同" in msg


def test_array_changed_vs_unchanged_takes_changed():
    base = {"required": ["id"]}
    a = {"required": ["id", "name"]}
    b = {"required": ["id"]}
    data = run(base, a, b).json()
    assert data["status"] == "merged"
    assert data["merged"]["required"] == ["id", "name"]


def test_array_same_edit_kept_once():
    base = {"required": ["id"]}
    a = b = {"required": ["id", "name"]}
    data = run(base, a, b).json()
    assert data["status"] == "merged"
    assert data["merged"]["required"] == ["id", "name"]


def test_nested_object_modify_modify_pointer():
    base = {"properties": {"id": {"type": "integer", "minimum": 0}}}
    a = {"properties": {"id": {"type": "integer", "minimum": 1}}}
    b = {"properties": {"id": {"type": "integer", "minimum": 2}}}
    data = run(base, a, b).json()
    assert data["status"] == "conflict"
    assert data["conflicts"][0]["pointer"] == "/properties/id/minimum"


def test_nested_object_disjoint_deep_edits_merge():
    base = {"properties": {"id": {"type": "integer"}, "name": {"type": "string"}}}
    a = {"properties": {"id": {"type": "integer", "minimum": 1}, "name": {"type": "string"}}}
    b = {"properties": {"id": {"type": "integer"}, "name": {"type": "string", "minLength": 2}}}
    data = run(base, a, b).json()
    assert data["status"] == "merged"
    assert data["merged"] == {
        "properties": {
            "id": {"type": "integer", "minimum": 1},
            "name": {"type": "string", "minLength": 2},
        }
    }


def test_delete_modify_conflict_both_directions():
    base = {"type": "object", "title": "t"}

    # 甲删除、乙修改。
    data = run(base, {"type": "object"}, {"type": "object", "title": "new"}).json()
    assert data["status"] == "conflict"
    c = data["conflicts"][0]
    assert c["pointer"] == "/title"
    assert c["kind"] == KIND_DELETE_MODIFY
    assert c["a_present"] is False
    assert c["b_present"] is True
    assert c["b"] == "new"
    assert c["base"] == "t"
    assert data["merged"] is None

    # 乙删除、甲修改。
    data = run(base, {"type": "object", "title": "new"}, {"type": "object"}).json()
    c = data["conflicts"][0]
    assert c["kind"] == KIND_DELETE_MODIFY
    assert c["a_present"] is True
    assert c["b_present"] is False
    assert c["a"] == "new"


def test_add_add_same_value_kept_once():
    base = {"type": "object"}
    a = {"type": "object", "required": ["id"]}
    b = {"type": "object", "required": ["id"]}
    data = run(base, a, b).json()
    assert data["status"] == "merged"
    assert data["merged"]["required"] == ["id"]


def test_add_add_different_values_conflict():
    base = {"type": "object"}
    a = {"type": "object", "required": ["id"]}
    b = {"type": "object", "required": ["name"]}
    data = run(base, a, b).json()
    assert data["status"] == "conflict"
    c = data["conflicts"][0]
    assert c["pointer"] == "/required"
    assert c["kind"] == KIND_ADD_ADD
    assert c["base_present"] is False
    assert c["a"] == ["id"]
    assert c["b"] == ["name"]


def test_multiple_conflicts_all_reported_sorted():
    base = {"type": "integer", "minimum": 0, "maximum": 10, "title": "t"}
    # 甲：改 type/maximum、删 title（minimum 单边改，应合并）
    a = {"type": "string", "minimum": 1, "maximum": 5}
    # 乙：改 type/maximum/title
    b = {"type": "number", "minimum": 0, "maximum": 20, "title": "new"}
    data = run(base, a, b).json()
    assert data["status"] == "conflict"
    assert data["merged"] is None
    pointers = [c["pointer"] for c in data["conflicts"]]
    assert pointers == sorted(pointers)
    assert set(pointers) == {"/maximum", "/title", "/type"}
    kinds = {c["pointer"]: c["kind"] for c in data["conflicts"]}
    assert kinds["/title"] == KIND_DELETE_MODIFY
    assert kinds["/type"] == KIND_MODIFY_MODIFY
    assert kinds["/maximum"] == KIND_MODIFY_MODIFY
    # 单边修改不进冲突列表。
    assert "/minimum" not in pointers


def test_root_level_modify_modify_conflict():
    # 基版对象，甲改成 true、乙改成 false：根位置冲突，pointer 为空串。
    data = run({"type": "integer"}, True, False).json()
    assert data["status"] == "conflict"
    (c,) = data["conflicts"]
    assert c["pointer"] == ""
    assert c["kind"] == KIND_MODIFY_MODIFY


def test_type_change_at_same_node_is_modify_modify():
    # 甲把节点改成标量、乙改成数组：类型本身不同，按整体冲突。
    base = {"value": {"type": "integer"}}
    a = {"value": True}
    b = {"value": [1, 2]}
    data = run(base, a, b).json()
    assert data["status"] == "conflict"
    assert data["conflicts"][0]["pointer"] == "/value"
    assert data["conflicts"][0]["kind"] == KIND_MODIFY_MODIFY


# ---- HTTP 层：合并结果再校验 ------------------------------------------------


def test_merged_schema_validated_and_rejected_without_output():
    # 基版 {}；甲切到 draft4 标记；乙加布尔子 schema（2020-12 合法、draft4 非法）。
    # 三版输入各自合法、修改互不相交，但合并结果不合法。
    base = {}
    a = {"$schema": DRAFT4}
    b = {"properties": {"id": False}}
    resp = run(base, a, b)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "invalid_merged"
    assert data["merged"] is None
    assert data["conflicts"] == []
    assert data["merged_error"]["pointer"] == "/properties/id"
    assert data["merged_error"]["path"] == ["properties", "id"]
    assert data["merged_error"]["message"]


def test_merged_valid_after_draft_switch_with_boolean_free_edit():
    # 同样切 draft4，但乙加的是对象子 schema：合并合法。
    data = run({}, {"$schema": DRAFT4}, {"properties": {"id": {"type": "integer"}}}).json()
    assert data["status"] == "merged"
    assert data["merged"] == {
        "$schema": DRAFT4,
        "properties": {"id": {"type": "integer"}},
    }


# ---- 确定性 -----------------------------------------------------------------


def test_determinism():
    base = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["x"]}
    a = {"type": "object", "properties": {"a": {"type": "integer", "minimum": 1}}, "required": ["x"]}
    b = {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "string"}}}
    first = run(base, a, b).json()
    second = run(base, a, b).json()
    assert first == second


# ---- 内核单元：json_equal ---------------------------------------------------


def test_json_equal_bool_vs_number():
    assert json_equal(True, True)
    assert not json_equal(True, 1)
    assert not json_equal(False, 0)
    assert json_equal(1, 1.0)
    assert json_equal({"a": [1, True]}, {"a": [1, True]})
    assert not json_equal({"a": [1, True]}, {"a": [1, 1]})
    assert not json_equal([1], {"a": 1})
    assert json_equal(None, None)
    assert not json_equal(None, False)


# ---- 内核单元：直接调用 merge_schemas ---------------------------------------


def test_merge_schemas_no_conflict_returns_merged():
    outcome = merge_schemas(
        {"type": "object", "x": 1},
        {"type": "object", "x": 1, "y": 2},
        {"type": "object", "x": 1, "z": 3},
    )
    assert outcome.ok
    assert outcome.merged == {"type": "object", "x": 1, "y": 2, "z": 3}
    assert outcome.conflicts == []


def test_merge_schemas_conflict_merged_is_none():
    outcome = merge_schemas({"v": 1}, {"v": 2}, {"v": 3})
    assert not outcome.ok
    assert outcome.merged is None
    assert outcome.conflicts[0].pointer == "/v"


def test_dictionary_payload_shape():
    outcome = merge_schemas({"v": 1}, {"v": 2}, {"v": 3})
    d = outcome.to_dict()
    assert d["merged"] is None
    assert d["conflicts"][0]["pointer"] == "/v"
    assert d["conflicts"][0]["kind"] == KIND_MODIFY_MODIFY
    assert d["conflicts"][0]["base_present"]
