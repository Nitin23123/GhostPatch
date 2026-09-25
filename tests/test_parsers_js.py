"""JavaScript / TypeScript support in the code graph (parsed with tree-sitter)."""

from pathlib import Path

import pytest

from ghostpatch.graph import CodeGraph
from ghostpatch.parsers import extract, is_test_path, language_of, module_name
from ghostpatch.tools import Workspace

CART_TS = """\
import { applyDiscount, addTax } from "./pricing.ts";

export interface Line { price: number; quantity: number }

export class Cart extends Base {
  lines: Line[] = [];

  add(price: number, quantity = 1): void {
    this.lines.push({ price, quantity });
  }

  subtotal(): number {
    return this.lines.reduce((sum, line) => sum + line.price, 0);
  }

  total = (coupon: number): number => addTax(applyDiscount(this.subtotal(), coupon));
}
"""

PRICING_TS = """\
export function applyDiscount(price: number, percent: number): number {
  return price - price * percent / 100;
}
export const addTax = (price: number) => Math.round(price * 1.08 * 100) / 100;
"""

CHECKOUT_JS = """\
const { Cart } = require("./cart");
function checkout(cart, coupon) {
  return `Total: ${cart.total(coupon)}`;
}
const quickCheckout = () => checkout(new Cart(), 0);
module.exports = { checkout, quickCheckout };
"""

TEST_TS = """\
import { test, describe } from "node:test";
import assert from "node:assert";
import { Cart } from "../src/cart.ts";

function makeCart() { const c = new Cart(); c.add(10, 2); return c; }

describe("Cart", () => {
  test("subtotal counts quantity", () => {
    assert.equal(makeCart().subtotal(), 20);
  });
  it("adds tax to the total", () => assert.equal(makeCart().total(0), 21.6));
});
"""


def symbols(source: str, path: str):
    return {s.qualname: s for s in extract(source, path).symbols}


def test_languages_and_module_names():
    assert language_of("src/app.tsx") == "tsx"
    assert language_of("lib/util.mjs") == "javascript"
    assert language_of("vendor/jquery.min.js") is None
    assert language_of("types/index.d.ts") is None
    assert module_name("src/cart.ts") == "src.cart"
    assert module_name("src/index.ts") == "src"
    assert is_test_path("src/cart.test.ts") and is_test_path("src/__tests__/cart.js")
    assert is_test_path("web/cart.spec.tsx")
    assert not is_test_path("src/cart.ts")


def test_typescript_classes_methods_and_functions():
    syms = symbols(CART_TS, "src/cart.ts")
    assert syms["src.cart.Cart"].kind == "class"
    assert syms["src.cart.Cart"].signature == "class Cart extends Base"
    assert syms["src.cart.Cart.add"].kind == "method"
    assert syms["src.cart.Cart.add"].signature == "add(price: number, quantity = 1): void"
    assert syms["src.cart.Cart.total"].kind == "method"  # class-field arrow function
    assert (syms["src.cart.Cart.subtotal"].line, syms["src.cart.Cart.subtotal"].end_line) == (12, 14)

    pricing = symbols(PRICING_TS, "src/pricing.ts")
    assert pricing["src.pricing.applyDiscount"].signature == "function applyDiscount(price: number, percent: number): number"
    assert pricing["src.pricing.addTax"].signature == "const addTax = (price: number) =>"


def test_calls_are_attributed_to_the_enclosing_function():
    facts = extract(CART_TS, "src/cart.ts")
    by_caller = {}
    for index, callee, _ in facts.calls:
        caller = facts.symbols[index].name if index is not None else None
        by_caller.setdefault(caller, set()).add(callee)
    assert by_caller["total"] == {"addTax", "applyDiscount", "subtotal"}
    assert by_caller["subtotal"] == {"reduce"}
    assert ("./pricing.ts", "applyDiscount", 1) in facts.imports


def test_javascript_commonjs_and_new():
    syms = symbols(CHECKOUT_JS, "src/checkout.js")
    assert {"src.checkout.checkout", "src.checkout.quickCheckout"} <= set(syms)
    calls = {callee for _, callee, _ in extract(CHECKOUT_JS, "src/checkout.js").calls}
    assert {"require", "total", "checkout", "Cart"} <= calls  # `new Cart()` counts as a call to Cart


def test_test_blocks_become_named_symbols():
    syms = symbols(TEST_TS, "tests/cart.test.ts")
    assert syms["tests.cart.test.Cart"].kind == "suite"
    test = syms["tests.cart.test.Cart.subtotal counts quantity"]
    assert test.kind == "test" and test.signature == 'test("subtotal counts quantity")'
    assert syms["tests.cart.test.Cart.adds tax to the total"].kind == "test"  # it(...) with an expression body


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    files = {
        "src/cart.ts": CART_TS, "src/pricing.ts": PRICING_TS, "src/checkout.js": CHECKOUT_JS,
        "tests/cart.test.ts": TEST_TS,
        "node_modules/lib/index.js": "function ignored() {}",
        "dist/app.min.js": "function ignored(){}",
    }
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    return tmp_path


@pytest.fixture
def graph(repo: Path):
    g = CodeGraph(repo, db_path=":memory:")
    g.refresh()
    yield g
    g.close()


def test_graph_indexes_js_and_ts_but_skips_vendored_code(graph: CodeGraph):
    assert graph.stats()["files"] == 4
    assert "No symbol" in graph.find_symbol("ignored")


def test_impact_crosses_files_and_finds_tests_by_title(graph: CodeGraph):
    impact = graph.impact_of_change("subtotal")
    assert "src/cart.ts:16  in src.cart.Cart.total" in impact
    assert "src/checkout.js:3  in src.checkout.checkout" in impact
    assert "tests/cart.test.ts::subtotal counts quantity" in impact
    assert "tests/cart.test.ts::adds tax to the total" in impact
    tests = graph.related_tests("applyDiscount")
    assert "tests.cart.test.Cart.adds tax to the total" in tests


def test_repo_map_includes_typescript_signatures(graph: CodeGraph):
    repo_map = graph.repo_map()
    assert "src/cart.ts:\n  class Cart extends Base\n    add(price: number, quantity = 1): void" in repo_map
    assert '  describe("Cart")\n    test("subtotal counts quantity")' in repo_map


def test_edits_to_typescript_report_impact(repo: Path, graph: CodeGraph):
    ws = Workspace(repo, approve_command=lambda cmd: True, graph=graph)
    out = ws.call("edit_file", {"path": "src/cart.ts", "old_text": "sum + line.price", "new_text": "sum + line.price * line.quantity"})
    assert "you changed src.cart.Cart.subtotal" in out
    assert "tests/cart.test.ts::subtotal counts quantity" in out


def test_editing_a_test_just_asks_to_run_it(repo: Path, graph: CodeGraph):
    ws = Workspace(repo, approve_command=lambda cmd: True, graph=graph)
    out = ws.call("edit_file", {"path": "tests/cart.test.ts", "old_text": "subtotal(), 20", "new_text": "subtotal(), 20.0"})
    assert "you changed the test tests.cart.test.Cart.subtotal counts quantity. Run it" in out
    assert "Nothing in the indexed code calls" not in out
