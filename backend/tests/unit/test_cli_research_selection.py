"""Research CLI preserves API JSON and explains discovery selection."""

import json
from unittest.mock import MagicMock, patch

import httpx
from typer.testing import CliRunner

from msai.cli import app


def test_discovery_help_explains_training_and_window_index() -> None:
    result = CliRunner().invoke(app, ["research", "promote", "--help"])
    assert result.exit_code == 0
    assert "discovery" in result.output.lower()
    assert "training" in result.output.lower()
    assert "window" in result.output.lower()


def test_explicit_choice_transports_index_and_same_json() -> None:
    payload = {
        "candidate_id": "candidate-1",
        "stage": "discovery",
        "message": "Created exploratory discovery candidate",
    }
    response = MagicMock(spec=httpx.Response)
    response.is_success = True
    response.json.return_value = payload
    with patch("msai.cli.httpx.request", return_value=response) as request:
        result = CliRunner().invoke(
            app, ["research", "promote", "--job-id", "job-1", "--trial-index", "1"]
        )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == payload
    assert request.call_args.kwargs["json"] == {"research_job_id": "job-1", "trial_index": 1}


def test_show_preserves_selection_and_diagnostic_json() -> None:
    payload = {
        "selection": {"version": 1, "basis": "train", "scope": "exploratory"},
        "discovery_eligible": True,
        "results": {"holdout_error": "no bars"},
    }
    response = MagicMock(spec=httpx.Response)
    response.is_success = True
    response.json.return_value = payload
    with patch("msai.cli.httpx.request", return_value=response):
        result = CliRunner().invoke(app, ["research", "show", "job-1"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == payload
