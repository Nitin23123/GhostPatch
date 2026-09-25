"""Turning source files into facts for the code graph: symbols, calls and imports.

Python is parsed with the standard-library `ast` module. JavaScript and TypeScript
are parsed with tree-sitter. Test blocks such as `test("adds tax", () => ...)` become
named symbols, so the graph can tell which tests exercise which code.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import PurePosixPath

LANGUAGES = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "tsx",
}
SKIPPED_SUFFIXES = (".min.js", ".d.ts", ".bundle.js")
JS_TEST_CALLS = {"test", "it"}
JS_SUITE_CALLS = {"describe", "suite", "context"}


class ParseError(Exception):
    """A file that could not be parsed. The message is stored with the file in the graph."""


@dataclass
class Symbol:
    name: str
    qualname: str
    kind: str  # class | function | method | test | suite
    line: int
    end_line: int
    signature: str
    parent: int | None  # index into FileFacts.symbols


@dataclass
class FileFacts:
    symbols: list[Symbol] = field(default_factory=list)
    calls: list[tuple[int | None, str, int]] = field(default_factory=list)  # (symbol index, callee, line)
    imports: list[tuple[str, str, int]] = field(default_factory=list)  # (module, name, line)


def language_of(rel_path: str) -> str | None:
    if rel_path.endswith(SKIPPED_SUFFIXES):
        return None
    return LANGUAGES.get(PurePosixPath(rel_path).suffix.lower())


def module_name(rel_path: str) -> str:
    """'shop/cart.py' -> 'shop.cart'; package entry files ('__init__.py', 'index.ts') name their folder."""
    path = PurePosixPath(rel_path)
    parts = [*path.parent.parts, path.name[: -len(path.suffix)] if path.suffix else path.name]
    parts = [p for p in parts if p not in ("", ".")]
    if parts and parts[-1] in ("__init__", "index"):
        parts = parts[:-1]
    return ".".join(parts)


def is_test_path(rel_path: str) -> bool:
    name = rel_path.rsplit("/", 1)[-1]
    return (
        name.startswith("test_") or name.endswith("_test.py")
        or ".test." in name or ".spec." in name
        or rel_path.startswith(("tests/", "test/", "__tests__/"))
        or any(f"/{d}/" in rel_path for d in ("tests", "test", "__tests__"))
    )


def extract(source: str, rel_path: str) -> FileFacts:
    language = language_of(rel_path)
    if language == "python":
        return _extract_python(source, rel_path)
    if language is not None:
        return _extract_js(source, rel_path, language)
    raise ParseError(f"unsupported file type: {rel_path}")


class _Builder:
    """Collects symbols while tracking which symbol we are currently inside."""

    def __init__(self, module: str):
        self.module = module
        self.facts = FileFacts()
        self.stack: list[int] = []

    @property
    def parent(self) -> Symbol | None:
        return self.facts.symbols[self.stack[-1]] if self.stack else None

    def enter(self, name: str, kind: str, line: int, end_line: int, signature: str) -> None:
        parent = self.stack[-1] if self.stack else None
        prefix = self.facts.symbols[parent].qualname if parent is not None else self.module
        self.facts.symbols.append(Symbol(
            name=name, qualname=f"{prefix}.{name}" if prefix else name, kind=kind,
            line=line, end_line=end_line, signature=signature, parent=parent,
        ))
        self.stack.append(len(self.facts.symbols) - 1)

    def exit(self) -> None:
        self.stack.pop()

    def call(self, callee: str, line: int) -> None:
        self.facts.calls.append((self.stack[-1] if self.stack else None, callee, line))

    def import_(self, module: str, name: str, line: int) -> None:
        self.facts.imports.append((module, name, line))


# ------------------------------------------------------------------------- Python


def _extract_python(source: str, rel_path: str) -> FileFacts:
    try:
        tree = ast.parse(source, filename=rel_path)
    except (SyntaxError, ValueError) as e:
        raise ParseError(f"could not parse: {e}") from e
    builder = _Builder(module_name(rel_path))
    _PythonVisitor(builder).visit(tree)
    return builder.facts


class _PythonVisitor(ast.NodeVisitor):
    def __init__(self, builder: _Builder):
        self.b = builder

    def _enter(self, node: ast.AST, kind: str, signature: str) -> None:
        if kind == "function" and self.b.parent is not None and self.b.parent.kind == "class":
            kind = "method"
        self.b.enter(node.name, kind, node.lineno, node.end_lineno or node.lineno, signature)
        self.generic_visit(node)
        self.b.exit()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node, "function", _py_signature(node, "def"))

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter(node, "function", _py_signature(node, "async def"))

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        bases = ", ".join(ast.unparse(b) for b in node.bases)
        self._enter(node, "class", f"class {node.name}({bases})" if bases else f"class {node.name}")

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
        if name:
            self.b.call(name, node.lineno)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.b.import_(alias.name, alias.asname or alias.name, node.lineno)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = "." * node.level + (node.module or "")
        for alias in node.names:
            self.b.import_(module, alias.name, node.lineno)


def _py_signature(node: ast.FunctionDef | ast.AsyncFunctionDef, keyword: str) -> str:
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"{keyword} {node.name}({ast.unparse(node.args)}){returns}"


# ------------------------------------------------------------ JavaScript / TypeScript

_PARSERS: dict[str, object] = {}
_FUNCTION_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}
_CLASS_NODES = {"class_declaration", "abstract_class_declaration", "class"}


def _js_parser(language: str):
    if language not in _PARSERS:
        try:
            import tree_sitter
            if language == "javascript":
                import tree_sitter_javascript as grammar
                lang = grammar.language()
            else:
                import tree_sitter_typescript as grammar
                lang = grammar.language_tsx() if language == "tsx" else grammar.language_typescript()
        except ImportError as e:
            raise ParseError(f"tree-sitter is not installed ({e}); run `pip install ghostpatch` again") from e
        _PARSERS[language] = tree_sitter.Parser(tree_sitter.Language(lang))
    return _PARSERS[language]


def _extract_js(source: str, rel_path: str, language: str) -> FileFacts:
    tree = _js_parser(language).parse(source.encode("utf-8"))
    builder = _Builder(module_name(rel_path))
    try:
        _JsVisitor(builder).visit(tree.root_node)
    except RecursionError as e:
        raise ParseError("file is nested too deeply to analyse") from e
    return builder.facts


def _text(node) -> str:
    return node.text.decode("utf-8", errors="replace") if node is not None else ""


def _line(node) -> int:
    return node.start_point[0] + 1


def _end_line(node) -> int:
    return node.end_point[0] + 1


def _params(fn) -> str:
    params = fn.child_by_field_name("parameters")
    if params is not None:
        text = _text(params)
    else:  # `x => ...` has a single `parameter` instead of `parameters`
        single = fn.child_by_field_name("parameter")
        text = f"({_text(single)})" if single is not None else "()"
    return text + _text(fn.child_by_field_name("return_type"))


def _string_value(node) -> str | None:
    if node is None or node.type not in ("string", "template_string"):
        return None
    return _text(node)[1:-1]


class _JsVisitor:
    def __init__(self, builder: _Builder):
        self.b = builder

    def visit(self, node) -> None:
        handler = getattr(self, f"_{node.type}", None)
        # A handler returns None when it has handled the node's children itself,
        # or False when the normal walk into the children should continue.
        if handler is not None and handler(node) is not False:
            return
        self.children(node)

    def children(self, node) -> None:
        for child in node.named_children:
            self.visit(child)

    def _scope(self, name: str, kind: str, node, signature: str, body) -> None:
        self.b.enter(name, kind, _line(node), _end_line(node), signature)
        if body is not None:
            self.visit(body)  # a block, a class body, or an arrow function's expression body
        self.b.exit()

    # --- declarations ------------------------------------------------------------

    def _function_declaration(self, node):
        name = _text(node.child_by_field_name("name"))
        self._scope(name, "function", node, f"function {name}{_params(node)}", node.child_by_field_name("body"))

    _generator_function_declaration = _function_declaration

    def _class_declaration(self, node):
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return False
        name = _text(name_node)
        heritage = next((c for c in node.named_children if c.type == "class_heritage"), None)
        signature = f"class {name} {_text(heritage)}".strip()
        self._scope(name, "class", node, signature, node.child_by_field_name("body"))

    _abstract_class_declaration = _class_declaration

    def _method_definition(self, node):
        name = _text(node.child_by_field_name("name"))
        parent = self.b.parent
        kind = "method" if parent is not None and parent.kind == "class" else "function"
        self._scope(name, kind, node, f"{name}{_params(node)}", node.child_by_field_name("body"))

    def _public_field_definition(self, node):
        value = node.child_by_field_name("value")
        if value is None or value.type not in _FUNCTION_VALUES:
            return False
        name = _text(node.child_by_field_name("name") or node.child_by_field_name("property"))
        self._scope(name, "method", node, f"{name} = {_params(value)} =>", value.child_by_field_name("body"))

    _field_definition = _public_field_definition

    def _variable_declarator(self, node):
        name_node, value = node.child_by_field_name("name"), node.child_by_field_name("value")
        if value is None or name_node is None or name_node.type != "identifier":
            return False
        name = _text(name_node)
        if value.type in _FUNCTION_VALUES:
            arrow = " =>" if value.type == "arrow_function" else ""
            self._scope(name, "function", node, f"const {name} = {_params(value)}{arrow}", value.child_by_field_name("body"))
        elif value.type == "class":
            self._scope(name, "class", node, f"class {name}", value.child_by_field_name("body"))
        else:
            return False

    # --- calls and imports ---------------------------------------------------------

    def _call_expression(self, node):
        fn = node.child_by_field_name("function")
        callee = None
        if fn is not None and fn.type == "identifier":
            callee = _text(fn)
        elif fn is not None and fn.type == "member_expression":
            callee = _text(fn.child_by_field_name("property"))
        args = node.child_by_field_name("arguments")

        if callee in JS_TEST_CALLS | JS_SUITE_CALLS and args is not None:
            arg_nodes = args.named_children
            title = _string_value(arg_nodes[0]) if arg_nodes else None
            callback = next((a for a in arg_nodes[1:] if a.type in _FUNCTION_VALUES), None)
            if title is not None and callback is not None:
                kind = "suite" if callee in JS_SUITE_CALLS else "test"
                self._scope(title, kind, node, f'{callee}("{title}")', callback.child_by_field_name("body"))
                return None

        if callee:
            self.b.call(callee, _line(node))
        return False

    def _new_expression(self, node):
        ctor = node.child_by_field_name("constructor")
        if ctor is not None and ctor.type == "identifier":
            self.b.call(_text(ctor), _line(node))
        return False

    def _import_statement(self, node):
        source = _string_value(node.child_by_field_name("source")) or ""
        names = [c for c in _walk(node) if c.type == "identifier"]
        for name in names or [None]:
            self.b.import_(source, _text(name) if name is not None else "*", _line(node))


def _walk(node):
    for child in node.named_children:
        yield child
        yield from _walk(child)
