"""Push sources: declared by push_token_env in project.yaml, next to a project's file sources."""

import pytest

from app.ingestion.projects import scan_projects
from tests.project_helpers import WSO2_YAML, make_project_folder

PUSH_YAML = WSO2_YAML + "push_token_env: ACME_TOKEN\n"


def only_project(tmp_path):
    (project,) = scan_projects(tmp_path)
    return project


def test_push_token_env_adds_push_source(tmp_path):
    make_project_folder(tmp_path, "demo", PUSH_YAML)
    project = only_project(tmp_path)
    assert project.error is None
    assert project.push_source.id == "demo/push"
    assert project.push_source.token_env == "ACME_TOKEN"
    assert project.push_source.timezone == "Asia/Kolkata"
    assert project.sources[-1] is project.push_source


def test_project_without_push_has_no_push_source(tmp_path):
    make_project_folder(tmp_path, "demo")
    project = only_project(tmp_path)
    assert project.push_source is None
    assert project.sources == project.files


@pytest.mark.parametrize("value", ["lower", "1BAD", "A" * 65, "123", "''"])
def test_bad_push_token_env_marks_project_invalid(tmp_path, value):
    make_project_folder(tmp_path, "demo", WSO2_YAML + f"push_token_env: {value}\n")
    assert "push_token_env must" in only_project(tmp_path).error


def test_file_named_push_is_skipped(tmp_path):
    folder = make_project_folder(tmp_path, "demo", PUSH_YAML)
    (folder / "push.log").write_text("x\n", encoding="utf-8")
    project = only_project(tmp_path)
    assert [f.name for f in project.skipped] == ["push.log"]
    assert project.files == ()
