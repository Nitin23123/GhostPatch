"""Linking each call to the symbols it means, following each language's own rules.

The parsers record what a call looks like: `apply(x)`, `self.total()`, `pricing.apply()`,
`cart.Total()` on a value of type Cart (receiver ":Cart"). This works out which function that is:

- Python / JavaScript / TypeScript: same module, then imports (`from shop.pricing import apply`,
  relative JS imports), module aliases, and `self`/`this` methods of the enclosing class.
- Go: a package is a folder, so a plain call can mean any function in the same folder.
  `pricing.Apply()` goes through the import path, which `go.mod` maps to a folder.
- Rust: `use crate::…` / `super::…` / `self::…` imports, module paths (`pricing::apply()`),
  `Type::new()`, and methods from every `impl` block of a type in the same crate.
- Java: a bare `total()` is a method of the enclosing class (or a static import); `Cart.of()` and
  `cart.total()` on a declared `Cart` go to class Cart, found through the file, its imports and
  its package.

A link the code spells out is "exact". A call on a value of unknown type falls back to the
methods with that name, marked as a name match, unless so many share the name that a guess says
nothing. A call into code that isn't in the repository
(`fmt.Println`, `std::cmp::max`, `Math.max`) gets no link at all.
"""

from __future__ import annotations

import posixpath
import re
from collections import defaultdict

from ghostpatch.parsers import language_of, module_name
from ghostpatch.parsers_typed import rust_module_path

JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
SELF_RECEIVERS = {"self", "cls", "this"}
CALLABLE_KINDS = ("function", "method", "class")
RUST_EXTERNAL = ("std", "core", "alloc")
MAX_GUESSES = 5  # name-only matches beyond this are dropped


class Linker:
    def __init__(self, symbols: list[tuple], files: set[str], imports: list[tuple], manifests: dict[str, str]):
        """`symbols`: (id, path, name, qualname, kind, parent_id, signature); `imports`: (path, module, name, alias);
        `manifests`: go.mod and Cargo.toml files by path, with their text."""
        self.files = files
        self.lang = {p: language_of(p) for p in files}
        self.path: dict[int, str] = {}
        self.name: dict[int, str] = {}
        self.qual: dict[int, str] = {}
        self.kind: dict[int, str] = {}
        self.parent: dict[int, int | None] = {}
        impl_blocks: set[int] = set()  # Rust `impl Cart { }`: holds methods, but isn't the type itself
        for i, path, name, qual, kind, parent, signature in symbols:
            self.path[i], self.name[i], self.qual[i], self.kind[i], self.parent[i] = path, name, qual, kind, parent
            if kind == "class" and signature.startswith("impl ") and path.endswith(".rs"):
                impl_blocks.add(i)

        self.go_modules: dict[str, str] = {}  # Go module path -> its folder
        self.crates: dict[str, str] = {}  # Rust crate folder -> crate name as written in code
        for rel, text in manifests.items():
            folder = posixpath.dirname(rel)
            if rel.endswith("go.mod"):
                found = re.search(r"^module\s+(\S+)", text, flags=re.MULTILINE)
                if found:
                    self.go_modules[found.group(1)] = folder
            else:
                package = re.search(r"^\[package\]\s*$(.*?)(?=^\[|\Z)", text, flags=re.MULTILINE | re.DOTALL)
                name = re.search(r'^name\s*=\s*"([^"]+)"', package.group(1), flags=re.MULTILINE) if package else None
                if name:
                    self.crates[folder] = name.group(1).replace("-", "_")
        self._crate_of: dict[str, str] = {}

        self.top: dict[tuple[str, str], list[int]] = defaultdict(list)  # (path, name) -> module-level symbols
        self.package_top: dict[tuple[str, str], list[int]] = defaultdict(list)  # Go: (folder, name) -> ...
        self.by_name: dict[str, list[int]] = defaultdict(list)
        self.by_qualname: dict[str, list[int]] = defaultdict(list)
        self.members: dict[tuple, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
        self.types: dict[str, list[tuple[tuple, int]]] = defaultdict(list)  # type name -> [(type key, class id)]
        self.type_ids: dict[tuple, list[int]] = defaultdict(list)
        self.java_package: dict[str, str] = {}
        for i in self.path:
            path, name, kind, parent = self.path[i], self.name[i], self.kind[i], self.parent[i]
            lang = self.lang.get(path)
            self.by_name[name].append(i)
            self.by_qualname[self.qual[i]].append(i)
            if parent is None and not self._go_method(i) and i not in impl_blocks:
                self.top[(path, name)].append(i)
                if lang == "go":
                    self.package_top[(posixpath.dirname(path), name)].append(i)
            if kind == "class":
                key = self.type_key(i)
                self.types[name].append((key, i))
                if i not in impl_blocks:
                    self.type_ids[key].append(i)
                if lang == "java" and parent is None and self.qual[i] != name:
                    self.java_package[path] = self.qual[i][: -len(name) - 1]
            owner = self.owner(i)
            if owner is not None:
                self.members[owner][name].append(i)

        self.modules = {module_name(p): p for p in files if p.endswith(".py")}
        self.imports: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)
        self.wildcards: dict[str, list[str]] = defaultdict(list)  # Rust `use x::*`, Java `import x.*`
        for path, module, name, alias in imports:
            if name == "*" and self.lang.get(path) in ("rust", "java"):
                self.wildcards[path].append(module)
            elif alias:
                self.imports[path][alias] = (module, name)

    # ------------------------------------------------------------------ types

    def _go_method(self, i: int) -> bool:
        """A Go method whose type is declared in another file (it has no parent symbol)."""
        return self.kind[i] == "method" and self.parent[i] is None and self.lang.get(self.path[i]) == "go"

    def crate_of(self, path: str) -> str:
        if path not in self._crate_of:
            folders = [f for f in self.crates if f == "" or path.startswith(f + "/")]
            self._crate_of[path] = max(folders, key=len, default="")
        return self._crate_of[path]

    def type_key(self, class_id: int) -> tuple:
        """What identifies a type. In Go its methods can sit in any file of the package, and in Rust
        in any `impl` block of the crate, so those are keyed by name; elsewhere by the class itself."""
        path, lang = self.path[class_id], self.lang.get(self.path[class_id])
        if lang == "go":
            return ("go", posixpath.dirname(path), self.name[class_id])
        if lang == "rust":
            return ("rust", self.crate_of(path), self.name[class_id])
        return ("id", class_id)

    def owner(self, i: int) -> tuple | None:
        """The type a method belongs to, if any."""
        parent = self.parent[i]
        if parent is not None and self.kind.get(parent) == "class":
            return self.type_key(parent)
        if self._go_method(i) and self.qual[i].count(".") >= 1:
            return ("go", posixpath.dirname(self.path[i]), self.qual[i].split(".")[-2])
        return None

    def enclosing(self, symbol_id: int | None) -> list[tuple]:
        """The types around a symbol, innermost first."""
        keys: list[tuple] = []
        while symbol_id is not None and symbol_id in self.kind:
            if self.kind[symbol_id] == "class":
                keys.append(self.type_key(symbol_id))
            elif self._go_method(symbol_id):
                keys.append(self.owner(symbol_id))
            symbol_id = self.parent.get(symbol_id)
        return keys

    def members_named(self, keys: list[tuple], name: str) -> list[int]:
        return [m for key in keys for m in self.members.get(key, {}).get(name, [])]

    def type_lookup(self, path: str, type_name: str) -> tuple[list[tuple], int]:
        """The type a name in `path` refers to: (type keys, 1 if certain)."""
        type_name = re.split(r"\.|::", type_name)[-1]
        candidates = self.types.get(type_name, [])
        keys = list(dict.fromkeys(k for k, _ in candidates))
        if len(keys) <= 1:
            return keys, 1 if keys else 0
        lang = self.lang.get(path)
        narrowings = [lambda c: self.path[c] == path]
        if lang == "go":
            narrowings.append(lambda c: posixpath.dirname(self.path[c]) == posixpath.dirname(path))
        elif lang == "rust":
            narrowings.append(lambda c: self.crate_of(self.path[c]) == self.crate_of(path))
        elif lang == "java":
            imported = self.imports[path].get(type_name)
            packages = [self.java_package.get(path, ""), *self.wildcards.get(path, [])]
            if imported:
                narrowings.append(lambda c: self.qual[c] == f"{imported[0]}.{imported[1]}")
            narrowings.append(lambda c: any(self.qual[c] == f"{p}.{type_name}".lstrip(".") for p in packages))
        else:
            imported = self.imports[path].get(type_name)
            target = self.module_file(path, imported[0]) if imported else None
            narrowings.append(lambda c: self.path[c] == target)
        for narrow in narrowings:
            chosen = [(k, c) for k, c in candidates if narrow(c)]
            if chosen:
                picked = list(dict.fromkeys(k for k, c in self.closest(path, chosen)))
                return picked, 1 if len(picked) == 1 else 0
        picked = list(dict.fromkeys(k for k, c in self.closest(path, candidates)))
        return picked, 1 if len(picked) == 1 else 0

    def closest(self, path: str, candidates: list[tuple]) -> list[tuple]:
        """Of (key, symbol id) pairs, the ones nearest `path` in the folder tree: a repository can hold
        two copies of a library (Guava's android/ flavour), and a call means the one beside it."""
        if len(candidates) < 2:
            return candidates
        folders = path.split("/")[:-1]

        def shared(item: tuple) -> int:
            other = self.path[item[1]].split("/")[:-1]
            return next((i for i, (a, b) in enumerate(zip(folders, other)) if a != b), min(len(folders), len(other)))

        best = max(shared(c) for c in candidates)
        return [c for c in candidates if shared(c) == best]

    def one_per_method(self, ids: list[int]) -> list[int]:
        """One target per method: a call to an overloaded method (Guava's checkNotNull has dozens)
        links to the first of them, not to all. Queries by name still find every overload."""
        seen: set[tuple[str, str]] = set()
        out = []
        for i in ids:
            key = (self.path[i], self.qual[i])
            if key not in seen:
                seen.add(key)
                out.append(i)
        return out

    # ---------------------------------------------------------------- modules

    def module_file(self, path: str, module: str) -> str | None:
        """Python / JS: the file an import refers to, from the importing file's point of view."""
        if path.endswith(".py"):
            return _python_module_file(path, module, self.modules)
        return _js_module_file(path, module, self.files)

    def go_folder(self, import_path: str) -> str | None:
        """The folder of a Go package in this repository, or None for other modules (fmt, net/http...)."""
        for module, folder in self.go_modules.items():
            if import_path == module or import_path.startswith(module + "/"):
                return posixpath.join(folder, import_path[len(module):].lstrip("/")).strip("/")
        if not self.go_modules:  # no go.mod: match the import path's end against the folders we know
            folders = {posixpath.dirname(p) for p in self.files if p.endswith(".go")}
            parts = import_path.split("/")
            for n in range(len(parts), 0, -1):
                if "/".join(parts[-n:]) in folders:
                    return "/".join(parts[-n:])
        return None

    def rust_module_file(self, path: str, module: str) -> str | None:
        """'crate::shop::cart' -> 'src/shop/cart.rs' (or .../mod.rs), in the crate `path` belongs to."""
        segments = module.split("::")
        crate = self.crate_of(path)
        if segments[0] != "crate":
            if segments[0] and segments[0] == self.crates.get(crate):  # integration tests name their crate
                segments = ["crate", *segments[1:]]
            else:
                return None
        src = f"{crate}/src" if crate else "src"
        rest = "/".join(segments[1:])
        candidates = [f"{src}/lib.rs", f"{src}/main.rs"] if not rest else [f"{src}/{rest}.rs", f"{src}/{rest}/mod.rs"]
        return next((c for c in candidates if c in self.files), None)

    def rust_candidates(self, path: str, receiver: str) -> list[str]:
        """What a Rust module path in a call (`pricing::apply()`) could mean, most likely first."""
        segments = receiver.split("::")
        if segments[0] == "crate":
            return [receiver]
        out = []
        imported = self.imports[path].get(segments[0])
        if imported:
            out.append("::".join([imported[0], imported[1], *segments[1:]]))
        out.append("::".join(["crate", *rust_module_path(path), *segments]))  # a child module of this one
        out.append("::".join(["crate", *segments]))
        if segments[0] == self.crates.get(self.crate_of(path)):
            out.append("::".join(["crate", *segments[1:]]))
        return out

    def module_member(self, path: str, module: str, name: str) -> list[int]:
        """The symbol `name` exported by `module`, as written in an import."""
        lang = self.lang.get(path)
        if lang == "go":
            folder = self.go_folder(module)
            return list(self.package_top.get((folder, name), [])) if folder is not None else []
        if lang == "rust":
            file = self.rust_module_file(path, module)
            return list(self.top.get((file, name), [])) if file else []
        if lang == "java":  # `import a.b.C` (a class) or `import static a.b.C.m` (a member)
            found = [(None, i) for i in self.by_qualname.get(f"{module}.{name}", []) if self.kind[i] in CALLABLE_KINDS]
            return [i for _, i in self.closest(path, found)]
        file = self.module_file(path, module)
        return list(self.top.get((file, name), [])) if file else []

    # ------------------------------------------------------------------ calls

    def by_kind(self, name: str, kinds: tuple[str, ...]) -> list[int]:
        """A guess by name alone. When many functions share the name (`new`, `len`, `get`), a guess
        says nothing and only floods impact reports, so there is none."""
        found = [i for i in self.by_name.get(name, []) if self.kind[i] in kinds]
        return found if len(found) <= MAX_GUESSES else []

    def targets(self, path: str, caller: int | None, callee: str, receiver: str | None) -> tuple[list[int], int]:
        """(the symbols a call can refer to, 1 if the code says so / 0 if matched by name)."""
        lang = self.lang.get(path)
        if receiver is None:
            return self._plain(path, caller, callee, lang)
        if receiver in SELF_RECEIVERS:
            found = self.members_named(self.enclosing(caller)[:1], callee)
            return (found, 1) if found else (self.by_kind(callee, ("method",)), 0)
        if receiver.startswith(":"):
            type_name = receiver[1:]
            keys, exact = self.type_lookup(path, type_name)
            if not keys:
                return [], 0  # a type from outside the repository: String, Vec, *http.Request...
            found = self.members_named(keys, callee)
            if not found and callee == type_name:  # `new Cart()` without a constructor of its own
                found = [i for key in keys for i in self.type_ids.get(key, [])]
            if found:
                return found, exact
            return self.by_kind(callee, ("method",)), 0  # inherited, or from an interface / trait
        found = self._via_module(path, receiver, callee, lang)
        if found:
            return found, 1
        if found is not None:
            return [], 0  # a module or package outside the repository
        return self.by_kind(callee, ("method",)) or self.by_kind(callee, CALLABLE_KINDS), 0

    def _plain(self, path: str, caller: int | None, callee: str, lang: str | None) -> tuple[list[int], int]:
        if lang == "java":  # a bare `total()` is a method of this class (or an outer one), or a static import
            for key in self.enclosing(caller):
                found = self.members.get(key, {}).get(callee)
                if found:
                    return list(found), 1
            imported = self.imports[path].get(callee)
            if imported:
                found = self.module_member(path, *imported)
                if found:
                    return found, 1
        if self.top.get((path, callee)):
            return list(self.top[(path, callee)]), 1
        if lang == "go":
            found = self.package_top.get((posixpath.dirname(path), callee))
            if found:
                return list(found), 1
        imported = self.imports[path].get(callee)
        if imported:
            module, name = imported
            found = self.module_member(path, module, callee if name in ("*", "default") else name)
            if found:
                return found, 1
        for module in self.wildcards.get(path, []):
            found = self.module_member(path, module, callee)
            if found:
                return found, 1
        return self.by_kind(callee, CALLABLE_KINDS), 0

    def _via_module(self, path: str, receiver: str, callee: str, lang: str | None) -> list[int] | None:
        """A call through a module or package name. None: `receiver` isn't one (maybe a variable);
        []: it is, but the module is outside the repository."""
        if lang == "go":
            imported = self.imports[path].get(receiver)
            if not imported:
                return None
            folder = self.go_folder(imported[0])
            return list(self.package_top.get((folder, callee), [])) if folder is not None else []
        if lang == "rust":
            if receiver.split("::")[0] in RUST_EXTERNAL:
                return []
            for module in self.rust_candidates(path, receiver):
                file = self.rust_module_file(path, module)
                if file and self.top.get((file, callee)):
                    return list(self.top[(file, callee)])
            return None
        if lang == "java":
            return None
        imported = self.imports[path].get(receiver)
        if imported:  # a module alias: `p.apply_discount()` / `util.slugify()`
            module, name = imported
            file = self.module_file(path, module if name == "*" else _join_module(module, name))
            if file and self.top.get((file, callee)):
                return list(self.top[(file, callee)])
        return None


# --------------------------------------------------------- Python / JS module resolution


def _join_module(module: str, name: str) -> str:
    return f"{module}.{name}" if module and not module.endswith(".") else f"{module}{name}"


def _python_module_file(path: str, module: str, modules: dict[str, str]) -> str | None:
    """The file a Python import refers to, from the importing file's point of view."""
    level = len(module) - len(module.lstrip("."))
    rest = module[level:]
    if level:
        package = module_name(path).split(".")
        if not path.endswith("__init__.py"):
            package = package[:-1]
        if level > 1:
            package = package[: -(level - 1)] if level - 1 <= len(package) else []
        full = ".".join([*package, rest] if rest else package)
    else:
        full = rest
    if full in modules:
        return modules[full]
    suffix = [m for m in modules if m.endswith("." + full)]  # e.g. a src/ layout: src.shop.cart for shop.cart
    return modules[suffix[0]] if len(suffix) == 1 else None


def _js_module_file(path: str, spec: str, files: set[str]) -> str | None:
    """The file a relative JS/TS import refers to. Packages (no leading dot) aren't in the graph."""
    if not spec.startswith("."):
        return None
    base = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec))
    stem, ext = posixpath.splitext(base)
    candidates = [base, *(base + e for e in JS_EXTENSIONS), *(f"{base}/index{e}" for e in JS_EXTENSIONS)]
    if ext in (".js", ".jsx", ".mjs", ".cjs"):  # TypeScript projects import "./x.js" for x.ts
        candidates += [stem + e for e in (".ts", ".tsx", ".mts", ".cts")]
    return next((c for c in candidates if c in files), None)
