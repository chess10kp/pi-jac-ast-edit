# pi-jac-ast-edit

AST-native editing tool for **Jac** (`.jac`) files in [pi](https://github.com/earendil-works/pi-coding-agent).
The Jac sibling of [`pi-ast-edit`](../pi-ast-edit) — but backed by **tree-sitter**
([tree-sitter-jac](../tree-sitter-jac)) instead of ts-morph.

## How it works

```
extensions/jac-ast-edit.ts   pi tool registration (TypeBox schema, mutation queue)
python/jac_ast_edit.py       surgical engine: parse → locate symbol → splice spans → re-parse → write
python/tree_sitter_jac/      local Python binding compiled from tree-sitter-jac's parser.c + scanner.c
```

- Locates symbols by `{target, name}` in the parse tree — no oldString, no
  whitespace/escape failures, no line-offset drift.
- Batches are **atomic**: every op resolves against the original tree, splices
  apply in one pass, and the result is **re-parsed before writing**. If the new
  source would introduce syntax errors, the batch is rejected and nothing is
  written.
- Works on files with pre-existing parse errors as long as the target symbol
  itself parses.

## Setup

The grammar binding is compiled locally (no changes to the grammar repo):

```bash
cd ~/repos/pi-jac-ast-edit
python3 -m venv .venv
.venv/bin/pip install "tree-sitter>=0.25,<0.27" setuptools
.venv/bin/pip install -e ./python
```

Install into pi:

```bash
pi install /home/jac/repos/pi-jac-ast-edit
```

## Tool: `jac_ast_edit`

Single op: `{path, action, target, name, value?, newCode?, index?}`.
Atomic batch: `{path, operations: [{...}, ...]}`.

Targets: `obj|node|edge|walker|class` (= `archetype`), `function` (module-level
def), `ability|method` (can/def; use `Card.label` for members), `has|property`
(has vars), `test`, `impl` (`Animal.speak`), `enum`, `member` (enum member),
`glob`, `type` (type_alias), `import`, `code` (free `with entry` blocks).
Duplicate names disambiguate with `index`.

| Tier | Ops |
|---|---|
| MICRO | `rename` (declaration site), `remove`, `set_initializer`, `set_return_type`, `set_type` (has/glob var), `add_parameter`, `remove_parameter` (`value` = param name), `set_extends` (archetype bases; `value` = `'Base, Mixin'` or `''` to clear) |
| BODY | `set_body`, `add_statement`, `replace_in_body` (unique anchor; ambiguous → rejected) |
| STRUCT | `add_method`, `add_property` (auto `has` prefix, lands above methods), `add_member`, `replace`, `add_function` |
| FILE | `create_file` (sole op only), `insert_text` (`value='after-imports'` or `index` 0/−1), `add_import` + `add_named_import` (both idempotent), `remove_import` (`value`=module, `newCode`=symbol for single item), `organize_imports` (merge/sort/dedupe), `add_enum`, `add_archetype` (`target`=obj\|node\|edge\|walker\|class, `value`=name), `add_glob`, `add_type_alias` (`value`=name, `newCode`=type expr), `add_impl` (`newCode`=full impl text), `add_test` (`value`=name, auto-quoted) |

Known grammar gaps (the re-parse gate rejects these — use supported forms):
bare `has x;` with neither type nor default, `impl ... for ...` root form
(write `impl Archetype.ability { }`), and `check` statements in test bodies
(use `assert`).

Body contract: `set_body`/`add_statement` take **contents only** (no braces),
written at final indentation; absolute-style indentation is auto-renested.
`add_method`/`add_property` take the **full declaration**.

Pair with the `jac` MCP tools: `jac_check_syntax` / `jac_validate_jac` for
verification, `jac_format_jac` for canonical formatting.

## Harvested from Empryo's ast_edit

Prompt suggestions and behaviors ported from `~/repos/notes/Empryo/src/core/tools/ast-edit.ts`
(+ its ts-morph backend):

- **Did-you-mean** fuzzy symbol suggestions (Damerau-Levenshtein, dot-prefix
  aware: `Card.labl` → `Did you mean: "Card.label"?`) alongside the
  available-symbols list on misses.
- **Idempotent `add_import`** — an identical import line is a no-op merge.
- **Examples embedded in the tool description** (MICRO multi-op, ANCHOR PAIR,
  ATOMIC import+method) and the CAN DO / CANNOT target / fallback-rules
  guidance.
- **Output deltas** — `lines +N`, byte counts, atomic op lists.
- **Full action parity where Jac allows it** — `set_type`, parameter ops,
  `set_extends`, named/organized import management, and FILE-level
  declaration creators (`add_enum`, `add_archetype`, `add_glob`,
  `add_type_alias`, `add_impl`, `add_test`). TS-only machinery (jsdoc,
  decorators, overloads, interfaces, exports, fix_missing_imports) has no
  Jac equivalent and is intentionally absent.

Not ported (Empryo-infra specific): undo stack, editor reload, memory hints,
clone hints, auto-format appends (use `jac_format_jac` instead), CAS
ts-morph-cache check (this engine re-reads per call; pi's mutation queue
serializes access).

## Engine CLI (debugging)

```bash
.venv/bin/python python/jac_ast_edit.py symbols path/to/file.jac
echo '{"operations":[{"action":"add_method","target":"archetype","name":"Card","newCode":"def f() -> int {\n    return 1;\n}"}]}' \
  | .venv/bin/python python/jac_ast_edit.py edit path/to/file.jac
```

## Known grammar gaps (upstream, not worked around here)

- `glob { ... }` block form does not parse (jsx_text fallback / ERROR).
- Named tests `test foo { ... }` leave the name as an ERROR node; the engine
  recovers the label heuristically but tests are best addressed by `index`.
