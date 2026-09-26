"""🎯 Where to look first: rank the code most likely related to a bug report.

Before the ghost takes a single step, GhostPatch scores every function against the report:
- names mentioned in it (`apply_discount`, `Cart.total`),
- words it shares with a function's name, its file path and its body ("coupon", "discount"),
- exact strings from the report found in the code (`SAVE10`, an error message),
then spreads some of each function's score to the functions it calls and is called by (from
the code graph), because a bug often sits one call away from where it shows.

The best matches go into the ghost's first message with their source, so it starts where the
bug most likely is instead of searching for it. That saves steps and tokens, which matters most
with small free models.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from ghostpatch.parsers import is_test_path

MAX_WITH_SOURCE = 5
MAX_LISTED = 8
MAX_SOURCE_LINES = 30
MAX_CHARS = 6000
MIN_SCORE = 1.5
# How much of a function's score spreads along the call graph. A bug usually sits downstream of
# where it shows (the symptom is in checkout, the cause in something it calls), so callees get more.
CALLEE_SHARE = 0.5
SECOND_CALLEE_SHARE = 0.25
CALLER_SHARE = 0.2
TEST_SHARE = 0.6  # a test that matches the report points at the code it tests

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "when", "then", "than", "from", "into", "should", "would",
    "could", "does", "doesn", "didn", "isn", "are", "was", "were", "has", "have", "had", "not", "but", "its",
    "our", "your", "you", "they", "their", "there", "here", "what", "which", "who", "why", "how", "all", "any",
    "some", "one", "two", "also", "only", "just", "like", "get", "gets", "got", "set", "use", "used", "using",
    "make", "makes", "made", "instead", "expected", "actual", "result", "results", "returns", "return",
    "value", "values", "bug", "issue", "error", "wrong", "correct", "fix", "fixed", "broken", "works", "work",
    "def", "self", "none", "true", "false", "null", "undefined", "function", "method", "class", "file", "code",
    "test", "tests", "line", "call", "calls", "called", "new", "old", "same", "other", "every", "each", "after",
    "before", "without", "because", "still", "shows", "show", "says", "said", "gives", "give", "given",
}


@dataclass
class Suspect:
    qualname: str
    name: str
    kind: str
    path: str
    line: int
    end_line: int
    score: float
    reasons: list[str] = field(default_factory=list)


def split_identifier(name: str) -> list[str]:
    """'applyDiscount' / 'apply_discount' / 'HTTPError' -> ['apply', 'discount'] / ['http', 'error']."""
    parts = []
    for chunk in re.split(r"[_\W]+", name):
        parts += re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|\b)|[A-Z]?[a-z]+|[A-Z]+|\d+", chunk)
    return [p.lower() for p in parts if p]


def stem(word: str) -> str:
    """A deliberately crude stemmer: discounts/discounted/discounting -> discount."""
    for suffix in ("ings", "ing", "ied", "ies", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix in ("ied", "ies") else "")
    return word


def report_terms(text: str) -> tuple[dict[str, float], set[str], set[str]]:
    """(weighted word stems, identifiers mentioned, literal strings worth finding in the code)."""
    weights: dict[str, float] = defaultdict(float)
    identifiers: set[str] = set()
    for token in re.findall(r"[A-Za-z_$][\w$.]*", text):
        token = token.strip(".")
        if "_" in token or re.search(r"[a-z][A-Z]", token) or "." in token:
            identifiers.add(token)
            identifiers.add(token.rsplit(".", 1)[-1])
        for part in split_identifier(token):
            if len(part) >= 3 and part not in STOPWORDS:
                weights[stem(part)] = min(3.0, weights[stem(part)] + 1.0)
    if re.search(r"\d\s*%", text):  # "120%" is about a percentage
        weights["percent"] = min(3.0, weights.get("percent", 0.0) + 1.0)
    quoted = set(re.findall(r"[`'\"]([^`'\"\n]{3,60})[`'\"]", text))
    literals = set(quoted) | set(re.findall(r"\b[A-Z][A-Z0-9_]{3,}\b", text))  # SAVE10, HTTP_TIMEOUT
    for quote in quoted:  # "Total charged: $0.00" -> "Total charged:", the part the code spells out
        literals |= {f.strip() for f in re.split(r"[$€£%]?\d[\d.,]*%?", quote) if len(f.strip()) >= 8}
    return dict(weights), identifiers, literals


def rank(graph: Any, issue: str, limit: int = MAX_LISTED) -> list[Suspect]:
    """The functions most likely related to `issue`, best first."""
    graph.refresh()
    weights, identifiers, literals = report_terms(issue)
    if not weights and not identifiers and not literals:
        return []
    rows = graph.db.execute("SELECT id, path, name, qualname, kind, line, end_line, signature FROM symbols "
                            "WHERE kind IN ('function', 'method', 'class')").fetchall()
    files: dict[str, list[str]] = {}

    def body(path: str, line: int, end_line: int) -> str:
        if path not in files:
            try:
                files[path] = (graph.root / path).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                files[path] = []
        return "\n".join(files[path][line - 1:end_line])

    scores: dict[int, float] = {}
    reasons: dict[int, list[str]] = defaultdict(list)
    info: dict[int, tuple] = {}
    for sym_id, path, name, qualname, kind, line, end_line, signature in rows:
        info[sym_id] = (path, name, qualname, kind, line, end_line)
        score = 0.0
        params = signature.split("(", 1)[1] if "(" in signature else ""
        param_hits = {stem(p) for p in split_identifier(params)} & weights.keys()
        if param_hits:  # discounted(price, percent): the report talks about a price and a percentage
            score += 1.5 * sum(weights[t] for t in param_hits)
            reasons[sym_id].append("parameters match " + ", ".join(f'"{t}"' for t in sorted(param_hits)))
        if name in identifiers or any(qualname.endswith("." + i) for i in identifiers if "." in i):
            score += 8
            reasons[sym_id].append("named in the report")
        name_hits = [t for t in {stem(p) for p in split_identifier(name)} if t in weights]
        if name_hits:
            score += 3 * sum(weights[t] for t in name_hits)
            reasons[sym_id].append("name matches " + ", ".join(f'"{t}"' for t in sorted(name_hits)))
        path_hits = {stem(p) for p in re.split(r"[/_.\-]", path.lower()) if p} & weights.keys()
        score += sum(weights[t] for t in path_hits)
        if kind != "class" or end_line - line < 60:  # a whole class's body says little about one bug
            text = body(path, line, end_line)
            words = Counter(stem(w) for w in split_identifier(" ".join(re.findall(r"[A-Za-z_]\w*", text))))
            body_score = sum(min(3, words[t]) * w for t, w in weights.items() if words[t])
            score += 0.4 * min(body_score, 15)
            found = [lit for lit in literals if lit in text]
            if found:
                score += 4 * len(found)
                reasons[sym_id].append("contains " + ", ".join(f"`{lit}`" for lit in sorted(found)[:3]))
        scores[sym_id] = score

    # Spread scores along the call graph: strongly to what a matching function calls (and what
    # those call), weakly to its callers, and from matching tests to the code they test.
    callees: dict[int, set[int]] = defaultdict(set)
    callers: dict[int, set[int]] = defaultdict(set)
    for caller, callee in graph.db.execute("SELECT caller_id, callee_id FROM edges WHERE caller_id IS NOT NULL"):
        if caller in info and callee in info:
            callees[caller].add(callee)
            callers[callee].add(caller)
    boosted = dict(scores)

    def lend(target: int, amount: float, reason: str) -> None:
        if amount > 0.5 and target in scores and not is_test_path(info[target][0]):
            boosted[target] += amount
            if reason not in reasons[target] and len(reasons[target]) < 4:
                reasons[target].append(reason)

    seeds = sorted((i for i in scores if scores[i] >= MIN_SCORE), key=lambda i: -scores[i])[:8]
    for seed in seeds:
        short = info[seed][2].rsplit(".", 1)[-1]
        if is_test_path(info[seed][0]):
            for callee in callees[seed]:
                lend(callee, TEST_SHARE * scores[seed], f"tested by {short}")
            continue
        for callee in callees[seed]:
            lend(callee, CALLEE_SHARE * scores[seed], f"called by {short}")
            for second in callees[callee] - {seed}:
                lend(second, SECOND_CALLEE_SHARE * scores[seed], f"called (via {info[callee][1]}) by {short}")
        for caller in callers[seed]:
            lend(caller, CALLER_SHARE * scores[seed], f"calls {short}")

    ranked = sorted((i for i in boosted if boosted[i] >= MIN_SCORE and not is_test_path(info[i][0])),
                    key=lambda i: (-boosted[i], info[i][0], info[i][4]))
    out = []
    for sym_id in ranked:
        path, name, qualname, kind, line, end_line = info[sym_id]
        if any(o.path == path and o.line <= line and end_line <= o.end_line and o.kind == "class" for o in out):
            continue  # a method of a class already listed
        if kind == "class" and any(o.path == path and line <= o.line and o.end_line <= end_line for o in out):
            continue  # one of its methods is already listed, which is more precise
        out.append(Suspect(qualname, name, kind, path, line, end_line, round(boosted[sym_id], 1),
                           reasons[sym_id] or ["shares words with the report"]))
        if len(out) >= limit:
            break
    return out


def where_to_look(graph: Any, issue: str) -> str:
    """The ranked suspects as text for the ghost's first message ('' if nothing stands out)."""
    suspects = rank(graph, issue)
    if not suspects:
        return ""
    lines = ["Code that looks most related to the issue (ranked by GhostPatch from the report and the code graph; "
             "a starting point to check, not a verdict):"]
    for n, s in enumerate(suspects, 1):
        lines.append(f"\n{n}. {s.qualname}  ({s.path}:{s.line}-{s.end_line})  why: {'; '.join(s.reasons)}")
        if n <= MAX_WITH_SOURCE:
            source = graph.symbol_source(s.qualname, max_lines=MAX_SOURCE_LINES)
            match = next((x for x in source if x["path"] == s.path and x["line"] == s.line), None)
            if match:
                lines.append(match["text"])
    text = "\n".join(lines)
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS].rsplit("\n", 1)[0] + "\n  ... (more suspects cut)"
