"""Ask the graph: a read-only agent, and the call flow drawn from the real code graph."""

from pathlib import Path
from types import SimpleNamespace

from ghostpatch.ask import ask, mentioned_flow, mermaid, render_flow
from ghostpatch.graph import CodeGraph
from test_agent import FakeClient, SilentUI, reply, tool_call

SHOP = {
    "shop/__init__.py": "",
    "shop/pricing.py": "def apply_discount(price, percent):\n    return price - price * percent / 100\n",
    "shop/cart.py": ("from shop.pricing import apply_discount\n\n\nclass Cart:\n"
                     "    def __init__(self):\n        self.items = []\n\n"
                     "    def total(self, percent=0):\n        return apply_discount(sum(self.items), percent)\n"),
    "shop/checkout.py": "def checkout(cart, code):\n    return cart.total(10 if code else 0)\n",
}


def make_repo(root: Path) -> Path:
    for rel, text in SHOP.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def config():
    return SimpleNamespace(provider=SimpleNamespace(name="fake"), model="m")


ANSWER = ("`checkout` (shop/checkout.py:1) asks the cart for its total. `Cart.total` sums the items and hands "
          "the sum to `apply_discount` (shop/pricing.py:1), which takes the percentage off.")


def test_ask_answers_with_the_real_call_flow(tmp_path: Path):
    repo = make_repo(tmp_path)
    client = FakeClient([
        reply(None, [tool_call("1", "find_callers", symbol="apply_discount")]),  # "symbol" is an alias of name
        reply(None, [tool_call("2", "finish", summary=ANSWER, fixed=True)]),
    ])
    graph = CodeGraph(repo)
    try:
        answer = ask(repo, config(), client, SilentUI(), "How is the total calculated?", graph=graph)
    finally:
        graph.close()

    assert answer.found and answer.text == ANSWER
    names = {n["qualname"] for n in answer.flow["nodes"]}
    assert names == {"shop.checkout.checkout", "shop.cart.Cart.total", "shop.pricing.apply_discount"}
    edges = {(e["source"], e["target"]) for e in answer.flow["edges"]}
    assert edges == {("shop.checkout.checkout", "shop.cart.Cart.total"),
                     ("shop.cart.Cart.total", "shop.pricing.apply_discount")}
    tree = render_flow(answer.flow).splitlines()
    assert tree[0].startswith("shop.checkout.checkout")
    assert tree[1].strip().startswith("└─ shop.cart.Cart.total") and "apply_discount" in tree[2]
    assert "flowchart LR" in mermaid(answer.flow)

    offered = {t["function"]["name"] for t in client.requests[0]["tools"]}
    assert "find_callers" in offered and not offered & {"edit_file", "create_file", "run_command", "replace_lines"}
    assert "Question:\nHow is the total calculated?" in client.requests[0]["messages"][1]["content"]


def test_ask_can_never_write(tmp_path: Path):
    repo = make_repo(tmp_path)
    client = FakeClient([  # a model that ignores the offered tools and tries to edit anyway
        reply(None, [tool_call("1", "edit_file", path="shop/pricing.py", old_text="/ 100", new_text="/ 10")]),
        reply(None, [tool_call("2", "finish", summary="Done.", fixed=True)]),
    ])
    ask(repo, config(), client, SilentUI(), "Change the discount?")
    assert "/ 100" in (repo / "shop" / "pricing.py").read_text(encoding="utf-8")
    refusal = [m for m in client.requests[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert "read-only" in refusal


def test_names_the_graph_doesnt_know_are_ignored(tmp_path: Path):
    graph = CodeGraph(make_repo(tmp_path))
    try:
        flow = mentioned_flow(graph, "It uses `sum` and `nonexistent_helper()` but also `apply_discount`.")
    finally:
        graph.close()
    assert [n["qualname"] for n in flow["nodes"]] == ["shop.pricing.apply_discount"]
    assert render_flow(flow) == "also mentioned: shop.pricing.apply_discount"
