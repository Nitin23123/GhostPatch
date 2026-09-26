"""Run the dashboard against a scripted, offline "model" for UI work and screenshots.

    python scripts/demo_server.py [--port 8770] [--delay 1.2]

It copies examples/buggy-shop into a temporary git repository (so the real example is never
touched), then serves the dashboard there with a fake model that plays out a realistic fix:
reading code, checking the blast radius, editing, running the tests, and a poltergeist round.
No API key or network is needed. Press "Fix it" in the browser to start the scripted run.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ghostpatch.bench import ensure_python_on_path  # noqa: E402
from ghostpatch.providers import PROVIDERS  # noqa: E402
from ghostpatch.server import Dashboard, create_server  # noqa: E402


def call(call_id: str, tool: str, **args):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=tool, arguments=json.dumps(args)))


def reply(content=None, *calls):
    message = SimpleNamespace(content=content, tool_calls=list(calls) or None)
    usage = SimpleNamespace(prompt_tokens=2400, completion_tokens=160)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def script(python: str) -> list:
    test = f"{python} -m pytest -q -p no:cacheprovider"
    return [
        reply("The coupon total is $0.00, so the discount is too large. Starting from pricing.",
              call("1", "read_file", path="shop/pricing.py"), call("2", "read_file", path="shop/cart.py")),
        reply(None, call("3", "impact_of_change", name="apply_discount")),
        reply("`percent / 10` turns 10% into 100%. It should divide by 100.",
              call("4", "edit_file", path="shop/pricing.py",
                   old_text="    return round(price - price * percent / 10, 2)",
                   new_text="    return round(price - price * percent / 100, 2)")),
        reply(None, call("5", "run_command", command=test)),
        reply(None, call("6", "create_file", path="tests/test_coupons.py", content=(
            "from shop.cart import Cart\nfrom shop.checkout import checkout\n\n\n"
            "def test_save10_coupon():\n    cart = Cart()\n    cart.add(\"mug\", 12.50, quantity=2)\n"
            "    cart.add(\"tea\", 25.00)\n    assert checkout(cart, \"SAVE10\") == \"Items: 2  Total charged: $48.60\"\n"))),
        reply(None, call("7", "run_command", command=test)),
        reply(None, call("8", "finish", fixed=True, summary=(
            "apply_discount divided the percentage by 10 instead of 100, so a 10% coupon removed the whole price. "
            "Fixed the divisor and added a SAVE10 regression test; bulk invoicing uses the same function and is now correct too."))),
        # poltergeist round 1
        reply("Trying edge cases: 50% coupons, 0%, and wholesale pricing.",
              call("9", "create_file", path="tests/test_poltergeist_edges.py", content=(
                  "from shop.invoice import bulk_price\nfrom shop.pricing import apply_discount\n\n\n"
                  "def test_half_off():\n    assert apply_discount(50, 50) == 25\n\n\n"
                  "def test_no_discount():\n    assert apply_discount(19.99, 0) == 19.99\n\n\n"
                  "def test_bulk_orders_get_five_percent_off():\n    assert bulk_price(2.00, 100) == 190.00\n"))),
        reply(None, call("10", "run_command", command=test)),
        reply(None, call("11", "finish", fixed=False,
                         summary="Tried 50%, 0% and bulk orders of 100 units: every edge case passes.")),
    ]


class PacedClient:
    """Plays the script back with a delay, so the dashboard updates like a real run."""

    def __init__(self, replies: list, delay: float):
        self.replies, self.delay = list(replies), delay
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.used = ["groq/qwen/qwen3.8-27b"]

    def create(self, **kwargs):
        time.sleep(self.delay)
        if not self.replies:
            return reply(None, call("end", "finish", fixed=False, summary="The demo script is over."))
        return self.replies.pop(0)


def make_repo(tmp: Path) -> Path:
    repo = tmp / "buggy-shop"
    shutil.copytree(ROOT / "examples" / "buggy-shop", repo, ignore=shutil.ignore_patterns(".ghostpatch", "__pycache__"))
    git = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, check=True)  # noqa: E731
    git("init", "-q", "-b", "main")
    git("config", "user.name", "Demo")
    git("config", "user.email", "demo@example.com")
    (repo / "shop" / "invoice.py").rename(repo / "shop" / "invoice.py.later")
    git("add", ".")
    git("commit", "-q", "-m", "Shop: cart, checkout and pricing")
    (repo / "shop" / "invoice.py.later").rename(repo / "shop" / "invoice.py")
    git("add", ".")
    git("commit", "-q", "-m", "Add wholesale bulk pricing")
    return repo


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--delay", type=float, default=1.2, help="Seconds per model reply.")
    args = parser.parse_args()
    ensure_python_on_path()
    tmp = Path(tempfile.mkdtemp(prefix="ghostpatch-demo-"))
    repo = make_repo(tmp)
    client = PacedClient(script(f'"{sys.executable}"'), args.delay)
    config = SimpleNamespace(provider=PROVIDERS["groq"], model="qwen/qwen3.8-27b", client=lambda: client)
    dashboard = Dashboard(repo, config, max_steps=30, approval="safe", use_graph=True, poltergeist=1)
    import ghostpatch.fallback as fallback

    fallback.make_client = lambda config, ui=None, fallback=True: client  # the demo never calls a real provider
    server = create_server(dashboard, args.port)
    print(f"GhostPatch demo at http://localhost:{args.port}  (repo copy: {repo})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
