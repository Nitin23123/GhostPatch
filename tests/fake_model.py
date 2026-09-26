"""A fake OpenAI-compatible model server, for end-to-end tests of the real CLI and the GitHub Action.

    python tests/fake_model.py [--port 8977]

It speaks the chat-completions API over HTTP (so the real `openai` client parses its replies)
and plays scripted replies for the examples/buggy-shop project, by role: the fixer (direct or
test-first), the poltergeist, the haunter, the skeptic and ask mode's code guide. It is
stateless: each conversation's position in its script is the number of assistant messages so
far, so any number of runs, in any order, work. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PYTEST = f'"{sys.executable}" -m pytest -q -p no:cacheprovider'

COUPON_TEST = (
    "from shop.cart import Cart\nfrom shop.checkout import checkout\nfrom shop.invoice import bulk_price\n\n\n"
    "def test_save10_coupon():\n    cart = Cart()\n    cart.add('mug', 12.50, quantity=2)\n"
    "    cart.add('tea', 25.00)\n    assert checkout(cart, 'SAVE10') == 'Items: 2  Total charged: $48.60'\n\n\n"
    "def test_bulk_orders_get_five_percent_off():\n    assert bulk_price(2.00, 100) == 190.00\n"
)
FIX_PRICING = {"path": "shop/pricing.py", "old_text": "price * percent / 10, 2)", "new_text": "price * percent / 100, 2)"}


def call(tool: str, **args) -> tuple[str, dict]:
    return tool, args


SCRIPTS: dict[str, list[tuple[str | None, list[tuple[str, dict]]]]] = {
    "fix:direct": [
        ("The discount is 10x too big; starting from pricing.", [call("read_file", path="shop/pricing.py")]),
        (None, [call("impact_of_change", name="apply_discount")]),
        ("`percent / 10` should be `percent / 100`.", [call("edit_file", **FIX_PRICING)]),
        (None, [call("create_file", path="tests/test_coupons.py", content=COUPON_TEST)]),
        (None, [call("run_command", command=PYTEST)]),
        (None, [call("finish", fixed=True, summary="apply_discount divided by 10 instead of 100. Fixed it and added tests.")]),
    ],
    "fix:test-first": [  # a symptom patch in the cart: right for checkout, wrong for bulk invoices
        ("Reproducing first.", [call("create_file", path="tests/test_checkout_coupon.py", content=(
            "from shop.cart import Cart\nfrom shop.checkout import checkout\n\n\ndef test_save10():\n    cart = Cart()\n"
            "    cart.add('mug', 12.50, quantity=2)\n    cart.add('tea', 25.00)\n"
            "    assert checkout(cart, 'SAVE10') == 'Items: 2  Total charged: $48.60'\n"))]),
        (None, [call("edit_file", path="shop/cart.py", old_text="apply_discount(self.subtotal(), coupon_percent)",
                     new_text="apply_discount(self.subtotal(), coupon_percent / 10)")]),
        (None, [call("run_command", command=PYTEST)]),
        (None, [call("finish", fixed=True, summary="Scaled the coupon percentage in Cart.total.")]),
    ],
    "poltergeist": [
        ("Edge cases: 50%, 0%, small wholesale orders.", [call("create_file", path="tests/test_poltergeist_edges.py", content=(
            "from shop.invoice import bulk_price\nfrom shop.pricing import apply_discount\n\n\n"
            "def test_half_off():\n    assert apply_discount(50, 50) == 25\n\n\n"
            "def test_small_orders_pay_full_price():\n    assert bulk_price(10.00, 1) == 10.00\n"))]),
        (None, [call("run_command", command=PYTEST)]),
        (None, [call("finish", fixed=False, summary="Every edge case passes.")]),
    ],
    "haunt:apply_discount": [
        (None, [call("create_file", path="tests/test_haunt_apply_discount.py", content=(
            "from shop.pricing import apply_discount\n\n\ndef test_ten_percent_off():\n"
            "    assert apply_discount(100, 10) == 90\n"))]),
        (None, [call("finish", fixed=True, summary="apply_discount takes 10x too much off.\n"
                     "apply_discount(100, 10) should be 90 but returns 0.0.")]),
    ],
    "haunt:other": [
        (None, [call("create_file", path="tests/test_haunt_other.py", content=(
            "from shop.cart import Cart\n\n\ndef test_empty_cart_costs_nothing():\n    assert Cart().subtotal() == 0\n"))]),
        (None, [call("finish", fixed=False, summary="Checked the empty case: it passes.")]),
    ],
    "skeptic": [(json.dumps({"verdict": "real_bug", "reason": "Callers treat percent as a percentage."}), [])],
    "ask": [
        (None, [call("find_callers", name="apply_discount")]),
        (None, [call("finish", fixed=True, summary=(
            "`checkout` asks the cart for its total; `Cart.total` passes the subtotal to `apply_discount`, "
            "and `bulk_price` uses `apply_discount` too."))]),
    ],
}


def role(messages: list[dict]) -> str:
    system = messages[0].get("content") or "" if messages else ""
    task = messages[1].get("content") or "" if len(messages) > 1 else ""
    if "skeptical senior engineer" in system:
        return "skeptic"
    if "code guide" in system:
        return "ask"
    if "You are the Haunter" in system:
        return "haunt:apply_discount" if "Function to investigate: shop.pricing.apply_discount" in task else "haunt:other"
    if "You are the Poltergeist" in system:
        return "poltergeist"
    return "fix:test-first" if "regression test that reproduces" in task else "fix:direct"


def completion(body: dict) -> dict:
    messages = body.get("messages", [])
    name = role(messages)
    turn = sum(1 for m in messages if m.get("role") == "assistant")
    script = SCRIPTS[name]
    content, calls = script[turn] if turn < len(script) else (None, [call("finish", fixed=False, summary="Script over.")])
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [{"id": f"{name}-{turn}-{i}", "type": "function",
                                  "function": {"name": tool, "arguments": json.dumps(args)}}
                                 for i, (tool, args) in enumerate(calls)]
    return {"id": f"chatcmpl-{name}-{turn}", "object": "chat.completion", "created": 0, "model": body.get("model", "fake"),
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # quiet
        pass

    def _json(self, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._json({"object": "list", "data": [{"id": "fake", "object": "model", "created": 0, "owned_by": "tests"}]})
        self.send_error(404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path.rstrip("/").endswith("/chat/completions"):
            return self._json(completion(body))
        self.send_error(404)


def start(port: int = 0) -> ThreadingHTTPServer:
    """Serve in a background thread; returns the server (its port is server.server_address[1])."""
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8977)
    server = ThreadingHTTPServer(("127.0.0.1", parser.parse_args().port), Handler)
    print(f"fake model at http://127.0.0.1:{server.server_address[1]}/v1", flush=True)
    server.serve_forever()
