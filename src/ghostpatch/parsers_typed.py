"""Go, Rust and Java: statically typed languages, parsed with tree-sitter.

Besides symbols, calls and imports, these parsers note the types the code states outright:
a parameter `cart *Cart`, a local `Discount d = ...`, `let d = Discount::new()`, `new Cart()`,
a Java field. A call on such a value is recorded with the receiver ":Cart", so the graph can
link `cart.Total()` to `Cart.Total` exactly instead of guessing by name.

Test code is recognised the way each language marks it:
- Go: `TestXxx`, `BenchmarkXxx`, `FuzzXxx` and `ExampleXxx` functions in `_test.go` files,
- Rust: `#[test]` functions and everything in a `#[cfg(test)]` module, which usually sits in the
  same file as the code it tests,
- Java: methods annotated `@Test`, `@ParameterizedTest`, `@RepeatedTest`, `@TestFactory` or `@TestTemplate`.
"""

from __future__ import annotations

import re

from ghostpatch.parsers import FileFacts, ParseError, _Builder, _text, is_test_path, module_name, tree_parser

GO_TEST_PREFIXES = ("Test", "Benchmark", "Fuzz", "Example")
JAVA_TEST_ANNOTATIONS = {"Test", "ParameterizedTest", "RepeatedTest", "TestFactory", "TestTemplate"}
JAVA_CLASSES = {
    "class_declaration": "class", "interface_declaration": "interface", "enum_declaration": "enum",
    "record_declaration": "record", "annotation_type_declaration": "@interface",
}
RUST_TYPES = {"struct_item": "struct", "enum_item": "enum", "union_item": "union"}
RUST_TEST_ATTRIBUTE = re.compile(r"#\[(?:\w+::)*test\b|#\[cfg\(\s*test\s*\)\]")
RUST_CFG_TEST = re.compile(r"#\[cfg\(\s*test\s*\)\]")
RUST_COMMENTS = ("line_comment", "block_comment")
MAX_MACRO_DEPTH = 6


def extract_typed(source: str, rel_path: str, language: str) -> FileFacts:
    root = tree_parser(language).parse(source.encode("utf-8")).root_node
    try:
        if language == "go":
            return _GoVisitor(rel_path).run(root)
        if language == "rust":
            return _RustVisitor(rel_path).run(root)
        return _JavaVisitor(rel_path).run(root)
    except RecursionError as e:
        raise ParseError("file is nested too deeply to analyse") from e


def rust_module_path(rel_path: str) -> list[str]:
    """The Rust module a file is: 'src/shop/cart.rs' -> ['shop', 'cart']; lib.rs, main.rs and
    files outside src/ (integration tests, examples) are crate roots -> []."""
    parts = rel_path.split("/")
    if "src" not in parts:
        return []
    parts = parts[len(parts) - parts[::-1].index("src"):]
    stem = parts[-1].removesuffix(".rs")
    parts = parts[:-1] + ([] if stem in ("lib", "main", "mod") else [stem])
    return [] if parts[:1] == ["bin"] else parts


def rust_split(source: str) -> tuple[str, str]:
    """(the code, the test modules): a Rust file's `#[cfg(test)] mod ... { }` blocks taken out.

    Lets the proof take a fix out of a file while keeping the new tests in the same file."""
    data = source.encode("utf-8")
    root = tree_parser("rust").parse(data).root_node
    spans: list[tuple[int, int]] = []
    attributes: list = []
    for node in root.named_children:
        if node.type in RUST_COMMENTS:
            continue
        if node.type == "attribute_item":
            attributes.append(node)
            continue
        if node.type == "mod_item" and any(RUST_CFG_TEST.search(_text(a)) for a in attributes):
            spans.append((attributes[0].start_byte, node.end_byte))
        attributes = []
    code, tests, last = [], [], 0
    for start, end in spans:
        code.append(data[last:start])
        tests.append(data[start:end])
        last = end
    code.append(data[last:])
    return b"".join(code).decode("utf-8"), "\n\n".join(t.decode("utf-8") for t in tests)


class _TypedVisitor:
    """Shared walking, scopes of variable types, and call recording."""

    def __init__(self, rel_path: str, module: str):
        self.rel_path = rel_path
        self.b = _Builder(module)
        self.scopes: list[dict[str, str]] = [{}]
        self.line_offset = 0

    def line(self, node) -> int:
        return node.start_point[0] + 1 + self.line_offset

    def end_line(self, node) -> int:
        return node.end_point[0] + 1 + self.line_offset

    def visit(self, node) -> None:
        handler = getattr(self, f"_{node.type}", None)
        # A handler returns None when it has handled the node's children itself,
        # or False when the normal walk into the children should continue.
        if handler is not None and handler(node) is not False:
            return
        for child in node.named_children:
            self.visit(child)

    def type_of(self, name: str) -> str | None:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return None

    def bind(self, name: str | None, type_name: str | None) -> None:
        if name and type_name:
            self.scopes[-1][name] = type_name

    def scope(self, name: str, kind: str, node, signature: str, body, variables: dict[str, str] | None = None,
              under: int | None = None, prefix: str | None = None) -> None:
        self.b.enter(name, kind, self.line(node), self.end_line(node), signature, under=under, prefix=prefix)
        self.scopes.append(dict(variables or {}))
        if body is not None:
            self.visit(body)
        self.scopes.pop()
        self.b.exit()

    def call(self, callee: str, node, receiver: str | None = None) -> None:
        if callee:
            self.b.call(callee, self.line(node), receiver)

    def typed(self, identifier: str) -> str:
        """The receiver for a call on a plain identifier: its type if the code states it."""
        known = self.type_of(identifier)
        return f":{known}" if known else identifier


def _field(node, name: str):
    return node.child_by_field_name(name) if node is not None else None


def _signature(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# ------------------------------------------------------------------------------ Go


class _GoVisitor(_TypedVisitor):
    def __init__(self, rel_path: str):
        super().__init__(rel_path, "")
        self.test_file = rel_path.endswith("_test.go")
        self.types: dict[str, int] = {}  # type name -> symbol index, for methods declared elsewhere in the file

    def run(self, root) -> FileFacts:
        package = next((c for c in root.named_children if c.type == "package_clause"), None)
        name = next((_text(c) for c in package.named_children), "") if package is not None else ""
        self.b.module = name or module_name(self.rel_path).rsplit(".", 1)[-1]
        for node in root.named_children:  # types first: a method may come before its type
            if node.type == "type_declaration":
                for spec in node.named_children:
                    if spec.type in ("type_spec", "type_alias"):
                        self._declare_type(spec)
        for node in root.named_children:
            if node.type not in ("type_declaration", "package_clause"):
                self.visit(node)
        return self.b.facts

    @staticmethod
    def base_type(node) -> str | None:
        """'*Cart' / 'pricing.Rule' / 'Box[T]' -> the type's name; None for slices, maps, funcs..."""
        while node is not None and node.type in ("pointer_type", "generic_type", "parenthesized_type"):
            node = _field(node, "type") if node.type == "generic_type" else (node.named_children or [None])[0]
        if node is None:
            return None
        if node.type == "type_identifier":
            return _text(node)
        if node.type == "qualified_type":
            return _text(_field(node, "name"))
        return None

    def _declare_type(self, spec) -> None:
        name = _text(_field(spec, "name"))
        shape = _field(spec, "type")
        what = {"struct_type": "struct", "interface_type": "interface"}.get(shape.type if shape else "", "")
        signature = f"type {name} {what or _signature(_text(shape))[:60]}".strip()
        self.b.enter(name, "class", self.line(spec), self.end_line(spec), signature)
        self.b.exit()
        self.types[name] = len(self.b.facts.symbols) - 1

    def _type_declaration(self, node):
        return None  # local types inside functions: nothing to link

    def parameters(self, node) -> dict[str, str]:
        out: dict[str, str] = {}
        for param in (node.named_children if node is not None else []):
            type_name = self.base_type(_field(param, "type"))
            for name in param.children_by_field_name("name"):
                if type_name:
                    out[_text(name)] = type_name
        return out

    def _function_declaration(self, node):
        name = _text(_field(node, "name"))
        params, result = _field(node, "parameters"), _field(node, "result")
        kind = "test" if self.test_file and name.startswith(GO_TEST_PREFIXES) else "function"
        signature = f"func {name}{_text(params)}" + (f" {_text(result)}" if result is not None else "")
        self.scope(name, kind, node, _signature(signature), _field(node, "body"), self.parameters(params))

    def _method_declaration(self, node):
        name = _text(_field(node, "name"))
        receiver, params, result = _field(node, "receiver"), _field(node, "parameters"), _field(node, "result")
        variables = self.parameters(params)
        first = receiver.named_children[0] if receiver is not None and receiver.named_children else None
        owner = self.base_type(_field(first, "type")) if first is not None else None
        receiver_name = _field(first, "name") if first is not None else None
        if receiver_name is not None and owner:
            variables[_text(receiver_name)] = owner
        signature = f"func {_text(receiver)} {name}{_text(params)}" + (f" {_text(result)}" if result is not None else "")
        under = self.types.get(owner or "")
        prefix = None if under is not None else ".".join(p for p in (self.b.module, owner) if p)
        self.scope(name, "method", node, _signature(signature), _field(node, "body"), variables, under=under, prefix=prefix)

    def infer(self, node) -> str | None:
        """The type of a value when the code makes it obvious: `Cart{}`, `&Cart{}`, `NewCart()`."""
        if node is None:
            return None
        if node.type == "composite_literal":
            return self.base_type(_field(node, "type"))
        if node.type == "unary_expression":
            return self.infer(_field(node, "operand"))
        if node.type == "call_expression":
            fn = _field(node, "function")
            name = _text(_field(fn, "field")) if fn is not None and fn.type == "selector_expression" else _text(fn)
            match = re.fullmatch(r"[Nn]ew([A-Z]\w*)", name)
            return match.group(1) if match else None
        return None

    def _var_spec(self, node):
        type_name = self.base_type(_field(node, "type"))
        values = _field(node, "value")
        names = node.children_by_field_name("name")
        exprs = values.named_children if values is not None else []
        for i, name in enumerate(names):
            self.bind(_text(name), type_name or (self.infer(exprs[i]) if i < len(exprs) else None))
        return False

    def _short_var_declaration(self, node):
        left, right = _field(node, "left"), _field(node, "right")
        names = left.named_children if left is not None else []
        exprs = right.named_children if right is not None else []
        for i, name in enumerate(names):
            if name.type == "identifier" and i < len(exprs):
                self.bind(_text(name), self.infer(exprs[i]))
        return False

    def _call_expression(self, node):
        fn = _field(node, "function")
        if fn is not None and fn.type == "identifier":
            self.call(_text(fn), node)
        elif fn is not None and fn.type == "selector_expression":
            operand = _field(fn, "operand")
            if operand is not None and operand.type == "identifier":
                receiver = self.typed(_text(operand))  # a typed value, or a package: `pricing.Apply()`
            else:
                receiver = f":{self.infer(operand)}" if self.infer(operand) else "?"
            self.call(_text(_field(fn, "field")), node, receiver)
        return False

    def _import_spec(self, node):
        path = _text(_field(node, "path")).strip('"`')
        alias = _field(node, "name")
        name = _text(alias) if alias is not None else path.rsplit("/", 1)[-1]
        if name not in ("_", "."):
            self.b.import_(path, "*", self.line(node), name)
        return None


# ---------------------------------------------------------------------------- Rust


class _RustVisitor(_TypedVisitor):
    def __init__(self, rel_path: str):
        self.module_path = rust_module_path(rel_path)
        in_src = "src" in rel_path.split("/")
        super().__init__(rel_path, ".".join(self.module_path) if in_src else module_name(rel_path))
        self.in_test = False
        self.impl_types: list[str] = []  # the type of each `impl` block we are in
        self.macro_depth = 0

    def run(self, root) -> FileFacts:
        self.items(root)
        return self.b.facts

    # --- items -------------------------------------------------------------------

    def items(self, container) -> None:
        attributes: list[str] = []
        for child in container.named_children:
            if child.type in RUST_COMMENTS:
                continue
            if child.type == "attribute_item":
                attributes.append(_text(child))
                continue
            self.item(child, attributes)
            attributes = []

    def _declaration_list(self, node):
        self.items(node)

    def item(self, node, attributes: list[str]) -> None:
        if node.type == "function_item":
            self.function(node, attributes)
        elif node.type == "mod_item":
            self.module(node, attributes)
        elif node.type == "impl_item":
            self.impl(node)
        elif node.type == "trait_item":
            name = _text(_field(node, "name"))
            self.impl_types.append(name)
            self.scope(name, "suite" if self.in_test else "class", node, f"trait {name}", _field(node, "body"))
            self.impl_types.pop()
        elif node.type in RUST_TYPES:
            name = _text(_field(node, "name"))
            self.b.enter(name, "suite" if self.in_test else "class", self.line(node), self.end_line(node),
                         f"{RUST_TYPES[node.type]} {name}")
            self.b.exit()
        else:
            self.visit(node)

    def function(self, node, attributes: list[str]) -> None:
        name = _text(_field(node, "name"))
        params, returns = _field(node, "parameters"), _field(node, "return_type")
        if self.in_test or any(RUST_TEST_ATTRIBUTE.search(a) for a in attributes):
            kind = "test"
        else:
            kind = "method" if self.impl_types else "function"
        signature = f"fn {name}{_text(params)}" + (f" -> {_text(returns)}" if returns is not None else "")
        variables = {}
        for param in (params.named_children if params is not None else []):
            if param.type == "parameter":
                pattern = _field(param, "pattern")
                if pattern is not None and pattern.type == "mut_pattern":
                    pattern = pattern.named_children[0] if pattern.named_children else None
                if pattern is not None and pattern.type == "identifier":
                    type_name = self.base_type(_field(param, "type"))
                    if type_name:
                        variables[_text(pattern)] = type_name
        self.scope(name, kind, node, _signature(signature), _field(node, "body"), variables)

    def module(self, node, attributes: list[str]) -> None:
        body = _field(node, "body")
        if body is None:  # `mod pricing;` only says where a file is
            return
        name = _text(_field(node, "name"))
        testing = self.in_test or any(RUST_CFG_TEST.search(a) for a in attributes)
        was_testing, self.in_test = self.in_test, testing
        self.module_path.append(name)
        if testing:
            self.scope(name, "suite", node, f"mod {name}", body)
        else:  # a plain inline module: its items are named after it, but it isn't a symbol itself
            saved = self.b.module
            self.b.module = f"{saved}.{name}" if saved else name
            self.items(body)
            self.b.module = saved
        self.module_path.pop()
        self.in_test = was_testing

    def impl(self, node) -> None:
        type_name = self.base_type(_field(node, "type")) or _text(_field(node, "type"))
        trait = _field(node, "trait")
        signature = f"impl {_text(trait)} for {type_name}" if trait is not None else f"impl {type_name}"
        self.impl_types.append(type_name)
        self.scope(type_name, "suite" if self.in_test else "class", node, signature, _field(node, "body"))
        self.impl_types.pop()

    # --- types ---------------------------------------------------------------------

    def base_type(self, node) -> str | None:
        """'&mut Cart' / 'Box<Cart>' / 'crate::cart::Cart' / 'Self' -> the type's name."""
        while node is not None and node.type in ("reference_type", "pointer_type", "generic_type"):
            node = _field(node, "type")
        if node is None:
            return None
        if node.type == "type_identifier":
            name = _text(node)
            return (self.impl_types[-1] if self.impl_types else None) if name == "Self" else name
        if node.type == "scoped_type_identifier":
            return _text(_field(node, "name"))
        return None

    def infer(self, node) -> str | None:
        """`Cart::new()` / `Cart { .. }` / `&Cart::default()` -> Cart."""
        if node is None:
            return None
        if node.type == "reference_expression":
            return self.infer(_field(node, "value"))
        if node.type == "struct_expression":
            return self.base_type(_field(node, "name")) or _text(_field(node, "name")).rsplit("::", 1)[-1]
        if node.type == "call_expression":
            fn = _field(node, "function")
            if fn is not None and fn.type == "scoped_identifier":
                path = _text(_field(fn, "path"))
                if path == "Self":
                    return self.impl_types[-1] if self.impl_types else None
                last = path.rsplit("::", 1)[-1]
                return last if last[:1].isupper() else None
        return None

    def _let_declaration(self, node):
        pattern = _field(node, "pattern")
        if pattern is not None and pattern.type == "mut_pattern":
            pattern = pattern.named_children[0] if pattern.named_children else None
        if pattern is not None and pattern.type == "identifier":
            self.bind(_text(pattern), self.base_type(_field(node, "type")) or self.infer(_field(node, "value")))
        return False

    # --- calls and imports ---------------------------------------------------------

    def absolute(self, segments: list[str]) -> list[str]:
        """Resolve `crate::` / `self::` / `super::` paths to ['crate', ...modules]."""
        if not segments:
            return segments
        if segments[0] == "crate":
            return segments
        if segments[0] == "self":
            return ["crate", *self.module_path, *segments[1:]]
        if segments[0] == "super":
            ups = len(segments) - len([s for s in segments if s != "super"])
            base = self.module_path[: max(0, len(self.module_path) - ups)]
            return ["crate", *base, *segments[ups:]]
        return segments  # another crate, or a module relative to this one: the graph works it out

    def _call_expression(self, node):
        fn = _field(node, "function")
        if fn is not None and fn.type == "generic_function":
            fn = _field(fn, "function")
        if fn is not None and fn.type == "identifier":
            self.call(_text(fn), node)
        elif fn is not None and fn.type == "field_expression":
            value = _field(fn, "value")
            if value is not None and value.type == "self":
                receiver = "self"
            elif value is not None and value.type == "identifier":
                known = self.type_of(_text(value))
                receiver = f":{known}" if known else "?"
            else:
                inferred = self.infer(value)
                receiver = f":{inferred}" if inferred else "?"
            self.call(_text(_field(fn, "field")), node, receiver)
        elif fn is not None and fn.type == "scoped_identifier":
            path = _field(fn, "path")
            text = _text(path).replace(" ", "")
            if text == "Self":
                receiver = "self"
            elif path is not None and path.type in ("generic_type", "scoped_type_identifier"):
                receiver = f":{self.base_type(path)}" if self.base_type(path) else "?"
            elif text.rsplit("::", 1)[-1][:1].isupper():  # `Cart::new()`, `cart::Cart::new()`
                receiver = f":{text.rsplit('::', 1)[-1]}"
            else:  # a module path: `pricing::apply()`, `crate::pricing::apply()`
                receiver = "::".join(self.absolute(text.split("::")))
            self.call(_text(_field(fn, "name")), node, receiver)
        return False

    def _struct_expression(self, node):
        name = self.base_type(_field(node, "name")) or _text(_field(node, "name")).rsplit("::", 1)[-1]
        self.call(name, node)
        return False

    def _macro_invocation(self, node):
        """Macro arguments are raw tokens to tree-sitter, yet `assert_eq!(total(&cart), 3)` is how
        tests call code. So the arguments are parsed again as an expression."""
        tokens = next((c for c in node.children if c.type == "token_tree"), None)
        if tokens is None or self.macro_depth >= MAX_MACRO_DEPTH:
            return None
        inner = _text(tokens)[1:-1]
        wrapped = tree_parser("rust").parse(f"fn __ghostpatch__() {{ ({inner}); }}".encode("utf-8")).root_node
        body = next((_field(c, "body") for c in wrapped.named_children if c.type == "function_item"), None)
        if body is None:
            return None
        saved = self.line_offset
        self.line_offset = self.line(tokens) - 1  # the wrapper starts on the line the arguments start on
        self.macro_depth += 1
        try:
            self.visit(body)
        finally:
            self.macro_depth -= 1
            self.line_offset = saved
        return None

    def _use_declaration(self, node):
        for segments, alias in self.use_paths(_field(node, "argument"), []):
            segments = self.absolute(segments)
            if segments[-1:] == ["self"]:  # `use crate::pricing::{self}` names the module itself
                segments = segments[:-1]
            if len(segments) < 2:
                continue
            name = segments[-1]
            self.b.import_("::".join(segments[:-1]), name, self.line(node), "*" if name == "*" else alias)
        return None

    def use_paths(self, node, prefix: list[str]) -> list[tuple[list[str], str | None]]:
        if node is None:
            return []
        if node.type in ("identifier", "crate", "self", "super", "metavariable"):
            return [([*prefix, _text(node)], None)]
        if node.type == "scoped_identifier":
            return [([*prefix, *self.segments(node)], None)]
        if node.type == "use_as_clause":
            return [([*prefix, *self.segments(_field(node, "path"))], _text(_field(node, "alias")))]
        if node.type == "scoped_use_list":
            path = _field(node, "path")
            return self.use_paths(_field(node, "list"), [*prefix, *self.segments(path)] if path is not None else prefix)
        if node.type == "use_list":
            return [p for child in node.named_children for p in self.use_paths(child, prefix)]
        if node.type == "use_wildcard":
            path = node.named_children[0] if node.named_children else None
            return [([*prefix, *self.segments(path), "*"], None)]
        return []

    def segments(self, node) -> list[str]:
        if node is None:
            return []
        if node.type == "scoped_identifier":
            return [*self.segments(_field(node, "path")), _text(_field(node, "name"))]
        return [_text(node)]


# ---------------------------------------------------------------------------- Java


class _JavaVisitor(_TypedVisitor):
    def __init__(self, rel_path: str):
        super().__init__(rel_path, "")
        self.test_file = is_test_path(rel_path)

    def run(self, root) -> FileFacts:
        package = next((c for c in root.named_children if c.type == "package_declaration"), None)
        self.b.module = _text(package.named_children[0]) if package is not None and package.named_children else ""
        for node in root.named_children:
            if node.type != "package_declaration":
                self.visit(node)
        return self.b.facts

    @staticmethod
    def base_type(node) -> str | None:
        """'Cart' / 'List<Item>' / 'com.shop.Cart' -> the class name; None for primitives and arrays."""
        if node is None:
            return None
        if node.type == "type_identifier":
            name = _text(node)
            return None if name == "var" else name
        if node.type == "generic_type":
            first = next((c for c in node.named_children if c.type in ("type_identifier", "scoped_type_identifier")), None)
            return _JavaVisitor.base_type(first)
        if node.type == "scoped_type_identifier":
            last = [c for c in node.named_children if c.type == "type_identifier"]
            return _text(last[-1]) if last else None
        return None

    def _import_declaration(self, node):
        target = next((c for c in node.named_children if c.type in ("scoped_identifier", "identifier")), None)
        if target is None:
            return None
        full = _text(target)
        if any(c.type == "asterisk" for c in node.children):
            self.b.import_(full, "*", self.line(node), "*")
        else:
            module, _, name = full.rpartition(".")
            self.b.import_(module, name, self.line(node), name)
        return None

    def fields(self, body) -> dict[str, str]:
        out: dict[str, str] = {}
        for member in (body.named_children if body is not None else []):
            if member.type in ("field_declaration", "constant_declaration"):
                type_name = self.base_type(_field(member, "type"))
                for declarator in member.children_by_field_name("declarator"):
                    if type_name:
                        out[_text(_field(declarator, "name"))] = type_name
        return out

    def parameters(self, node) -> dict[str, str]:
        out: dict[str, str] = {}
        for param in (node.named_children if node is not None else []):
            if param.type in ("formal_parameter", "spread_parameter"):
                type_name = self.base_type(_field(param, "type") or next(
                    (c for c in param.named_children if c.type.endswith("type") or c.type == "type_identifier"), None))
                name = _field(param, "name") or next((c for c in param.named_children if c.type == "variable_declarator"), None)
                if name is not None and name.type == "variable_declarator":
                    name = _field(name, "name")
                if type_name and name is not None:
                    out[_text(name)] = type_name
        return out

    def _class(self, node):
        name_node, body = _field(node, "name"), _field(node, "body")
        if name_node is None:
            return False
        name = _text(name_node)
        end = body.start_byte if body is not None else node.end_byte
        header = node.text[name_node.start_byte - node.start_byte:end - node.start_byte].decode("utf-8", "replace")
        signature = _signature(f"{JAVA_CLASSES[node.type]} {header}")
        variables = {**self.fields(body), **self.parameters(_field(node, "parameters"))}  # record components are fields
        self.scope(name, "class", node, signature, body, variables)
        return None

    _class_declaration = _interface_declaration = _enum_declaration = _record_declaration = _class
    _annotation_type_declaration = _class

    def annotations(self, node) -> set[str]:
        modifiers = next((c for c in node.named_children if c.type == "modifiers"), None)
        names = set()
        for c in (modifiers.named_children if modifiers is not None else []):
            if c.type in ("marker_annotation", "annotation"):
                names.add(_text(_field(c, "name")).rsplit(".", 1)[-1])
        return names

    def _method_declaration(self, node):
        name = _text(_field(node, "name"))
        params = _field(node, "parameters")
        kind = "test" if self.annotations(node) & JAVA_TEST_ANNOTATIONS else "method"
        signature = _signature(f"{_text(_field(node, 'type'))} {name}{_text(params)}")
        self.scope(name, kind, node, signature, _field(node, "body"), self.parameters(params))
        return None

    def _constructor_declaration(self, node):
        name = _text(_field(node, "name"))
        params = _field(node, "parameters")
        self.scope(name, "method", node, _signature(f"{name}{_text(params)}"), _field(node, "body"), self.parameters(params))
        return None

    def _local_variable_declaration(self, node):
        declared = self.base_type(_field(node, "type"))
        for declarator in node.children_by_field_name("declarator"):
            value = _field(declarator, "value")
            inferred = self.base_type(_field(value, "type")) if value is not None and value.type == "object_creation_expression" else None
            self.bind(_text(_field(declarator, "name")), declared or inferred)
        return False

    def _enhanced_for_statement(self, node):
        self.bind(_text(_field(node, "name")), self.base_type(_field(node, "type")))
        return False

    def receiver(self, obj) -> str:
        if obj.type == "this":
            return "this"
        if obj.type == "super":
            return "?"
        if obj.type == "identifier":
            name = _text(obj)
            known = self.type_of(name)
            if known:
                return f":{known}"
            return f":{name}" if name[:1].isupper() else name  # `Discounts.best()`: a static call
        if obj.type == "object_creation_expression":
            type_name = self.base_type(_field(obj, "type"))
            return f":{type_name}" if type_name else "?"
        if obj.type == "field_access" and _field(obj, "object") is not None and _field(obj, "object").type == "this":
            known = self.type_of(_text(_field(obj, "field")))
            return f":{known}" if known else "?"
        return "?"

    def _method_invocation(self, node):
        obj = _field(node, "object")
        self.call(_text(_field(node, "name")), node, self.receiver(obj) if obj is not None else None)
        return False

    def _object_creation_expression(self, node):
        type_name = self.base_type(_field(node, "type"))
        if type_name:
            self.call(type_name, node, f":{type_name}")  # constructors are named after their class
        for child in node.named_children:
            if child.type == "class_body":  # an anonymous class: its calls belong to the enclosing method
                for member in child.named_children:
                    body = _field(member, "body")
                    if body is not None:
                        self.visit(body)
            elif child.type != "type_identifier":
                self.visit(child)
        return None
