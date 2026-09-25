<div align="center">

```
   .-.
  (o o)
  | O \
   \   \
    `~~~'
```

# GhostPatch

**Your bugs, fixed while you sleep.**

An open-source AI software engineer that **understands your codebase's structure**.
Describe a bug, and GhostPatch maps your code into a live graph, finds the problem,
fixes it, checks what else the change affects, and verifies it with your tests.

Runs on free models.

</div>

---

## Quickstart

```bash
git clone https://github.com/Nitin23123/GhostPatch
cd GhostPatch
pip install -e .

# Add a free API key (Groq: https://console.groq.com/keys)
cp .env.example .env        # set GHOSTPATCH_PROVIDER=groq and paste the key into GROQ_API_KEY

# Watch it fix a bug that spans several files
cd examples/buggy-shop
ghostpatch fix @ISSUE.md
```

Or watch it work in your browser:

```bash
ghostpatch serve            # opens http://localhost:8765
```

Or fix a bug in your own project:

```bash
cd path/to/your/repo
ghostpatch fix "Login fails when the email contains a plus sign"
```

## How it works

```
 bug report
     │
     ▼
 ┌─────────┐   🔎 explore   list, search and read the code
 │  model  │   ⚡ reproduce  run the tests / the program
 │ (brain) │◄─► ✏️  fix       make the smallest correct change
 └─────────┘   ⚡ verify     run the tests again
     │
     ▼
 🏁 summary + changed files
```

GhostPatch runs an **agent loop**. On each step the model looks at everything it has
learned so far and picks a tool to call. GhostPatch runs that tool on your machine and
sends the result back. This repeats until the model calls `finish`.

The model never touches your computer directly:

- File access is locked to the repository folder.
- Every shell command asks for your approval (unless you pass `--yes`).
- Nothing is committed or pushed. You review the changes with `git diff`.

## The code graph 🕸

Most coding agents explore a repository by searching text. GhostPatch first parses your
code into a **graph** of files, classes, functions and who-calls-what, stored in
`.ghostpatch/graph.db` and updated incrementally as files change. The agent gets:

- **A map of the repository** at the start of every run
- **Graph tools**: `find_symbol`, `find_callers`, `find_callees`, `related_tests`, `impact_of_change`
- **Automatic impact reports**: whenever the agent edits a function, GhostPatch tells it
  everything that depends on it and which tests to run, even if the model forgot to ask

You can query the graph yourself too:

```text
$ ghostpatch graph impact apply_discount
Changing 'apply_discount' may affect:
  direct callers:
    shop/cart.py:15  in shop.cart.Cart.total
    shop/invoice.py:11  in shop.invoice.bulk_price
  callers of those:
    shop/checkout.py:7  in shop.checkout.checkout
  tests to run: tests/test_cart.py::test_checkout_receipt, tests/test_cart.py::test_total_without_coupon_adds_tax
```

| Command | Shows |
|---|---|
| `ghostpatch graph map` | Every file's classes, functions and signatures |
| `ghostpatch graph stats` | Files, symbols and calls indexed |
| `ghostpatch graph symbol NAME` | Where something is defined |
| `ghostpatch graph callers NAME` | Everything that calls it |
| `ghostpatch graph callees NAME` | Everything it calls |
| `ghostpatch graph tests NAME` | Tests that exercise it, even indirectly |
| `ghostpatch graph impact NAME` | What could break if it changes |

The graph understands **Python, JavaScript and TypeScript** (`.py`, `.js`, `.jsx`, `.mjs`, `.cjs`,
`.ts`, `.tsx`). Python is parsed with the standard library; JS/TS with
[tree-sitter](https://tree-sitter.github.io/). Test blocks such as `test("adds tax", () => ...)`
become named symbols, so `related_tests` and `impact_of_change` can tell you exactly which JS
tests to run. `node_modules`, build output and minified files are skipped.

Calls are matched by name, so results can include unrelated functions that share a name.

## The dashboard 👻

`ghostpatch serve` opens a live dashboard in your browser:

- **Describe the bug** (or load the repo's `ISSUE.md`) and press **Fix it**
- **Watch every step** as it happens: files read, searches, edits (as mini diffs), test runs
- **See the code graph light up**: functions glow as the ghost reads them, the edited
  function turns orange, and everything the edit could affect is outlined in red
- **Approve or deny commands** with a click, or start it with `--yes` inside a sandbox
- **Review the final diff** of every changed file

It runs entirely on your machine using only Python's standard library: it listens on
`127.0.0.1` only and rejects cross-site requests. Use `--port` to change the port and
`--no-browser` to skip opening a tab.

## Models: free by default

| Provider | Cost | Key | Default model |
|---|---|---|---|
| `gemini` (default) | Free tier | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | `gemini-3.8-flash` |
| `groq` | Free tier | [console.groq.com/keys](https://console.groq.com/keys) | `qwen/qwen3.8-27b` |
| `ollama` | Free, runs on your PC | none ([install Ollama](https://ollama.com/download)) | `qwen2.5-coder:7b` |
| `openai` | Paid | [platform.openai.com](https://platform.openai.com/api-keys) | `gpt-5-mini` |

Choose with `--provider` or `GHOSTPATCH_PROVIDER` in `.env`. Free tiers limit requests per
minute; GhostPatch waits and retries automatically when it hits that limit.

## Options

| Flag | Meaning |
|---|---|
| `--repo PATH` | Repository to work in (default: current folder) |
| `--provider NAME` | `gemini`, `groq`, `ollama` or `openai` (default: `gemini`) |
| `--model NAME` | Model name (default: `$GHOSTPATCH_MODEL` or the provider's default) |
| `--max-steps N` | Stop after N steps (default: 30) |
| `--yes` | Run commands without asking. **Only use this inside a sandbox.** |
| `--no-graph` | Don't build or use the code graph |

## Examples

| Folder | Bug |
|---|---|
| `examples/buggy-calculator` | `average()` returns the wrong result (one file) |
| `examples/buggy-shop` | A 10% coupon makes checkout charge $0.00 (bug spans several files) |
| `examples/buggy-store-ts` | TypeScript: buying 3 mugs only charges for 1 (tests run with `node --test`) |

## Roadmap

- [x] **Phase 1:** CLI agent that explores, fixes and verifies bugs
- [ ] **Phase 2:** Docker sandbox, so commands can run safely without approval (postponed)
- [x] **Phase 3:** Code graph: the agent sees callers, callees, tests and change impact
- [x] Code graph for JavaScript and TypeScript (tree-sitter)
- [ ] Code graph for more languages (Go, Rust, Java, ...)
- [ ] **Phase 4:** GitHub integration: issue in, pull request out
- [ ] **Phase 5:** SWE-bench evaluation with public scores
- [x] **Phase 6:** Web dashboard to watch the agent work live
- [x] Free model providers (Gemini, Groq, Ollama) alongside OpenAI
- [ ] Anthropic Claude support

## Development

```bash
pip install -e ".[dev]"
pytest
```

The tests use a scripted fake model, so they need no API key and cost nothing.

## Contributing

Contributions are very welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
