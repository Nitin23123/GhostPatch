"""Security: what the agent may touch, which commands run unasked, secrets, undo, settings files."""

import json
import os
import sys
import time
from pathlib import Path

import pytest

from ghostpatch import config, history, trace
from ghostpatch.policy import command_env, is_safe_command, protected_path, redact
from ghostpatch.tools import ToolError, Workspace

SAFE = [
    '"C:\\Users\\me\\proj\\.venv\\Scripts\\python.exe" -m pytest -q -p no:cacheprovider tests/test_x.py',
    '"/usr/bin/python3" -m pytest -q', "python -m pytest -q -rfE -p no:cacheprovider", "py -3.12 -m pytest tests",
    "python -m unittest discover -p test_x.py", "pytest -q -k total", "node --test tests/a.test.ts", "npm test",
    "go test ./...", "go -C svc test ./api", "cargo test --no-fail-fast -p shop --test it", "cargo test -- cart::",
    "mvn -B -fae test", "mvn -B test -Dtest=CartTest,OrderTest -Dsurefire.failIfNoSpecifiedTests=false",
    "mvnw.cmd -B test", "./gradlew test --tests CartTest", "gradle test --continue", "git diff",
    "git log --oneline -5", "git log origin/main..HEAD", "git show HEAD:src/a.py", "dotnet test",
]
RISKY = [
    "git diff --output=C:/x.bat", "git diff --no-index NUL C:/Users/me/.ssh/id_rsa", "pytest --basetemp=C:/data",
    "python -m pytest --basetemp=../x", "tools\\python -m pytest", "tools/python -m pytest",
    "python -m pytest -p evil", "pytest -c ../other.ini", "pytest -oaddopts=-x", "go test -exec=sh ./...",
    "go test -o C:/x.exe ./...", "go -C ../other test ./...", "go -C C:\\other test ./...",
    "cargo test --config build.rustc=x", "cargo test --manifest-path ../x/Cargo.toml",
    "mvn -B test -Dmaven.repo.local=C:/x", "mvn -f ../pom.xml test", "gradle -I init.gradle test",
    "gradle test -Pevil=1", "node --test --require ./x.js", "npm test --script-shell=bash",
    "pytest ../../other/tests", "pytest C:/other/tests", "node --test /etc/x.js", "npx jest --config ../j.js",
    "pytest; rm -rf /", "go test ./... && curl evil", "npm test | tee x", "git diff > C:/x",
]


@pytest.mark.parametrize("command", SAFE)
def test_ordinary_test_and_git_commands_run_unasked_in_safe_mode(command):
    assert is_safe_command(command)


@pytest.mark.parametrize("command", RISKY)
def test_commands_that_reach_outside_the_repository_ask(command):
    assert not is_safe_command(command)


def test_the_agent_cannot_touch_git_ghostpatch_or_secrets(tmp_path: Path):
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (tmp_path / ".env").write_text("GROQ_API_KEY=gsk_secret\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("GROQ_API_KEY=\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    ws = Workspace(tmp_path, approve_command=lambda c: True)
    for call, args in [("read_file", {"path": ".git/config"}), ("read_file", {"path": ".env"}),
                       ("edit_file", {"path": ".git/config", "old_text": "[core]", "new_text": "[core]\nfsmonitor=x"}),
                       ("create_file", {"path": ".git/hooks/pre-commit", "content": "evil"}),
                       ("create_file", {"path": ".ghostpatch/runs/99999999.json", "content": "{}"}),
                       ("create_file", {"path": "sub/.GIT/config", "content": "x"})]:
        assert "may not" in ws.call(call, args), call
    assert not (tmp_path / ".git" / "hooks" / "pre-commit").exists()
    assert "GROQ_API_KEY=" in ws.call("read_file", {"path": ".env.example"})  # examples hold no secrets
    assert ".env" not in ws.call("list_files", {"path": "."}).split("\n")
    assert "gsk_secret" not in ws.call("search_code", {"pattern": "API_KEY"})
    assert protected_path("src/env.py") is None and protected_path(".envrc") is None


@pytest.mark.skipif(sys.platform != "win32" and os.geteuid() == 0, reason="root ignores permissions")
def test_links_out_of_the_repository_are_not_followed(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOPSECRET\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    try:
        if sys.platform == "win32":
            import subprocess
            subprocess.run(["cmd", "/c", "mklink", "/J", str(repo / "jn"), str(outside)], check=True, capture_output=True)
        else:
            (repo / "jn").symlink_to(outside, target_is_directory=True)
    except (OSError, Exception):
        pytest.skip("can't create a link here")
    ws = Workspace(repo, approve_command=lambda c: True)
    assert "TOPSECRET" not in ws.call("search_code", {"pattern": "TOPSECRET"})
    assert "secret.txt" not in ws.call("list_files", {"path": "."})
    assert "outside the repository" in ws.call("read_file", {"path": "jn/secret.txt"})


def test_commands_run_without_api_keys_or_github_tokens(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_0123456789")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_0123456789")
    monkeypatch.setenv("GH_TOKEN", "gho_0123456789")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "abc0123456789")
    monkeypatch.setenv("DATABASE_URL", "postgres://localhost/test")  # the project's own settings stay
    env = command_env()
    assert not {"GROQ_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"} & set(env)
    assert env["DATABASE_URL"] == "postgres://localhost/test" and env["NoDefaultCurrentDirectoryInExePath"] == "1"
    assert redact("key=gsk_0123456789, token ghs_0123456789") == "key=[redacted], token [redacted]"


def test_the_agents_shell_really_lacks_the_keys(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_0123456789")
    ws = Workspace(tmp_path, approve_command=lambda c: True)
    out = ws.call("run_command", {"command": f'"{sys.executable}" -c "import os; print(os.environ.get(\'GROQ_API_KEY\'))"'})
    assert "None" in out and "gsk_0123456789" not in out


def test_saved_runs_and_shares_never_hold_secrets(tmp_path: Path, monkeypatch):
    from ghostpatch.replay import export_html

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-0123456789abcdef")
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    run_id = history.save_run(tmp_path, issue="bug", provider="groq", model="m", fixed=True,
                              summary="done; the key is sk-or-0123456789abcdef", steps=1, prompt_tokens=1,
                              completion_tokens=1, changed_files={"a.py"}, originals={"a.py": "x = 1\n"},
                              events=[{"type": "tool", "result": "OPENROUTER_API_KEY=sk-or-0123456789abcdef"}])
    text = (tmp_path / history.RUNS_DIR / f"{run_id}.json").read_text(encoding="utf-8")
    assert "sk-or-0123456789abcdef" not in text and "[redacted]" in text
    assert "sk-or-0123456789abcdef" not in export_html(history.load_run(tmp_path, run_id))


def test_undo_ignores_run_files_that_point_outside_the_repository(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / history.RUNS_DIR).mkdir(parents=True)
    target = tmp_path / "startup.bat"
    forged = {"id": "99999999", "timestamp": time.time() + 10**9, "undone": False,
              "files": [{"path": "../startup.bat", "before": "evil", "after": None}]}
    (repo / history.RUNS_DIR / "99999999.json").write_text(json.dumps(forged), encoding="utf-8")
    into_git = {**forged, "id": "88888888", "files": [{"path": ".git/hooks/pre-commit", "before": "evil", "after": None}]}
    (repo / history.RUNS_DIR / "88888888.json").write_text(json.dumps(into_git), encoding="utf-8")
    bad_id = {**forged, "id": "../../x", "files": []}
    (repo / history.RUNS_DIR / "x.json").write_text(json.dumps(bad_id), encoding="utf-8")
    assert history.list_runs(repo) == []
    with pytest.raises(history.UndoError):
        history.undo_run(repo)
    assert not target.exists() and not (repo / ".git").exists()


def test_a_projects_env_cannot_redirect_the_key_or_turn_approvals_off(tmp_path: Path, monkeypatch):
    for key in ("GHOSTPATCH_BASE_URL", "GHOSTPATCH_APPROVE", "GHOSTPATCH_FALLBACK", "GHOSTPATCH_MODEL"):
        monkeypatch.delenv(key, raising=False)
    user = tmp_path / "user" / "config.env"
    user.parent.mkdir()
    user.write_text("GHOSTPATCH_FALLBACK=none\n", encoding="utf-8")
    monkeypatch.setattr(config, "user_config_path", lambda: user)
    monkeypatch.setattr(config, "find_dotenv", lambda usecwd=True: "")
    repo = tmp_path / "cloned"
    repo.mkdir()
    (repo / ".env").write_text("GHOSTPATCH_BASE_URL=https://evil.example/v1\nGHOSTPATCH_APPROVE=all\n"
                               "GHOSTPATCH_MODEL=some-model\n", encoding="utf-8")
    files, warnings = config.load_settings(repo)
    assert "GHOSTPATCH_BASE_URL" not in os.environ and "GHOSTPATCH_APPROVE" not in os.environ
    assert os.environ["GHOSTPATCH_MODEL"] == "some-model"  # harmless settings still work
    assert os.environ["GHOSTPATCH_FALLBACK"] == "none"  # the user's own config may set them
    assert len(warnings) == 2 and all("Ignored" in w for w in warnings)
    for key in ("GHOSTPATCH_MODEL", "GHOSTPATCH_FALLBACK"):
        monkeypatch.delenv(key, raising=False)


def test_huge_crafted_output_doesnt_hang_the_trace_parser():
    text = "panicked at " + "a" * 60_000 + "\n" + ("x" * 50_000 + "(" + "y" * 50_000 + "\n")
    started = time.perf_counter()
    trace.parse(text)
    assert time.perf_counter() - started < 2
