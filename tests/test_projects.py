import logging
import os
from pathlib import Path

import pytest

from app.core.errors import NotFoundError
from app.ingestion.projects import find_project, scan_projects

WSO2_YAML = "title: WSO2 API Gateway\ntype: wso2\ntimezone: Asia/Kolkata\n"


def write_project(root: Path, name: str, yaml_text: str | bytes | None, *files: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    if isinstance(yaml_text, bytes):
        (folder / "project.yaml").write_bytes(yaml_text)
    elif yaml_text is not None:
        (folder / "project.yaml").write_text(yaml_text, encoding="utf-8")
    for file in files:
        (folder / file).parent.mkdir(parents=True, exist_ok=True)
        (folder / file).write_text("line\n", encoding="utf-8")
    return folder


def test_a_wso2_project_loads_with_its_preset(tmp_path):
    write_project(tmp_path, "wso2-gateway", WSO2_YAML, "wso2carbon-10-30-2026.1.log")

    [project] = scan_projects(tmp_path)

    assert project.status == "ok" and project.error is None
    assert project.title == "WSO2 API Gateway" and project.type == "wso2"
    [file] = project.files
    assert file.id == "wso2-gateway/wso2carbon-10-30-2026.1"
    assert file.event_start.pattern == r"^TID: \["
    assert file.app_packages == ("org.wso2",)
    assert file.timezone == "Asia/Kolkata"
    assert file.format == "text"


def test_a_json_project_reads_json(tmp_path):
    write_project(tmp_path, "api", "title: API\ntype: json\n", "app.log")
    assert scan_projects(tmp_path)[0].files[0].format == "json"


def test_overrides_beat_the_preset(tmp_path):
    write_project(tmp_path, "p", WSO2_YAML + "event_start: '^X'\napp_packages: [com.acme]\n", "a.log")
    [file] = scan_projects(tmp_path)[0].files
    assert file.event_start.pattern == "^X"
    assert file.app_packages == ("com.acme",)


def test_include_glob_filters_files_and_project_yaml_is_never_a_file(tmp_path):
    write_project(tmp_path, "p", WSO2_YAML, "a.log", "b.log.1", "notes.txt", ".hidden.log", "prod/c.log")
    assert [f.id for f in scan_projects(tmp_path)[0].files] == ["p/a", "p/b.log.1"]


def test_include_can_narrow_the_files(tmp_path):
    write_project(tmp_path, "p", WSO2_YAML + 'include: "wso2carbon-*.log"\n', "wso2carbon-1.log", "other.log")
    assert [f.id for f in scan_projects(tmp_path)[0].files] == ["p/wso2carbon-1"]


def test_folder_without_project_yaml_is_not_a_project(tmp_path):
    write_project(tmp_path, "loose", None, "a.log")
    assert scan_projects(tmp_path) == ()


@pytest.mark.parametrize("bad", [
    "",
    "- a\n- b",
    "title: x\ntype: nope\n",
    "title: x\ntype: wso2\ncolour: red\n",
    "title: x\ntype: wso2\ntimezone: Mars/Base\n",
    "type: wso2\n",
    "title: ''\ntype: wso2\n",
    f"title: x\ntype: wso2\ndescription: {'d' * 301}\n",
    "title: x\ntype: wso2\nevent_start: '('\n",
    "title: [unclosed\n",
    b"\xff\xfe\x00bad",
    "title: x\ntype: [wso2]\n",
    "title: x\ntype: {a: 1}\n",
    "title: x\ntype: wso2\ninclude: 7\n",
])
def test_bad_yaml_marks_only_that_project_invalid(tmp_path, bad):
    write_project(tmp_path, "broken", bad, "a.log")
    write_project(tmp_path, "good", WSO2_YAML, "a.log")

    broken, good = scan_projects(tmp_path)

    assert broken.id == "broken" and broken.status == "invalid" and broken.error
    assert broken.files == () and broken.title == "broken"
    assert good.status == "ok" and len(good.files) == 1


def test_a_utf8_bom_is_accepted(tmp_path):
    write_project(tmp_path, "p", b"\xef\xbb\xbf" + WSO2_YAML.encode(), "a.log")
    assert scan_projects(tmp_path)[0].status == "ok"


def test_bad_folder_name_is_skipped(tmp_path, caplog):
    write_project(tmp_path, "Bad Name", WSO2_YAML, "a.log")
    with caplog.at_level(logging.WARNING):
        assert scan_projects(tmp_path) == ()
    assert "Bad Name" in caplog.text


def test_unusable_file_names_are_skipped_with_a_reason(tmp_path):
    write_project(tmp_path, "p", WSO2_YAML, "my app.log", "ok.log")

    [project] = scan_projects(tmp_path)

    assert [f.id for f in project.files] == ["p/ok"]
    [skipped] = project.skipped
    assert skipped.name == "my app.log" and "name" in skipped.reason


def test_files_that_clash_on_one_id_are_both_skipped(tmp_path):
    folder = write_project(tmp_path, "p", WSO2_YAML, "app.log", "ok.log")
    if (folder / "APP.log").exists():
        pytest.skip("case-insensitive filesystem")
    (folder / "APP.log").write_text("x\n", encoding="utf-8")

    [project] = scan_projects(tmp_path)

    assert [f.id for f in project.files] == ["p/ok"]
    assert sorted(s.name for s in project.skipped) == ["APP.log", "app.log"]
    assert all("same id" in s.reason for s in project.skipped)


def test_two_extensions_clash_on_one_stem(tmp_path):
    write_project(tmp_path, "p", WSO2_YAML + 'include: "a.*"\n', "a.log", "a.txt")
    [project] = scan_projects(tmp_path)
    assert project.files == ()
    assert len(project.skipped) == 2


def test_symlink_outside_the_folder_is_skipped(tmp_path):
    outside = tmp_path / "secret.log"
    outside.write_text("x\n", encoding="utf-8")
    folder = write_project(tmp_path / "root", "p", WSO2_YAML)
    try:
        os.symlink(outside, folder / "link.log")
    except OSError:
        pytest.skip("symlinks need privileges here")

    [project] = scan_projects(tmp_path / "root")

    assert project.files == ()
    assert project.skipped[0].name == "link.log" and "outside" in project.skipped[0].reason


@pytest.mark.parametrize("project_id", ["nope", "../etc", "UPPER", "", "p/x"])
def test_find_project_has_one_message_for_unknown_and_malformed(tmp_path, project_id):
    write_project(tmp_path, "p", WSO2_YAML, "a.log")
    with pytest.raises(NotFoundError) as caught:
        find_project(scan_projects(tmp_path), project_id)
    assert caught.value.message == "No such project."


def test_find_project_returns_the_project(tmp_path):
    write_project(tmp_path, "p", WSO2_YAML, "a.log")
    assert find_project(scan_projects(tmp_path), "p").id == "p"


def test_missing_root_is_no_projects(tmp_path):
    assert scan_projects(tmp_path / "nowhere") == ()


def test_projects_are_sorted_by_id(tmp_path):
    for name in ("b", "a", "c"):
        write_project(tmp_path, name, WSO2_YAML)
    assert [p.id for p in scan_projects(tmp_path)] == ["a", "b", "c"]


def test_rotated_names_keep_distinct_ids(tmp_path):
    # logrotate/log4j naming: dropping only the last suffix gave app.log.1 and app.log.2 the same id
    write_project(tmp_path, "p", WSO2_YAML, "app.log", "app.log.1", "app.log.2", "wso2carbon-10-30-2026.1.log")

    [project] = scan_projects(tmp_path)

    assert [f.id for f in project.files] == ["p/app", "p/app.log.1", "p/app.log.2", "p/wso2carbon-10-30-2026.1"]
    assert project.skipped == ()
