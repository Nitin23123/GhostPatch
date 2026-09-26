"""Go, Rust and Java: parsing, the code graph, test runners, stack traces and real fixing sessions.

The first part needs no toolchain. The sessions at the end run the real `go test`, `cargo test`
and Maven; they are skipped when those aren't installed, unless GHOSTPATCH_REQUIRE_TOOLCHAINS is
set (as in CI), in which case a missing toolchain fails the test instead.
"""

import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from ghostpatch import cifix, trace
from ghostpatch.graph import CodeGraph
from ghostpatch.parsers import extract, is_test_path, syntax_error
from ghostpatch.parsers_typed import rust_module_path, rust_split
from ghostpatch.policy import is_safe_command, is_test_command
from ghostpatch.proof import only_tests_changed, prove_fix, rust_tests_changed
from ghostpatch.regression import failing_tests
from ghostpatch.session import run_session
from test_agent import FakeClient, SilentUI, reply, tool_call


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))  # exact bytes: no CRLF translation on Windows
    return root


def edges(graph: CodeGraph) -> set[tuple[str, str, int]]:
    """(caller qualname, callee qualname, exact) for every link in the graph."""
    graph.refresh()
    return set(graph.db.execute(
        "SELECT (SELECT qualname FROM symbols WHERE id = e.caller_id), "
        "(SELECT qualname FROM symbols WHERE id = e.callee_id), e.exact FROM edges e"))


def symbols(facts) -> dict[str, str]:
    return {s.qualname: s.kind for s in facts.symbols}


# ---------------------------------------------------------------------------- Go

GO_SHOP = {
    "go.mod": "module example.com/shop\n\ngo 1.22\n",
    "cart/cart.go": 'package cart\n\nimport (\n\t"fmt"\n\tp "example.com/shop/pricing"\n)\n\n'
                    "type Cart struct{ Items []int }\n\n"
                    "func (c *Cart) Total() int {\n\tfmt.Println(len(c.Items))\n\treturn p.Discount(c.subtotal())\n}\n",
    "cart/sub.go": "package cart\n\nfunc (c *Cart) subtotal() int {\n\treturn sum(c.Items)\n}\n\n"
                   "func sum(xs []int) int {\n\tt := 0\n\tfor _, x := range xs {\n\t\tt += x\n\t}\n\treturn t\n}\n",
    "cart/cart_test.go": 'package cart\n\nimport "testing"\n\nfunc TestTotal(t *testing.T) {\n'
                         "\tc := &Cart{Items: []int{1, 2}}\n\tif c.Total() != 3 {\n\t\tt.Fatal(c.Total())\n\t}\n}\n",
    "pricing/pricing.go": "package pricing\n\n// Discount takes nothing off yet.\nfunc Discount(x int) int { return x }\n",
}


def test_go_symbols_types_and_tests():
    facts = extract(GO_SHOP["cart/cart.go"], "cart/cart.go")
    assert symbols(facts) == {"cart.Cart": "class", "cart.Cart.Total": "method"}
    assert facts.symbols[1].parent == 0 and facts.symbols[1].signature == "func (c *Cart) Total() int"
    assert ("Discount", 12, "p") in [(c, line, r) for _, c, line, r in facts.calls]
    assert ("subtotal", ":Cart") in [(c, r) for _, c, _, r in facts.calls]  # the receiver `c` is a *Cart
    assert ("example.com/shop/pricing", "*", 5, "p") in facts.imports
    # a method whose type lives in another file of the package is still named after it
    assert symbols(extract(GO_SHOP["cart/sub.go"], "cart/sub.go")) == {"cart.Cart.subtotal": "method", "cart.sum": "function"}
    assert symbols(extract(GO_SHOP["cart/cart_test.go"], "cart/cart_test.go")) == {"cart.TestTotal": "test"}


def test_go_calls_link_through_packages_and_go_mod(tmp_path: Path):
    graph = CodeGraph(write(tmp_path, GO_SHOP), db_path=":memory:")
    assert edges(graph) == {
        ("cart.Cart.Total", "pricing.Discount", 1),     # p.Discount(): import alias -> go.mod -> pricing/
        ("cart.Cart.Total", "cart.Cart.subtotal", 1),   # a method declared in another file
        ("cart.Cart.subtotal", "cart.sum", 1),          # same package, another file
        ("cart.TestTotal", "cart.Cart.Total", 1),
    }  # and nothing for fmt.Println: it isn't in the repository
    assert "cart/cart_test.go  cart.TestTotal" in graph.related_tests("Discount")
    assert graph.untested() == []


# -------------------------------------------------------------------------- Rust

RUST_SHOP = {
    "Cargo.toml": '[package]\nname = "shop-core"\nversion = "0.1.0"\nedition = "2021"\n',
    "src/lib.rs": "pub mod cart;\npub mod pricing;\n",
    "src/pricing.rs": "pub fn discount(x: u32) -> u32 {\n    x\n}\n",
    "src/cart.rs": "use crate::pricing::discount;\n\npub struct Cart {\n    pub items: Vec<u32>,\n}\n\n"
                   "impl Cart {\n    pub fn new() -> Self {\n        Cart { items: vec![] }\n    }\n\n"
                   "    pub fn total(&self) -> u32 {\n        discount(self.subtotal())\n    }\n}\n\n"
                   "impl Cart {\n    fn subtotal(&self) -> u32 {\n        self.items.iter().sum()\n    }\n}\n\n"
                   "pub fn describe(cart: &Cart) -> String {\n    format!(\"{} total\", cart.total())\n}\n\n"
                   "#[cfg(test)]\nmod tests {\n    use super::*;\n\n    // an empty cart\n    #[test]\n"
                   "    fn empty() {\n        let cart = Cart::new();\n        assert_eq!(cart.total(), 0);\n    }\n}\n",
    "tests/it.rs": "use shop_core::cart::Cart;\nuse shop_core::pricing;\n\n#[test]\n"
                   "fn integration() {\n    assert_eq!(Cart::new().total(), pricing::discount(0));\n}\n",
}


def test_rust_symbols_impls_tests_and_macros():
    facts = extract(RUST_SHOP["src/cart.rs"], "src/cart.rs")
    kinds = [(s.qualname, s.kind, s.signature) for s in facts.symbols]
    assert ("cart.Cart", "class", "struct Cart") in kinds and ("cart.Cart", "class", "impl Cart") in kinds
    assert ("cart.Cart.total", "method", "fn total(&self) -> u32") in kinds
    assert ("cart.tests", "suite", "mod tests") in kinds and ("cart.tests.empty", "test", "fn empty()") in kinds
    calls = {(c, r) for _, c, _, r in facts.calls}
    assert {("subtotal", "self"), ("total", ":Cart"), ("new", ":Cart")} <= calls  # incl. inside format!/assert_eq!
    assert ("crate::pricing", "discount", 1, "discount") in facts.imports
    assert ("crate::cart", "*", 29, "*") in facts.imports  # `use super::*` inside the tests module
    assert rust_module_path("src/shop/cart.rs") == ["shop", "cart"] and rust_module_path("src/shop/mod.rs") == ["shop"]
    assert rust_module_path("src/lib.rs") == [] and rust_module_path("tests/it.rs") == []


def test_rust_calls_link_through_modules_impls_and_the_crate_name(tmp_path: Path):
    graph = CodeGraph(write(tmp_path, RUST_SHOP), db_path=":memory:")
    found = edges(graph)
    assert {
        ("cart.Cart.new", "cart.Cart", 1),              # the struct, not the impl blocks
        ("cart.Cart.total", "pricing.discount", 1),     # use crate::pricing::discount
        ("cart.Cart.total", "cart.Cart.subtotal", 1),   # self.subtotal(), in another impl block
        ("cart.describe", "cart.Cart.total", 1),        # cart: &Cart, called inside format!()
        ("cart.tests.empty", "cart.Cart.new", 1),
        ("cart.tests.empty", "cart.Cart.total", 1),     # inside assert_eq!()
        ("tests.it.integration", "pricing.discount", 1),  # shop_core::pricing, through the crate's name
    } <= found
    assert all(exact for *_, exact in found)
    tests = graph.related_tests("Cart::total")
    assert "src/cart.rs  cart.tests.empty" in tests and "tests/it.rs  tests.it.integration" in tests
    assert "tests to run" in graph.impact_of_change("discount") and "src/cart.rs:13" in graph.impact_of_change("discount")
    assert [u["qualname"] for u in graph.untested()] == ["cart.describe"]  # the in-file tests count as tests


def test_rust_test_modules_can_be_split_from_the_code():
    code, tests = rust_split(RUST_SHOP["src/cart.rs"])
    assert "mod tests" in tests and "#[cfg(test)]" in tests and "mod tests" not in code and "fn total" in code
    before = RUST_SHOP["src/cart.rs"]
    more_tests = before.replace("    fn empty()", "    fn two() { assert_eq!(1, 1); }\n\n    #[test]\n    fn empty()")
    assert rust_tests_changed("src/cart.rs", before, more_tests) and only_tests_changed("src/cart.rs", before, more_tests)
    code_change = before.replace("self.items.iter().sum()", "self.items.iter().sum::<u32>() + 1")
    assert not only_tests_changed("src/cart.rs", before, code_change) and not rust_tests_changed("src/cart.rs", before, code_change)


# -------------------------------------------------------------------------- Java

JAVA_SHOP = {
    "src/main/java/com/shop/Cart.java":
        "package com.shop;\n\nimport com.shop.pricing.Pricing;\n\npublic class Cart {\n"
        "    private final Pricing pricing = new Pricing();\n\n"
        "    public int total() { return pricing.discount(subtotal()); }\n\n"
        "    int subtotal() { return Math.max(3, 0); }\n}\n",
    "src/main/java/com/shop/pricing/Pricing.java":
        "package com.shop.pricing;\n\npublic class Pricing {\n    public int discount(int x) { return x; }\n}\n",
    "src/test/java/com/shop/CartTest.java":
        "package com.shop;\n\nimport org.junit.jupiter.api.Test;\n"
        "import static org.junit.jupiter.api.Assertions.assertEquals;\n\nclass CartTest {\n    @Test\n"
        "    void total() {\n        Cart cart = new Cart();\n        assertEquals(3, cart.total());\n    }\n}\n",
}


def test_java_symbols_tests_and_typed_calls():
    facts = extract(JAVA_SHOP["src/main/java/com/shop/Cart.java"], "src/main/java/com/shop/Cart.java")
    assert symbols(facts) == {"com.shop.Cart": "class", "com.shop.Cart.total": "method", "com.shop.Cart.subtotal": "method"}
    calls = {(c, r) for _, c, _, r in facts.calls}
    assert {("discount", ":Pricing"), ("subtotal", None), ("max", ":Math"), ("Pricing", ":Pricing")} <= calls
    test = extract(JAVA_SHOP["src/test/java/com/shop/CartTest.java"], "src/test/java/com/shop/CartTest.java")
    assert symbols(test)["com.shop.CartTest.total"] == "test"


def test_java_calls_link_through_packages_imports_and_fields(tmp_path: Path):
    graph = CodeGraph(write(tmp_path, JAVA_SHOP), db_path=":memory:")
    assert edges(graph) == {
        ("com.shop.Cart", "com.shop.pricing.Pricing", 1),              # the field's `new Pricing()`
        ("com.shop.Cart.total", "com.shop.pricing.Pricing.discount", 1),  # a field of type Pricing
        ("com.shop.Cart.total", "com.shop.Cart.subtotal", 1),          # a bare call: this class's method
        ("com.shop.CartTest.total", "com.shop.Cart", 1),               # same package, no import needed
        ("com.shop.CartTest.total", "com.shop.Cart.total", 1),         # Cart cart = ...; cart.total()
    }  # and nothing for Math.max or assertEquals
    assert "com.shop.CartTest.total" in graph.related_tests("Pricing.discount")


def test_test_files_and_syntax_errors_in_every_language():
    assert is_test_path("cart/cart_test.go") and not is_test_path("cart/cart.go")
    assert is_test_path("src/test/java/com/shop/CartTest.java") and is_test_path("core/CartTests.java")
    assert not is_test_path("src/main/java/com/shop/Cart.java") and not is_test_path("src/main/java/Testing.java")
    assert is_test_path("tests/it.rs") and not is_test_path("src/cart.rs")
    assert syntax_error("package x\nfunc f( {\n", "x.go") and syntax_error("fn f() { let x = ; }", "a.rs")
    assert syntax_error("class A { void f() { int x = } }", "A.java")
    assert syntax_error(GO_SHOP["cart/cart.go"], "cart/cart.go") is None


# ------------------------------------------------------------ test runners and output

GO_OUTPUT = ("--- FAIL: TestTotal (0.00s)\n    cart_test.go:7: bad\n--- FAIL: TestSum (0.00s)\n"
             "    --- FAIL: TestSum/empty (0.00s)\n        cart_test.go:12: sum(nil) = 0, want 1\nFAIL\n"
             "FAIL\texample.com/shop/cart\t1.031s\n?   \texample.com/shop/pricing\t[no test files]\nFAIL\n")
CARGO_OUTPUT = ("running 1 test\ntest cart::tests::totals ... FAILED\n\nfailures:\n    cart::tests::totals\n\n"
                "test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s\n\n"
                "running 1 test\ntest integration ... ok\n\n"
                "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s\n")
MAVEN_OUTPUT = (
    "[INFO] Running com.shop.CartTest\n"
    "[ERROR] Tests run: 1, Failures: 1, Errors: 0, Skipped: 0, Time elapsed: 0.05 s <<< FAILURE! -- in com.shop.CartTest\n"
    "[ERROR] com.shop.CartTest.total -- Time elapsed: 0.032 s <<< FAILURE!\n"
    "org.opentest4j.AssertionFailedError: expected: <4> but was: <3>\n\tat com.shop.CartTest.total(CartTest.java:8)\n\n"
    "[ERROR] com.shop.PricingTest.boom -- Time elapsed: 0.006 s <<< ERROR!\n\n[INFO] Results:\n[INFO] \n"
    "[ERROR] Failures: \n[ERROR]   CartTest.total:8 expected: <4> but was: <3>\n[ERROR] Errors: \n"
    "[ERROR]   PricingTest.boom:10 NullPointer Cannot invoke \"Object.toString()\"\n[INFO] \n"
    "[ERROR] Tests run: 3, Failures: 1, Errors: 1, Skipped: 0\n[INFO] \n[INFO] BUILD FAILURE\n")
GRADLE_OUTPUT = ("> Task :test FAILED\n\nCartTest > total() FAILED\n    org.opentest4j.AssertionFailedError at CartTest.java:8\n\n"
                 "3 tests completed, 1 failed\n\nFAILURE: Build failed with an exception.\n\nBUILD FAILED in 2s\n")


def test_failing_tests_headlines_and_empty_runs_for_every_runner():
    assert failing_tests(GO_OUTPUT) == {"TestTotal", "TestSum", "TestSum/empty"}
    assert failing_tests(CARGO_OUTPUT) == {"cart::tests::totals"}
    assert failing_tests(MAVEN_OUTPUT) == {"CartTest.total", "PricingTest.boom"}  # listed twice, named once
    assert failing_tests(GRADLE_OUTPUT) == {"CartTest.total"}
    assert failing_tests("[ERROR] total(com.shop.CartTest)  Time elapsed: 0.01 s  <<< FAILURE!\n") == {"CartTest.total"}

    assert cifix.headline(GO_OUTPUT) == "0 packages ok, 1 failed (3 failing tests)"
    assert cifix.headline("ok  \texample.com/shop/cart\t0.2s\nok  \texample.com/shop/pricing\t0.1s\n") == "2 packages ok"
    assert cifix.headline(CARGO_OUTPUT) == "1 passed, 1 failed"
    assert cifix.headline(MAVEN_OUTPUT) == "1 passed, 2 failed"
    assert cifix.headline(GRADLE_OUTPUT) == "2 passed, 1 failed"
    assert cifix.headline("error[E0425]: cannot find value\nerror: could not compile `shop` due to 1 previous error") \
        == "the code does not compile"

    assert cifix.no_tests_ran("?   \texample.com/shop/pricing\t[no test files]\n")
    assert cifix.no_tests_ran("ok  \texample.com/shop/cart\t0.1s [no tests to run]\n")
    assert not cifix.no_tests_ran(GO_OUTPUT) and not cifix.no_tests_ran(CARGO_OUTPUT)
    assert cifix.no_tests_ran("running 0 tests\n\ntest result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 1 filtered out;")
    assert cifix.no_tests_ran("[INFO] BUILD SUCCESS\n") and not cifix.no_tests_ran(MAVEN_OUTPUT)
    assert cifix.no_tests_ran("No tests found for given includes: [CartTest](--tests filter)")
    # built, but the OS refused to start the test binary: the tests never ran (not a failure of the code)
    assert cifix.runner_missing("error: test failed, to rerun pass `--lib`\n\nCaused by:\n  could not execute process "
                                "`target\\debug\\deps\\shop.exe` (never executed)\n\nCaused by:\n  An Application Control "
                                "policy has blocked this file. (os error 4551)\n")


def test_test_commands_for_every_build_tool(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cifix.sys, "platform", "linux")
    go = write(tmp_path / "go", GO_SHOP)
    assert cifix.detect_test_command(go) == "go test ./..."
    assert cifix.test_files_command(go, ["cart/cart_test.go", "pricing/x_test.go"]) == ["go test ./cart ./pricing"]
    nested = write(tmp_path / "nested", {"svc/go.mod": "module example.com/svc\n", "svc/api/api_test.go": "package api\n"})
    assert cifix.test_files_command(nested, ["svc/api/api_test.go"]) == ["go -C svc test ./api"]

    rust = write(tmp_path / "rust", RUST_SHOP)
    assert cifix.detect_test_command(rust) == "cargo test --no-fail-fast"
    assert cifix.test_files_command(rust, ["tests/it.rs", "src/cart.rs"]) == [
        "cargo test --no-fail-fast --test it", "cargo test --no-fail-fast -- cart::"]

    maven = write(tmp_path / "maven", {**JAVA_SHOP, "pom.xml": "<project/>"})
    assert cifix.detect_test_command(maven) == "mvn -B -fae test"
    assert cifix.test_files_command(maven, ["src/test/java/com/shop/CartTest.java"]) == [
        "mvn -B test -Dtest=CartTest -Dsurefire.failIfNoSpecifiedTests=false"]
    (maven / "mvnw").write_text("#!/bin/sh\n", encoding="utf-8")
    assert cifix.detect_test_command(maven) == "./mvnw -B -fae test"

    gradle = write(tmp_path / "gradle", {**JAVA_SHOP, "build.gradle.kts": "plugins { java }\n"})
    assert cifix.detect_test_command(gradle) == "gradle test --continue"
    assert cifix.test_files_command(gradle, ["src/test/java/com/shop/CartTest.java"]) == ["gradle test --tests CartTest"]


def test_build_tools_run_without_asking_but_nothing_else_does():
    for command in ("go test ./...", "go -C svc test ./api", "go vet ./...", "cargo test --no-fail-fast -- cart::",
                    "cargo check", "mvn -B -fae test", "mvn -B test -Dtest=CartTest,OrderTest", "./mvnw -B test",
                    "mvnw.cmd -B test", "gradle test --continue", "./gradlew test --tests CartTest", "gradlew.bat :core:test"):
        assert is_safe_command(command), command
    for command in ("mvn deploy", "mvn -B install", "gradle publish", "go run .", "cargo run", "cargo test; rm -rf x",
                    "mvn test | tee log"):
        assert not is_safe_command(command), command
    assert is_test_command("mvn -B -fae test") and is_test_command("./gradlew test") and is_test_command("go -C a test ./b")


# ---------------------------------------------------------------------- stack traces

def test_go_rust_and_java_crashes_map_onto_the_graph(tmp_path: Path):
    go = trace.parse("panic: runtime error: index out of range [5] with length 2\n\ngoroutine 1 [running]:\n"
                     "example.com/shop/cart.(*Cart).Total(...)\n\t/home/me/shop/cart/cart.go:12 +0x1d\n"
                     "main.main()\n\t/home/me/shop/main.go:6 +0xa\nexit status 2\n")
    assert go.language == "go" and go.error.startswith("panic: runtime error")
    assert [(f.path, f.line) for f in go.frames] == [("/home/me/shop/main.go", 6), ("/home/me/shop/cart/cart.go", 12)]

    rust = trace.parse("thread 'main' panicked at src/cart.rs:13:9:\nattempt to subtract with overflow\n"
                       "stack backtrace:\n   0: rust_begin_unwind\n             at /rustc/abc/library/std/src/panicking.rs:665:5\n"
                       "   3: shop_core::cart::Cart::total\n             at ./src/cart.rs:13:9\n"
                       "   4: shop_core::describe\n             at ./src/cart.rs:24:30\n")
    assert rust.language == "rust" and rust.error == "panicked: attempt to subtract with overflow"
    graph = CodeGraph(write(tmp_path / "rust", RUST_SHOP), db_path=":memory:")
    trace.locate(rust, tmp_path / "rust", graph)
    assert rust.path_qualnames() == ["cart.describe", "cart.Cart.total"]
    assert trace.parse("thread 'main' panicked at src\\cart.rs:13:9:\noverflow\n").frames[0].line == 13  # Windows

    java = trace.parse('Exception in thread "main" java.lang.IllegalStateException: empty cart\n'
                       "\tat com.shop.pricing.Pricing.discount(Pricing.java:4)\n\tat com.shop.Cart.total(Cart.java:8)\n"
                       "\tat com.shop.Main.main(Main.java:5)\n")
    assert java.language == "java" and java.error == "java.lang.IllegalStateException: empty cart"
    graph = CodeGraph(write(tmp_path / "java", JAVA_SHOP), db_path=":memory:")
    trace.locate(java, tmp_path / "java", graph)
    assert java.path_qualnames() == ["com.shop.Cart.total", "com.shop.pricing.Pricing.discount"]
    assert java.repo_frames[-1].rel_path == "src/main/java/com/shop/pricing/Pricing.java"


# ------------------------------------------------------- real toolchains: whole sessions

def need(*tools: str) -> None:
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        if os.environ.get("GHOSTPATCH_REQUIRE_TOOLCHAINS"):
            pytest.fail(f"{', '.join(missing)} not installed, but GHOSTPATCH_REQUIRE_TOOLCHAINS is set")
        pytest.skip(f"{', '.join(missing)} not installed")


def session(repo: Path, replies: list, issue: str, **kwargs):
    config = SimpleNamespace(provider=SimpleNamespace(name="fake"), model="m")
    graph = CodeGraph(repo)
    try:
        outcome = run_session(repo, config, FakeClient(replies), SilentUI(), issue, graph=graph,
                              approve_command=lambda command: True, save=False, **kwargs)
    finally:
        graph.close()
    outputs = [outcome.proof.red_output + outcome.proof.green_output if outcome.proof else "",
               outcome.regression.output if outcome.regression else ""]
    if any("Application Control policy" in text for text in outputs):  # a locked-down Windows machine, not a bug
        pytest.skip("this machine's Application Control policy blocked a freshly built test binary")
    return outcome


def fix_and_test(code_path: str, old: str, new: str, test_path: str, test: str, *, edit_test: bool = False) -> list:
    """The ghost's replies: fix the code, add a test (a new file, or an edit to a Rust test module), finish."""
    add = (tool_call("2", "edit_file", path=test_path, old_text=test[0], new_text=test[1]) if edit_test
           else tool_call("2", "create_file", path=test_path, content=test))
    return [
        reply(None, [tool_call("1", "edit_file", path=code_path, old_text=old, new_text=new)]),
        reply(None, [add]),
        reply(None, [tool_call("3", "finish", summary="Fixed the loop.", fixed=True)]),
    ]


GO_BUGGY = {
    "go.mod": "module example.com/shop\n\ngo 1.21\n",
    "cart/cart.go": "package cart\n\n// Total adds up every price.\nfunc Total(prices []int) int {\n\tsum := 0\n"
                    "\tfor i := 1; i < len(prices); i++ {\n\t\tsum += prices[i]\n\t}\n\treturn sum\n}\n",
    "cart/cart_test.go": 'package cart\n\nimport "testing"\n\nfunc TestEmpty(t *testing.T) {\n'
                         "\tif Total(nil) != 0 {\n\t\tt.Fatal(Total(nil))\n\t}\n}\n",
}
GO_TEST = ('package cart\n\nimport "testing"\n\nfunc TestTotalCountsTheFirstItem(t *testing.T) {\n'
           "\tif got := Total([]int{1, 2, 3}); got != 6 {\n\t\tt.Fatalf(\"got %d, want 6\", got)\n\t}\n}\n")


def test_a_go_fix_is_proven_and_guarded_with_go_test(tmp_path: Path):
    need("go")
    repo = write(tmp_path, GO_BUGGY)
    outcome = session(repo, fix_and_test("cart/cart.go", "i := 1", "i := 0", "cart/total_test.go", GO_TEST),
                      "Total() ignores the first price")
    assert outcome.fixed, outcome.error
    assert outcome.proof.status == "proven", outcome.proof.red_output + outcome.proof.green_output
    assert outcome.proof.commands == ["go test ./cart"] and "failed" in outcome.proof.red
    assert outcome.regression.status == "clean" and outcome.regression.passed_after


def test_a_go_tournament_cross_examines_tests_with_the_same_names(tmp_path: Path):
    """Both candidates name their test TestTotal. Renaming rival files would not help in Go (one package,
    one namespace); putting the rival's tests in place of the candidate's own does."""
    need("go")
    repo = write(tmp_path, GO_BUGGY)
    rigged = ('package cart\n\nimport "testing"\n\nfunc TestTotal(t *testing.T) {\n'
              "\tif Total([]int{1, 2, 3}) != 6 {\n\t\tt.Fatal()\n\t}\n}\n")
    thorough = rigged.replace("\tif Total([]int{1, 2, 3}) != 6 {\n\t\tt.Fatal()\n\t}\n",
                              "\tif Total([]int{1, 2, 3}) != 6 || Total([]int{5}) != 5 {\n\t\tt.Fatal()\n\t}\n")
    hard_coded = "\tif len(prices) == 3 {\n\t\treturn 6\n\t}\n\tfor i := 1;"
    replies = [*fix_and_test("cart/cart.go", "\tfor i := 1;", hard_coded, "cart/a_test.go", rigged),
               *fix_and_test("cart/cart.go", "i := 1", "i := 0", "cart/b_test.go", thorough)]
    outcome = session(repo, replies, "Total() ignores the first price", candidates=2)
    first, second = outcome.tournament.candidates
    assert (first.rivals_passed, first.rivals_total) == (0, 1) and (second.rivals_passed, second.rivals_total) == (1, 1)
    assert outcome.tournament.winner.number == 2
    assert not (repo / "cart" / "a_test.go").exists() and (repo / "cart" / "b_test.go").is_file()


RUST_BUGGY = {
    "Cargo.toml": '[package]\nname = "shop"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\n',
    "src/lib.rs": "/// Adds up every price.\npub fn total(prices: &[u32]) -> u32 {\n    prices.iter().skip(1).sum()\n}\n\n"
                  "#[cfg(test)]\nmod tests {\n    use super::*;\n\n    #[test]\n    fn empty() {\n"
                  "        assert_eq!(total(&[]), 0);\n    }\n}\n",
}


def test_a_rust_fix_is_proven_with_a_test_in_the_same_file(tmp_path: Path):
    """Rust keeps unit tests next to the code: the proof takes the fix out and keeps the new test in."""
    need("cargo")
    repo = write(tmp_path, RUST_BUGGY)
    new_test = ("    #[test]\n    fn empty() {", "    #[test]\n    fn counts_the_first_price() {\n"
                "        assert_eq!(total(&[1, 2, 3]), 6);\n    }\n\n    #[test]\n    fn empty() {")
    replies = fix_and_test("src/lib.rs", "prices.iter().skip(1).sum()", "prices.iter().sum()", "src/lib.rs", new_test,
                           edit_test=True)
    outcome = session(repo, replies, "total() ignores the first price")
    assert outcome.fixed, outcome.error
    assert outcome.proof.status == "proven", outcome.proof.red_output + outcome.proof.green_output
    assert outcome.proof.tests == ["src/lib.rs"] and outcome.proof.red == "1 passed, 1 failed"
    assert outcome.regression.status == "clean"
    assert "skip(1)" not in (repo / "src" / "lib.rs").read_text(encoding="utf-8")  # the fix is back in


def test_a_rust_proof_that_changes_only_tests_proves_nothing(tmp_path: Path):
    need("cargo")
    repo = write(tmp_path, RUST_BUGGY)
    before = RUST_BUGGY["src/lib.rs"]
    (repo / "src" / "lib.rs").write_bytes(before.replace("fn empty()", "fn empty_again()").encode())
    assert prove_fix(repo, {"src/lib.rs": before}, {"src/lib.rs"}).status == "no_code_change"


JAVA_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.shop</groupId>
  <artifactId>shop</artifactId>
  <version>1.0</version>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.junit.jupiter</groupId>
      <artifactId>junit-jupiter</artifactId>
      <version>5.11.4</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-surefire-plugin</artifactId>
        <version>3.5.2</version>
      </plugin>
    </plugins>
  </build>
</project>
"""
JAVA_BUGGY = {
    "pom.xml": JAVA_POM,
    "src/main/java/com/shop/Cart.java": "package com.shop;\n\npublic class Cart {\n    /** Adds up every price. */\n"
                                        "    public static int total(int[] prices) {\n        int sum = 0;\n"
                                        "        for (int i = 1; i < prices.length; i++) {\n            sum += prices[i];\n"
                                        "        }\n        return sum;\n    }\n}\n",
    "src/test/java/com/shop/CartTest.java": "package com.shop;\n\nimport org.junit.jupiter.api.Test;\n"
                                            "import static org.junit.jupiter.api.Assertions.assertEquals;\n\n"
                                            "class CartTest {\n    @Test\n    void empty() {\n"
                                            "        assertEquals(0, Cart.total(new int[0]));\n    }\n}\n",
}
JAVA_TEST = ("package com.shop;\n\nimport org.junit.jupiter.api.Test;\nimport static org.junit.jupiter.api.Assertions.assertEquals;\n\n"
             "class CartTotalTest {\n    @Test\n    void countsTheFirstPrice() {\n"
             "        assertEquals(6, Cart.total(new int[] {1, 2, 3}));\n    }\n}\n")


def test_a_java_fix_is_proven_and_guarded_with_maven(tmp_path: Path):
    need("mvn", "java")
    repo = write(tmp_path, JAVA_BUGGY)
    outcome = session(repo, fix_and_test("src/main/java/com/shop/Cart.java", "int i = 1", "int i = 0",
                                         "src/test/java/com/shop/CartTotalTest.java", JAVA_TEST),
                      "Cart.total() ignores the first price")
    assert outcome.fixed, outcome.error
    assert outcome.proof.status == "proven", outcome.proof.red_output + outcome.proof.green_output
    assert outcome.proof.red == "0 passed, 1 failed" and outcome.proof.green == "1 passed, 0 failed"
    assert outcome.regression.status == "clean" and outcome.regression.passed_after
