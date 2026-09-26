"""End to end: the real `ghostpatch` command, as a user runs it, against a fake model over HTTP.

The other tests call GhostPatch's functions with a scripted client. These start the actual CLI
in a subprocess, pointed (with GHOSTPATCH_BASE_URL) at tests/fake_model.py, so argument parsing,
settings, the real `openai` client parsing real JSON, test runs, the proof, history and undo are
all exercised together, on a copy of examples/buggy-shop.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import fake_model

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "buggy-shop"


@pytest.fixture(scope="module")
def model_url():
    server = fake_model.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    repo = tmp_path / "shop"
    shutil.copytree(EXAMPLE, repo, ignore=shutil.ignore_patterns(".ghostpatch", "__pycache__"))
    return repo


def ghostpatch(repo: Path, model_url: str, *args: str, code: int = 0) -> str:
    env = {**os.environ, "GHOSTPATCH_PROVIDER": "ollama", "GHOSTPATCH_MODEL": "fake",
           "GHOSTPATCH_BASE_URL": model_url, "GHOSTPATCH_FALLBACK": "none", "GHOSTPATCH_APPROVE": "safe",
           "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", "COLUMNS": "200"}
    proc = subprocess.run([sys.executable, "-m", "ghostpatch", *args], cwd=repo, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=600)
    output = proc.stdout + proc.stderr
    assert proc.returncode == code, f"exit {proc.returncode}, expected {code}:\n{output[-4000:]}"
    return output


def pricing(repo: Path) -> str:
    return (repo / "shop" / "pricing.py").read_text(encoding="utf-8")


def test_fix_prove_history_and_undo(shop: Path, model_url: str):
    out = ghostpatch(shop, model_url, "fix", "The SAVE10 coupon charges $0.00", "--approve", "safe")
    assert "Bug fixed" in out and "Proven" in out and "Confidence" in out
    assert "percent / 100" in pricing(shop) and (shop / "tests" / "test_coupons.py").is_file()

    assert "fixed" in ghostpatch(shop, model_url, "history")
    ghostpatch(shop, model_url, "undo")
    assert "percent / 10," in pricing(shop) and not (shop / "tests" / "test_coupons.py").exists()


def test_tournament_with_poltergeist(shop: Path, model_url: str):
    out = ghostpatch(shop, model_url, "fix", "The SAVE10 coupon charges $0.00", "--candidates", "2", "--poltergeist", "1")
    assert "Fix tournament" in out and "won the tournament" in out and "survived the poltergeist" in out
    assert "percent / 100" in pricing(shop)  # the root-cause fix won, not the cart patch
    assert "coupon_percent / 10" not in (shop / "shop" / "cart.py").read_text(encoding="utf-8")


def test_haunt_finds_and_fixes_the_bug(shop: Path, model_url: str):
    ranking = ghostpatch(shop, model_url, "haunt", "--list")
    assert "shop.pricing.apply_discount" in ranking
    out = ghostpatch(shop, model_url, "haunt", "--only", "apply_discount", "--targets", "1", "--fix")
    assert "confirmed" in out and "Fixed 1 of 1 bug" in out
    assert "percent / 100" in pricing(shop) and (shop / "tests" / "test_haunt_apply_discount.py").is_file()


def test_ask_answers_with_the_call_flow(shop: Path, model_url: str):
    out = ghostpatch(shop, model_url, "ask", "How is the total calculated?")
    assert "Call flow" in out and "shop.cart.Cart.total" in out and "shop.pricing.apply_discount" in out
    assert pricing(shop) == (EXAMPLE / "shop" / "pricing.py").read_text(encoding="utf-8")  # read-only


def test_ci_fix_fixes_a_failing_suite(shop: Path, model_url: str):
    (shop / "tests" / "test_coupon_in_ci.py").write_text(
        "from shop.pricing import apply_discount\n\n\ndef test_ten_percent():\n    assert apply_discount(100, 10) == 90\n",
        encoding="utf-8")
    out = ghostpatch(shop, model_url, "ci-fix", "--mode", "report")
    assert "The tests pass now" in out and "percent / 100" in pricing(shop)


def test_doctor_reaches_the_model(shop: Path, model_url: str):
    out = ghostpatch(shop, model_url, "doctor")
    assert "reached ollama" in out and "fake is available" in out
