"""
Test the validate_workflow_result helper function.
"""

from tests.utils.test_helpers import validate_workflow_result


def test_validate_workflow_result_success():
    """Test validation with a successful workflow result."""
    result = {
        "status": "completed",
        "output": "Workflow completed successfully",
        "final_state": {
            "definition": {
                "name": "Test Workflow",
                "version": "1.0",
                "tool_definitions": [],
                "nodes": [
                    {"id": "node1", "type": "Orchestrator", "config": {}},
                    {"id": "node2", "type": "Specialist", "config": {}},
                ],
                "edges": [{"source": "node1", "target": "node2", "type": "orchestrator"}],
            }
        },
    }

    is_valid, errors = validate_workflow_result(result, [])
    assert is_valid, f"Expected valid result, got errors: {errors}"
    assert len(errors) == 0
    print("✅ test_validate_workflow_result_success passed")


def test_validate_workflow_result_halt_error():
    """Test validation with a HALT error."""
    result = {
        "status": "completed",
        "output": "HALT: Logical Error: Missing input_schema because the `/THE_SPEC/requirements.md` file does not exist.",
        "final_state": {},
    }

    is_valid, errors = validate_workflow_result(result, [])
    assert not is_valid, "Expected invalid result for HALT error"
    assert len(errors) > 0
    assert any("HALT" in error for error in errors)
    print("✅ test_validate_workflow_result_halt_error passed")


def test_validate_workflow_result_missing_definition():
    """Test validation with missing/empty final_state (output does not indicate success)."""
    result = {"status": "completed", "output": "Some output", "final_state": {}}

    is_valid, errors = validate_workflow_result(result, [])
    assert not is_valid, "Expected invalid result for missing definition"
    assert any("output" in error.lower() for error in errors), (
        f"Expected output-related error, got: {errors}"
    )
    print("✅ test_validate_workflow_result_missing_definition passed")


def test_validate_workflow_result_empty_nodes():
    """Test validation with empty nodes (output does not indicate success)."""
    result = {
        "status": "completed",
        "output": "Some output",
        "final_state": {
            "definition": {
                "name": "Test Workflow",
                "version": "1.0",
                "tool_definitions": [],
                "nodes": [],
                "edges": [],
            }
        },
    }

    is_valid, errors = validate_workflow_result(result, [])
    assert not is_valid, "Expected invalid result for empty nodes"
    assert any("output" in error.lower() for error in errors)
    print("✅ test_validate_workflow_result_empty_nodes passed")


if __name__ == "__main__":
    test_validate_workflow_result_success()
    test_validate_workflow_result_halt_error()
    test_validate_workflow_result_missing_definition()
    test_validate_workflow_result_empty_nodes()
    print("\n✅ All validation helper tests passed!")
