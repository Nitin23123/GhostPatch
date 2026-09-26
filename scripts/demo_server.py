"""Run the dashboard against a scripted, offline "model" for UI work and screenshots.

    python scripts/demo_server.py [--port 8770] [--delay 1.2]

It copies examples/buggy-shop into a temporary git repository (so the real example is never
touched), then serves the dashboard there with a fake model. The fake model answers by role,
so every mode plays out realistically, in any order:

- Fix: reads the code, checks the blast radius, fixes pricing, writes a regression test.
  With the tournament on, a second candidate patches the symptom in the cart instead, and
  loses the cross-examination. The poltergeist tries edge cases and fails to break the fix.
- Haunt: finds the real bug in apply_discount (the skeptic confirms it); other functions are clean.
- Ask: explains how checkout computes the total, with the call flow.

No API key or network is needed.
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


COUPON_TEST = (
    "from shop.cart import Cart\nfrom shop.checkout import checkout\nfrom shop.invoice import bulk_price\n\n\n"
    "def test_save10_coupon():\n    cart = Cart()\n    cart.add(\"mug\", 12.50, quantity=2)\n"
    "    cart.add(\"tea\", 25.00)\n    assert checkout(cart, \"SAVE10\") == \"Items: 2  Total charged: $48.60\"\n\n\n"
    "def test_bulk_orders_get_five_percent_off():\n    assert bulk_price(2.00, 100) == 190.00\n"
)


def scripts(python: str) -> dict[str, list]:
    test = f"{python} -m pytest -q -p no:cacheprovider"
    return {
        "fix:direct": [
            reply("The coupon total is $0.00, so the discount is too large. Starting from pricing.",
                  call("1", "read_file", path="shop/pricing.py"), call("2", "read_file", path="shop/cart.py")),
            reply(None, call("3", "impact_of_change", name="apply_discount")),
            reply("`percent / 10` turns 10% into 100%. It should divide by 100.",
                  call("4", "edit_file", path="shop/pricing.py",
                       old_text="    return round(price - price * percent / 10, 2)",
                       new_text="    return round(price - price * percent / 100, 2)")),
            reply(None, call("5", "create_file", path="tests/test_coupons.py", content=COUPON_TEST)),
            reply(None, call("6", "run_command", command=test)),
            reply(None, call("7", "finish", fixed=True, summary=(
                "apply_discount divided the percentage by 10 instead of 100, so a 10% coupon removed the whole price. "
                "Fixed the divisor and added regression tests; bulk invoicing uses the same function and is now correct too."))),
        ],
        "fix:test-first": [  # a symptom patch: right for checkout, still wrong for bulk invoices
            reply("Reproducing first.", call("t1", "create_file", path="tests/test_checkout_coupon.py", content=(
                "from shop.cart import Cart\nfrom shop.checkout import checkout\n\n\n"
                "def test_save10():\n    cart = Cart()\n    cart.add(\"mug\", 12.50, quantity=2)\n"
                "    cart.add(\"tea\", 25.00)\n    assert checkout(cart, \"SAVE10\") == \"Items: 2  Total charged: $48.60\"\n"))),
            reply(None, call("t2", "run_command", command=test)),
            reply(None, call("t3", "read_file", path="shop/cart.py")),
            reply("The cart passes the raw percentage; scale it here.",
                  call("t4", "edit_file", path="shop/cart.py", old_text="apply_discount(self.subtotal(), coupon_percent)",
                       new_text="apply_discount(self.subtotal(), coupon_percent / 10)")),
            reply(None, call("t5", "run_command", command=test)),
            reply(None, call("t6", "finish", fixed=True, summary="Scaled the coupon percentage in Cart.total.")),
        ],
        "poltergeist": [
            reply("Trying edge cases: 50% coupons, 0%, and wholesale pricing.",
                  call("p1", "create_file", path="tests/test_poltergeist_edges.py", content=(
                      "from shop.invoice import bulk_price\nfrom shop.pricing import apply_discount\n\n\n"
                      "def test_half_off():\n    assert apply_discount(50, 50) == 25\n\n\n"
                      "def test_no_discount():\n    assert apply_discount(19.99, 0) == 19.99\n\n\n"
                      "def test_small_orders_pay_full_price():\n    assert bulk_price(10.00, 1) == 10.00\n"))),
            reply(None, call("p2", "run_command", command=test)),
            reply(None, call("p3", "finish", fixed=False,
                             summary="Tried 50%, 0% and small wholesale orders: every edge case passes.")),
        ],
        "haunt:apply_discount": [
            reply("A discount of `percent` percent should take percent/100 of the price.",
                  call("h1", "create_file", path="tests/test_haunt_apply_discount.py", content=(
                      "from shop.pricing import apply_discount\n\n\n"
                      "def test_ten_percent_off():\n    assert apply_discount(100, 10) == 90\n\n\n"
                      "def test_no_discount_keeps_the_price():\n    assert apply_discount(40, 0) == 40\n"))),
            reply(None, call("h2", "run_command", command=test)),
            reply(None, call("h3", "finish", fixed=True, summary=(
                "apply_discount takes 10x too much off.\napply_discount(100, 10) should be 90 (10% off) but returns 0.0, "
                "because it divides the percentage by 10 instead of 100."))),
        ],
        "haunt:other": [
            reply(None, call("c1", "create_file", path="tests/test_haunt_other.py", content=(
                "from shop.cart import Cart\n\n\ndef test_empty_cart_costs_nothing():\n    assert Cart().subtotal() == 0\n"))),
            reply(None, call("c2", "finish", fixed=False, summary="Checked the empty and single-item cases: all pass.")),
        ],
        "skeptic": [reply(json.dumps({"verdict": "real_bug", "reason": (
            "The name and every caller treat `percent` as a percentage, so 10 must mean 10% off, not 100%.")}))],
        "ask": [
            reply(None, call("a1", "find_callers", name="apply_discount")),
            reply(None, call("a2", "read_file", path="shop/checkout.py")),
            reply(None, call("a3", "finish", fixed=True, summary=(
                "`checkout` (shop/checkout.py:4) looks up the coupon and asks the cart for its total.\n\n"
                "- `Cart.total` (shop/cart.py:14) sums the items, then hands the subtotal and the coupon's percentage "
                "to `apply_discount` (shop/pricing.py:6).\n"
                "- `apply_discount` takes the percentage off, and `add_tax` adds tax on top.\n\n"
                "Wholesale orders reuse the same function: `bulk_price` (shop/invoice.py:7) calls `apply_discount` "
                "with a fixed 5%, so any change to the discount maths affects both."))),
        ],
    }


class RoleClient:
    """Plays a scripted reply for whoever is asking, with a delay, like a real (slowish) model."""

    def __init__(self, replies: dict[str, list], delay: float):
        self.replies, self.delay = replies, delay
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.used = ["groq/qwen/qwen3.8-27b"]

    @staticmethod
    def role(messages: list[dict]) -> str:
        system = messages[0]["content"] if messages else ""
        task = messages[1]["content"] if len(messages) > 1 else ""
        if "skeptical senior engineer" in system:
            return "skeptic"
        if "code guide" in system:
            return "ask"
        if "You are the Haunter" in system:
            return "haunt:apply_discount" if "Function to investigate: shop.pricing.apply_discount" in task else "haunt:other"
        if "You are the Poltergeist" in system:
            return "poltergeist"
        return "fix:test-first" if "write a small regression test that reproduces" in task else "fix:direct"

    def create(self, **kwargs):
        time.sleep(self.delay)
        messages = kwargs.get("messages", [])
        role = self.role(messages)
        # Each agent conversation starts fresh: replay its script from where that conversation is.
        done = sum(1 for m in messages if m.get("role") == "assistant")
        script = self.replies.get(role, [])
        if done < len(script):
            return script[done]
        return reply(None, call(f"end-{role}-{done}", "finish", fixed=False, summary="The demo script is over."))


def make_repo(tmp: Path) -> Path:
    repo = tmp / "buggy-shop"
    shutil.copytree(ROOT / "examples" / "buggy-shop", repo, ignore=shutil.ignore_patterns(".ghostpatch", "__pycache__"))
    git = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, check=True)  # noqa: E731
    git("init", "-q", "-b", "main")
    git("config", "user.name", "Demo")
    git("config", "user.email", "demo@example.com")
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
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
    client = RoleClient(scripts(f'"{sys.executable}"'), args.delay)
    config = SimpleNamespace(provider=PROVIDERS["groq"], model="qwen/qwen3.8-27b", client=lambda: client)
    dashboard = Dashboard(repo, config, max_steps=30, approval="safe", use_graph=True, poltergeist=1)
    import ghostpatch.cifix as cifix
    import ghostpatch.fallback as fallback

    fallback.make_client = lambda config, ui=None, fallback=True: client  # the demo never calls a real provider
    cifix.project_python = lambda repo: f'"{sys.executable}"'  # the repo copy has no virtualenv of its own
    server = create_server(dashboard, args.port)
    print(f"GhostPatch demo at http://localhost:{args.port}  (repo copy: {repo})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
