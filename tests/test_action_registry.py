import textwrap

import pytest

from openmux.server.actions.errors import ActionValidationError
from openmux.server.actions.registry import ActionParam, load_action_from_file, redact_params, validate_params

SCRIPT_TEMPLATE = textwrap.dedent(
    """
    ACTION = {{
        "id": "sample",
        "name": "Sample Action",
        "description": "A sample action for tests.",
        "timeout": 5.0,
        "params": [
            {{"name": "host", "type": "str", "required": True}},
            {{"name": "retries", "type": "int", "required": False, "default": 3}},
            {{"name": "password", "type": "str", "required": True, "sensitive": True}},
        ],
    }}

    async def run(session, params):
        {body}
    """
)


def _write_script(tmp_path, body="pass"):
    path = tmp_path / "sample_action.py"
    path.write_text(SCRIPT_TEMPLATE.format(body=body))
    return str(path)


def test_load_action_from_file_parses_metadata(tmp_path):
    action = load_action_from_file(_write_script(tmp_path))
    assert action.id == "sample"
    assert action.name == "Sample Action"
    assert action.timeout == 5.0
    assert [p.name for p in action.params] == ["host", "retries", "password"]
    assert action.param("password").sensitive is True
    assert callable(action.run_func)


def test_load_action_from_file_missing_file_raises(tmp_path):
    with pytest.raises(ActionValidationError):
        load_action_from_file(str(tmp_path / "does_not_exist.py"))


def test_load_action_from_file_missing_action_dict_raises(tmp_path):
    path = tmp_path / "no_meta.py"
    path.write_text("async def run(session, params):\n    pass\n")
    with pytest.raises(ActionValidationError):
        load_action_from_file(str(path))


def test_load_action_from_file_missing_run_raises(tmp_path):
    path = tmp_path / "no_run.py"
    path.write_text("ACTION = {'id': 'x', 'name': 'x', 'params': []}\n")
    with pytest.raises(ActionValidationError):
        load_action_from_file(str(path))


def test_action_param_rejects_unsupported_type():
    with pytest.raises(ActionValidationError):
        ActionParam(name="bad", type="list")


def test_action_param_select_widget_normalizes_choices():
    param = ActionParam(name="device_type", widget="select", choices=["router", {"label": "L3 switch", "value": "switch"}])
    assert param.choices == [
        {"label": "router", "value": "router"},
        {"label": "L3 switch", "value": "switch"},
    ]


def test_action_param_radio_widget_normalizes_choices():
    param = ActionParam(name="device_type", widget="radio", choices=["router", "switch"])
    assert param.choices == [
        {"label": "router", "value": "router"},
        {"label": "switch", "value": "switch"},
    ]


def test_action_param_select_widget_requires_choices():
    with pytest.raises(ActionValidationError):
        ActionParam(name="device_type", widget="select")


def test_action_param_radio_widget_requires_choices():
    with pytest.raises(ActionValidationError):
        ActionParam(name="device_type", widget="radio", choices=[])


def test_action_param_rejects_unsupported_widget():
    with pytest.raises(ActionValidationError):
        ActionParam(name="bad", widget="checkbox")


def test_validate_params_applies_defaults_and_coerces_types(tmp_path):
    action = load_action_from_file(_write_script(tmp_path))
    result = validate_params(action, {"host": "dev1", "password": "s3cret"})
    assert result == {"host": "dev1", "retries": 3, "password": "s3cret"}

    result2 = validate_params(action, {"host": "dev1", "retries": "5", "password": "x"})
    assert result2["retries"] == 5


def test_validate_params_rejects_unknown_param(tmp_path):
    action = load_action_from_file(_write_script(tmp_path))
    with pytest.raises(ActionValidationError):
        validate_params(action, {"host": "dev1", "password": "x", "bogus": "1"})


def test_validate_params_rejects_missing_required_param(tmp_path):
    action = load_action_from_file(_write_script(tmp_path))
    with pytest.raises(ActionValidationError):
        validate_params(action, {"password": "x"})


def test_validate_params_rejects_badly_typed_value(tmp_path):
    action = load_action_from_file(_write_script(tmp_path))
    with pytest.raises(ActionValidationError):
        validate_params(action, {"host": "dev1", "password": "x", "retries": "not-a-number"})


def test_redact_params_hides_sensitive_values(tmp_path):
    action = load_action_from_file(_write_script(tmp_path))
    validated = validate_params(action, {"host": "dev1", "password": "s3cret"})
    redacted = redact_params(action, validated)
    assert redacted["password"] == "<redacted>"
    assert redacted["host"] == "dev1"
