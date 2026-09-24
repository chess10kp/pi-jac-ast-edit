/**
 * pi extension: `jac_ast_edit` — AST-native editor for Jac (.jac) files.
 *
 * Mirrors ast_edit (TS/JS) but backed by tree-sitter: the Jac grammar
 * (~/repos/tree-sitter-jac) is compiled into a local Python binding and the
 * surgical engine lives in ../python/jac_ast_edit.py. Edits locate symbols by
 * {target, name} in the parse tree, splice byte spans against the ORIGINAL
 * tree, and re-parse the result — batches are atomic and rejected if the new
 * source would introduce syntax errors.
 */
import { withFileMutationQueue } from "@earendil-works/pi-coding-agent";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";
import { mkdir, stat, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Type } from "typebox";

const PKG_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const VENV_PYTHON = resolve(PKG_ROOT, ".venv", "bin", "python");
const ENGINE = resolve(PKG_ROOT, "python", "jac_ast_edit.py");

function isJacFile(filePath: string): boolean {
  return filePath.endsWith(".jac");
}

async function pythonBin(): Promise<string> {
  try {
    await stat(VENV_PYTHON);
    return VENV_PYTHON;
  } catch {
    throw new Error(
      `jac_ast_edit: python binding not built. Run:\n` +
        `  cd ${PKG_ROOT} && python3 -m venv .venv && .venv/bin/pip install "tree-sitter>=0.25,<0.27" setuptools && .venv/bin/pip install -e ./python`,
    );
  }
}

interface EngineResult {
  ok?: boolean;
  error?: { code: string; message: string; suggestions?: string[] };
  symbols?: unknown[];
  applied?: { op: number; action: string; ok: boolean }[];
  changed?: boolean;
  bytes?: { before: number; after: number };
  lines?: { before: number; after: number };
  errors?: { before: number; after: number };
  hasErrors?: boolean;
}

function runEngine(
  args: string[],
  stdinBody: string | null,
  timeoutMs = 30_000,
): Promise<EngineResult> {
  return new Promise((resolvePromise, rejectPromise) => {
    const child = spawn(VENV_PYTHON, [ENGINE, ...args], {
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      rejectPromise(new Error(`jac_ast_edit engine timed out after ${String(timeoutMs)}ms`));
    }, timeoutMs);
    child.stdout.on("data", (d: Buffer) => (stdout += d.toString()));
    child.stderr.on("data", (d: Buffer) => (stderr += d.toString()));
    child.on("error", (err) => {
      clearTimeout(timer);
      rejectPromise(err);
    });
    child.on("close", () => {
      clearTimeout(timer);
      try {
        resolvePromise(JSON.parse(stdout) as EngineResult);
      } catch {
        rejectPromise(
          new Error(`jac_ast_edit engine returned non-JSON output: ${stdout.slice(0, 400)} ${stderr.slice(0, 400)}`),
        );
      }
    });
    if (stdinBody === null) child.stdin.end();
    else child.stdin.write(stdinBody), child.stdin.end();
  });
}

const operationSchema = Type.Object({
  action: Type.String({
    description:
      "Operation to apply. 'symbols' = list every symbol (kind, name, line range) — run this FIRST to discover targets before editing. " +
      "MICRO: rename, remove, set_initializer, set_return_type, set_type, add_parameter, remove_parameter, set_extends. " +
      "BODY: set_body, add_statement, replace_in_body. " +
      "STRUCT: add_method, add_property (has var), add_member (autodetect), replace (whole symbol), add_function (module-level def). " +
      "FILE: insert_text (value='after-imports' | index 0|-1), add_import, add_named_import, remove_import, organize_imports, add_enum, add_archetype, add_glob, add_type_alias, add_impl, add_test.",
  }),
  target: Type.Optional(
    Type.String({
      description:
        "Symbol kind: obj|node|edge|walker|class|archetype|function|ability|method|has|property|test|impl|enum|member|glob|type|import|code.",
    }),
  ),
  name: Type.Optional(
    Type.String({
      description:
        "Symbol name. Members: 'Card.label' or bare 'label'. impl: 'Animal.speak' (impl_target text).",
    }),
  ),
  value: Type.Optional(
    Type.String({
      description:
        "Short value: new name (rename), initializer expr (set_initializer), return type (set_return_type), " +
        "anchor text (replace_in_body), import line (add_import).",
    }),
  ),
  valueEnd: Type.Optional(
    Type.String({
      description: "replace_in_body only: end anchor — replaces span [value .. valueEnd].",
    }),
  ),
  newCode: Type.Optional(
    Type.String({
      description:
        "Code. set_body/add_statement: body CONTENTS only (no braces), written at final indentation. " +
        "add_method/add_property/add_function: FULL declaration. replace: whole symbol text. " +
        "add_property: field text like 'count: int = 5' ('has ' prefix added if missing).",
    }),
  ),
  index: Type.Optional(
    Type.Number({
      description: "Disambiguates duplicate {target,name} matches; also insert_text anchor offset (0=start, -1=end).",
    }),
  ),
});

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "jac_ast_edit",
    label: "Jac AST Edit",
    description:
      "AST-native editor for Jac files (.jac) — default editor for Jac, used BEFORE write/text edits, not as a fallback. " +
      "Locates symbols by {target, name} via the tree-sitter-jac grammar: no oldString, no whitespace/escape failures, no line-offset drift. " +
      "DISCOVERY: {path, action:'symbols'} lists every symbol with kind + line range — use before batch edits to pick correct target/name/index. " +
      "Single op: {path, action, target, name, value?, newCode?, index?}. " +
      "ATOMIC multi-op: {path, operations:[{...}, ...]} — all-or-nothing; the result is re-parsed and REJECTED if it would introduce syntax errors (nothing written). Use for 'add import + use it' in one call. " +
      "Targets: obj|node|edge|walker|class (=archetype), function (module-level def), ability|method (can/def — members as 'Card.label' or bare 'label'), has|property (has vars), test, impl ('Animal.speak'), enum, member (enum member), glob, type (type_alias), import, code (free 'with entry' blocks). Duplicate names: index disambiguates. " +
      "CAN DO (no fallback): any named symbol — obj/node/edge/walker/def/can/has/test/impl/enum/glob; f-strings, JSX, Unicode/special chars/escape sequences/quotes (tree-sitter's external scanner handles them); large rewrites via replace or anchor-pair replace_in_body; whitespace drift (tab<->space, CRLF<->LF) auto-handled by span splicing. " +
      "CANNOT target: expressions inside statements, anonymous lambdas, or union-style fragments -> use replace_in_body on the enclosing NAMED symbol. Raw text inside comments/strings -> same. rename is declaration-site only — no cross-file semantic rename; update call sites with follow-up ops. " +
      "ONLY fall back to write when: non-.jac file, the edit is entirely outside any named symbol (top-of-file banner), or the file has parse errors breaking the target symbol. Long newCode / special chars are NOT fallback reasons. Known grammar gaps (the re-parse gate rejects these — use supported forms): bare 'has x;' with neither type nor default, 'impl ... for ...' root form (write 'impl Archetype.ability { }'), and 'check' statements in test bodies (use assert). " +
      "Body shape — get this wrong and the batch gets rejected by the re-parse gate: " +
      "set_body/add_statement take body CONTENTS ONLY — NO surrounding {} (passing {…} yields {{…}}). " +
      "add_method/add_property take the FULL declaration INCLUDING braces; add_property field text like 'count: int = 5' ('has ' prefix auto-added; lands above methods). " +
      "replace takes the WHOLE symbol text. " +
      "replace_in_body shapes (pick smallest): SHORT ANCHOR value=<1-2 unique lines> + newCode. ANCHOR PAIR (RANGE): value=<short start anchor> + valueEnd=<short end anchor> + newCode=<span replacement> — rewrites a 100-line block with ~20 tokens of anchors. Exact-match ambiguity (>=2 hits) THROWS — add surrounding context or use an anchor pair. " +
      "Tiers (pick smallest): MICRO (1-10 tok) rename, remove, set_initializer, set_return_type, set_type (has/glob var), add_parameter / remove_parameter (value=param name), set_extends (archetype bases, value='Base, Mixin' or '' to clear). BODY (10-100 tok) set_body, add_statement, add_property, add_method, replace_in_body. STRUCT replace, add_function. FILE-LEVEL insert_text (anchor: index=0|-1 or value='after-imports'), add_import + add_named_import (value=module, newCode=symbol — both IDEMPOTENT), remove_import (value=module, newCode=symbol to remove one item), organize_imports (merge/sort/dedupe), plus declaration creators taking newCode=BODY CONTENTS without braces: add_enum, add_archetype (target=obj|node|edge|walker|class, value=name), add_glob, add_type_alias (value=name, newCode=type expr), add_impl + add_test (value=name auto-quoted, newCode=body). " +
      "Examples — " +
      "MICRO multi-op: {path, operations:[{action:'set_initializer',target:'has',name:'Card.value',value:'52'},{action:'set_return_type',target:'function',name:'add_two',value:'float'}]}. " +
      "BODY: {path, action:'add_statement', target:'ability', name:'Deal.start', newCode:'report self.count;'}. " +
      "ANCHOR PAIR: {path, action:'replace_in_body', target:'function', name:'crawl', value:'visit [-->, -->];', valueEnd:'disengage;', newCode:'<new traversal>'}. " +
      "ATOMIC import+method: {path, operations:[{action:'add_import',value:'import from math { sqrt };'},{action:'add_method',target:'obj',name:'Vector',newCode:'def norm() -> float {\\n    return sqrt(self.x*self.x + self.y*self.y);\\n}'}]}. " +
      "Pair with jac MCP tools: jac_check_syntax/jac_validate_jac to verify, jac_format_jac to canonicalize formatting after edits.",
    promptSnippet: "AST-native Jac editing by symbol name — atomic batches, re-parse validated",
    parameters: Type.Object({
      path: Type.String({ description: "Jac file to edit (relative to cwd or absolute). Leading @ is stripped." }),
      action: Type.Optional(Type.String({ description: "Single-operation mode: the action." })),
      target: Type.Optional(Type.String()),
      name: Type.Optional(Type.String()),
      value: Type.Optional(Type.String()),
      valueEnd: Type.Optional(Type.String()),
      newCode: Type.Optional(Type.String()),
      index: Type.Optional(Type.Number()),
      operations: Type.Optional(
        Type.Array(operationSchema, {
          description: "Multi-operation atomic mode: all ops apply or none do.",
        }),
      ),
    }),

    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      const rawPath = params.path.replace(/^@/, "");
      const filePath = resolve(ctx.cwd, rawPath);

      if (!isJacFile(filePath)) {
        throw new Error(`jac_ast_edit only supports .jac files. Got: ${rawPath}`);
      }

      await pythonBin();

      let ops: Record<string, unknown>[];
      if (params.operations && params.operations.length > 0) {
        ops = params.operations as Record<string, unknown>[];
      } else if (params.action) {
        ops = [
          {
            action: params.action,
            target: params.target,
            name: params.name,
            value: params.value,
            valueEnd: params.valueEnd,
            newCode: params.newCode,
            index: params.index,
          },
        ];
      } else {
        throw new Error(
          "Provide action (+ target/name/value/newCode) for a single operation, " +
            "or an operations array for multiple atomic operations.",
        );
      }

      return withFileMutationQueue(filePath, async () => {
        // Fast path: create_file must be the sole operation
        if (ops.length === 1 && ops[0]?.action === "create_file") {
          const content = (ops[0].newCode as string) ?? "";
          let exists = false;
          try {
            await stat(filePath);
            exists = true;
          } catch {
            exists = false;
          }
          if (exists) {
            throw new Error(`File already exists: ${rawPath}. Use a non-create_file action to modify it.`);
          }
          await mkdir(dirname(filePath), { recursive: true });
          await writeFile(filePath, content, "utf-8");
          return {
            content: [
              { type: "text", text: `Created ${rawPath} (${String(content.split("\n").length)} lines)` },
            ],
            details: { created: true, path: filePath },
            isError: false,
          } as const;
        }

        let result: EngineResult;
        if (params.action === "symbols" || (ops.length === 1 && ops[0]?.action === "symbols")) {
          result = await runEngine(["symbols", filePath], null);
          if (result.error) throw new Error(`${result.error.code}: ${result.error.message}`);
          const syms = (result.symbols ?? []) as {
            kind: string; name: string | null; qualified: string | null;
            startLine: number; endLine: number;
          }[];
          const lines = syms.map(
            (s) =>
              `${s.kind}${s.name ? " " + s.qualified : ""} L${String(s.startLine)}-${String(s.endLine)}`,
          );
          return {
            content: [{ type: "text", text: lines.join("\n") || "<no symbols>" }],
            details: { symbols: result.symbols },
            isError: false,
          } as const;
        }

        result = await runEngine(["edit", filePath], JSON.stringify({ operations: ops }));
        if (result.error) {
          const sug = result.error.suggestions?.length
            ? "\n" + result.error.suggestions.join("\n")
            : "";
          throw new Error(`${result.error.code}: ${result.error.message}${sug}`);
        }

        const applied = result.applied ?? [];
        let output: string;
        if (applied.length === 1) {
          output = `${applied[0].action} ok`;
        } else {
          output = `${String(applied.length)} ops (atomic):\n${applied.map((a) => `  • ${a.action}`).join("\n")}`;
        }
        if (result.changed) {
          const dl = (result.lines?.after ?? 0) - (result.lines?.before ?? 0);
          const deltas = [
            dl !== 0 ? `lines ${dl > 0 ? "+" : ""}${String(dl)}` : "",
            `bytes ${String(result.bytes?.before)}→${String(result.bytes?.after)}`,
          ].filter(Boolean);
          output += ` (${deltas.join(", ")})`;
        }
        if (result.errors && result.errors.after > 0) {
          output += ` [note: file has ${String(result.errors.after)} pre-existing parse error(s)]`;
        }

        return {
          content: [{ type: "text", text: output }],
          details: { path: filePath, applied, bytes: result.bytes },
          isError: false,
        } as const;
      });
    },
  });
}
