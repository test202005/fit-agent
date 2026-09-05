import pytest

from backend.clock import FrozenClock
from backend.storage import FakeStorage, RECORD_FIELDS
from backend.tools import TOOL_NAMES, TOOL_SCHEMAS, ToolError, make_executors


NOW = "2026-09-03T20:00:00+08:00"

EXPECTED_PARAMETERS = {
    "create_record": {
        "properties": {
            "exercise": "string",
            "weight_kg": "number",
            "sets": "number",
            "reps": "number",
            "duration_min": "number",
            "distance_km": "number",
        },
        "required": {"exercise"},
    },
    "query_records": {
        "properties": {"date": "string"},
        "required": {"date"},
    },
    "count_exercise": {
        "properties": {
            "exercise": "string",
            "from": "string",
            "to": "string",
        },
        "required": {"exercise", "from", "to"},
    },
}


def _schemas_by_name():
    return {item["function"]["name"]: item for item in TOOL_SCHEMAS}


def test_tool_schemas_match_declared_contract():
    schemas = _schemas_by_name()

    assert len(schemas) == len(TOOL_SCHEMAS)
    assert set(schemas) == set(EXPECTED_PARAMETERS) == TOOL_NAMES
    assert set(EXPECTED_PARAMETERS["create_record"]["properties"]) == set(RECORD_FIELDS)

    for name, expected in EXPECTED_PARAMETERS.items():
        schema = schemas[name]
        function = schema["function"]
        parameters = function["parameters"]

        assert schema["type"] == "function"
        assert function["description"].strip()
        assert parameters["type"] == "object"
        assert parameters["additionalProperties"] is False
        assert set(parameters["required"]) == expected["required"]
        assert set(parameters["required"]) <= set(parameters["properties"])
        assert {
            field: definition["type"]
            for field, definition in parameters["properties"].items()
        } == expected["properties"]


def test_every_schema_has_an_executor():
    executors = make_executors(FakeStorage(), FrozenClock(NOW), "t-schema")
    assert set(executors) == TOOL_NAMES


@pytest.mark.parametrize(
    ("tool_name", "valid_args"),
    [
        ("create_record", {"exercise": "卧推"}),
        ("query_records", {"date": "2026-09-03"}),
        (
            "count_exercise",
            {"exercise": "卧推", "from": "2026-09-01", "to": "2026-09-03"},
        ),
    ],
)
def test_executors_reject_fields_forbidden_by_schema(tool_name, valid_args):
    executors = make_executors(FakeStorage(), FrozenClock(NOW), "t-schema")
    with pytest.raises(ToolError):
        executors[tool_name]({**valid_args, "unknown": "value"})
