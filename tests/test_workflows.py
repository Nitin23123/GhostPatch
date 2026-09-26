"""The GitHub Actions workflows GhostPatch can add to a repository."""

from pathlib import Path

import pytest

from ghostpatch import cli, workflows

DOCS = Path(__file__).resolve().parent.parent / "docs"


@pytest.mark.parametrize("name", list(workflows.WORKFLOWS))
def test_the_packaged_workflows_match_the_docs(name):
    workflow = workflows.WORKFLOWS[name]
    assert (DOCS / Path(workflow.docs_file).name).read_text(encoding="utf-8") == workflow.yaml


def test_the_cli_lists_prints_and_writes_workflows(tmp_path: Path, capsys):
    assert cli.main(["workflow", "--repo", str(tmp_path)]) == 0
    assert "nightshift" in capsys.readouterr().out
    assert cli.main(["workflow", "issues", "--repo", str(tmp_path)]) == 0
    assert "task: fix-issue" in capsys.readouterr().out
    assert cli.main(["workflow", "ci", "--write", "--repo", str(tmp_path)]) == 0
    assert (tmp_path / ".github" / "workflows" / "ghostpatch.yml").is_file()
    assert cli.main(["workflow", "ci", "--write", "--repo", str(tmp_path)]) == 1  # exists: needs --force
    assert cli.main(["workflow", "ci", "--write", "--force", "--repo", str(tmp_path)]) == 0
