from pathlib import Path

import pytest

from ghostpatch.graph import CodeGraph, is_test_path, module_name
from ghostpatch.tools import Workspace

FILES = {
    "shop/pricing.py": (
        "def apply_discount(price, percent):\n"
        "    return price - price * percent / 100\n"
    ),
    "shop/cart.py": (
        "from shop.pricing import apply_discount\n"
        "\n"
        "class Cart:\n"
        "    def __init__(self):\n"
        "        self.items = []\n"
        "\n"
        "    def total(self, percent=0):\n"
        "        return apply_discount(sum(self.items), percent)\n"
    ),
    "shop/checkout.py": (
        "def checkout(cart):\n"
        "    return cart.total(10)\n"
    ),
    "tests/test_cart.py": (
        "from shop.cart import Cart\n"
        "\n"
        "def test_total():\n"
        "    assert Cart().total() == 0\n"
    ),
    "broken.py": "def oops(:\n",
}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    for rel, text in FILES.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    return tmp_path


@pytest.fixture
def graph(repo: Path):
    g = CodeGraph(repo, db_path=":memory:")
    g.refresh()
    yield g
    g.close()


def test_helpers():
    assert module_name("shop/cart.py") == "shop.cart"
    assert module_name("shop/__init__.py") == "shop"
    assert is_test_path("tests/test_cart.py") and is_test_path("pkg/foo_test.py")
    assert not is_test_path("shop/cart.py")


def test_indexes_symbols_and_reports_parse_errors(graph: CodeGraph):
    stats = graph.stats()
    assert stats["files"] == 5
    assert stats["parse_errors"] == 1
    assert "method shop.cart.Cart.total" in graph.find_symbol("total")
    assert "shop/cart.py:7-8" in graph.find_symbol("Cart.total")
    assert "No symbol" in graph.find_symbol("nope")


def test_callers_and_callees(graph: CodeGraph):
    callers = graph.find_callers("apply_discount")
    assert "shop/cart.py:8  in shop.cart.Cart.total" in callers
    callees = graph.find_callees("Cart.total")
    assert "apply_discount  (defined at shop/pricing.py:1)" in callees
    assert "sum  (external / builtin)" in callees


def test_related_tests_follow_the_call_chain(graph: CodeGraph):
    # test_total -> Cart.total -> apply_discount: the test is two hops away.
    assert "tests/test_cart.py  tests.test_cart.test_total" in graph.related_tests("apply_discount")


def test_impact_of_change(graph: CodeGraph):
    impact = graph.impact_of_change("apply_discount")
    assert "direct callers:" in impact and "shop.cart.Cart.total" in impact
    assert "shop.checkout.checkout" in impact  # two levels up
    assert "tests/test_cart.py::test_total" in impact


def test_refresh_is_incremental(repo: Path, graph: CodeGraph):
    assert graph.refresh().indexed == 0  # nothing changed

    (repo / "shop" / "pricing.py").write_text(
        "def apply_discount(price, percent):\n    return helper(price)\n\ndef helper(x):\n    return x\n",
        encoding="utf-8",
    )
    (repo / "broken.py").unlink()
    stats = graph.refresh()
    assert (stats.indexed, stats.removed) == (1, 1)
    assert "shop.pricing.apply_discount" in graph.find_callers("helper")


def test_repo_map_lists_files_and_signatures(graph: CodeGraph):
    repo_map = graph.repo_map()
    assert "shop/cart.py:\n  class Cart\n    def __init__(self)\n    def total(self, percent=0)" in repo_map
    assert "shop/pricing.py:\n  def apply_discount(price, percent)" in repo_map


def test_workspace_exposes_graph_tools(repo: Path, graph: CodeGraph):
    ws = Workspace(repo, approve_command=lambda cmd: True, graph=graph)
    assert "shop.cart.Cart.total" in ws.call("find_callers", {"symbol": "apply_discount"})

    no_graph = Workspace(repo, approve_command=lambda cmd: True)
    assert "not available" in no_graph.call("find_symbol", {"name": "Cart"})


def test_edits_report_their_impact_automatically(repo: Path, graph: CodeGraph):
    ws = Workspace(repo, approve_command=lambda cmd: True, graph=graph)
    out = ws.call("edit_file", {"path": "shop/pricing.py", "old_text": "/ 100", "new_text": "/ 100.0"})
    assert out.startswith("Edited shop/pricing.py.")
    assert "you changed shop.pricing.apply_discount" in out
    assert "tests/test_cart.py::test_total" in out


def test_cache_directory_ignores_itself(repo: Path):
    g = CodeGraph(repo)
    g.close()
    assert (repo / ".ghostpatch" / ".gitignore").read_text(encoding="utf-8") == "*\n"


# ------------------------------------------------------------ precise call resolution

PRECISE = {
    "billing/__init__.py": "",
    "billing/tax.py": "def total(amount):\n    return amount * 1.2\n",
    "billing/stats.py": "def total(values):\n    return sum(values)\n",
    "billing/invoice.py": (
        "from .tax import total\n"
        "import billing.stats as stats\n\n\n"
        "class Invoice:\n"
        "    def render(self):\n        return self.header() + str(total(10))\n\n"
        "    def header(self):\n        return 'INVOICE'\n\n"
        "    def summary(self, rows):\n        return stats.total(rows)\n\n\n"
        "class Report:\n"
        "    def header(self):\n        return 'REPORT'\n"
    ),
    "billing/view.py": "def show(doc):\n    return doc.header()\n",
    "web/pricing.ts": "export function applyDiscount(p: number) { return p * 0.9; }\nexport function total(x: number) { return x; }\n",
    "web/cart.ts": (
        "import { applyDiscount } from \"./pricing.ts\";\n"
        "export class Cart {\n  sum() { return this.subtotal(); }\n  subtotal() { return applyDiscount(10); }\n}\n"
    ),
}


@pytest.fixture
def precise(tmp_path: Path):
    for rel, text in PRECISE.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    g = CodeGraph(tmp_path, db_path=":memory:")
    g.refresh()
    yield g
    g.close()


def test_imports_decide_which_function_a_call_means(precise: CodeGraph):
    tax_callers = precise.find_callers("billing.tax.total")
    stats_callers = precise.find_callers("billing.stats.total")
    assert "Invoice.render" in tax_callers and "Invoice.summary" not in tax_callers        # from .tax import total
    assert "Invoice.summary" in stats_callers and "Invoice.render" not in stats_callers    # stats.total(...) via the alias
    assert "matched by name" not in tax_callers + stats_callers


def test_self_calls_stay_in_their_own_class(precise: CodeGraph):
    invoice_header = precise.find_callers("Invoice.header")
    report_header = precise.find_callers("Report.header")
    assert "billing.invoice.Invoice.render" in invoice_header
    assert "Invoice.render" not in report_header


def test_calls_on_unknown_objects_are_marked_as_name_matches(precise: CodeGraph):
    # doc.header() could be either class's method: both are linked, and flagged as a guess.
    assert "billing.view.show  (matched by name)" in precise.find_callers("Report.header")


def test_js_imports_and_this_are_resolved(precise: CodeGraph):
    assert "web.cart.Cart.subtotal" in precise.find_callers("web.pricing.applyDiscount")
    assert "web.cart.Cart.sum" in precise.find_callers("Cart.subtotal")
    assert "Nothing calls" in precise.find_callers("web.pricing.total")  # not billing's total() calls


def test_blast_radius_follows_real_links_only(precise: CodeGraph):
    radius = precise.blast_radius(["billing.tax.total"])
    assert "billing.invoice.Invoice.render" in radius and "billing.invoice.Invoice.summary" not in radius
