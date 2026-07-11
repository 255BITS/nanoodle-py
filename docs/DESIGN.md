# nanoodle executor libraries — DESIGN (binding decisions)

Two sibling repos, both MIT, both ZERO runtime dependencies:
- /home/ntc/dev/nanoodle-js  — npm package `nanoodle` (ESM, Node >= 20, built-in fetch, node:test)
- /home/ntc/dev/nanoodle-py  — PyPI package `nanoodle` (Python >= 3.9, stdlib urllib only, unittest)

Read SPEC-format.md, SPEC-engine.md, SPEC-io.md in this directory first. They are the contract.

## Public API — JS
```js
import { Workflow, NanoodleError, UnsupportedNodeError, RunError } from "nanoodle";

const wf = await Workflow.load("noodle-graph.json", { apiKey });   // path, or pass a parsed object / JSON string to Workflow.fromJSON(objOrString, opts)
wf.inputs    // [{ key, nodeId, field, kind, label, optional, def, options? }]  key = resolved friendly name
wf.outputs   // [{ key, nodeId, type, ports }]
wf.settings  // [{ key, nodeId, field, kind, def, options? }]

const result = await wf.run({ "Text": "a cozy ramen shop" }, { settings: { "n3.model": "..." }, timeoutMs, signal, onProgress });
result.get("Image")          // primary output value of that sink node
result.outputs               // { [key]: value } (plus node-id keys)
result.costUsd, result.costExact, result.remainingBalance
result.nodes                 // per-node { status, out, error, costUsd, ms }
result.errors                // [] of { nodeId, name, message }
```
- run() REJECTS with RunError when any SINK node failed (RunError carries .result with partials). Non-sink failure that no sink depends on → warning in result.errors only. (A failed non-sink makes its downstream sinks fail with "upstream failed: <name>" — so effectively any failure that matters rejects.)
- Media values: class MediaRef { url (data: or https), mime?, async bytes(), async save(path), toString() → url }. Text outputs are plain strings. Inputs accept: string (text), data: URL, https URL, Buffer/Uint8Array (+ mime option via {data, mime}), or local file path via Workflow helpers (mediaFromFile(path)).
- onProgress(evt): { type: "node-start"|"node-done"|"node-error"|"poll", nodeId, name, ... }.
- Constructor opts: { apiKey = process.env.NANOGPT_API_KEY, baseUrl = "https://nano-gpt.com", fetch = globalThis.fetch, pollIntervals, timeouts } — injectable fetch/baseUrl is what the test harness uses.

CLI (bin/nanoodle.mjs, "nanoodle" bin entry):
```
nanoodle run graph.json --input Text="a cozy ramen shop" --input n2.system=@file.txt --set n3.size=1k --out ./out [--json]
nanoodle inspect graph.json      # prints inputs/outputs/settings + node table
```
--out saves media outputs to files (fetch https, decode data:), prints text outputs; --json prints machine-readable result.

## Public API — Python (mirror, pythonic)
```python
from nanoodle import Workflow, NanoodleError, UnsupportedNodeError, RunError, media_from_file

wf = Workflow.load("noodle-graph.json", api_key=None)   # env NANOGPT_API_KEY fallback; Workflow.from_dict(d) too
wf.inputs / wf.outputs / wf.settings                     # lists of dataclasses, same fields as JS
result = wf.run({"Text": "a cozy ramen shop"}, settings=None, timeout=None, on_progress=None)
result["Image"]              # __getitem__ = outputs lookup (friendly key or node id)
result.outputs, result.cost_usd, result.cost_exact, result.remaining_balance, result.nodes, result.errors
```
- Sync API (urllib + concurrent.futures ThreadPoolExecutor for node concurrency). Same RunError semantics.
- MediaRef: .url, .mime, .bytes(), .save(path), __str__ → url.
- Injectable transport: Workflow(..., base_url=..., http=callable) for the harness (default small urllib wrapper).
- CLI: `python -m nanoodle run|inspect ...` mirroring the JS flags.

## Shared behaviors (both)
- run() input validation UPFRONT: unknown input key → error listing valid keys; missing required input with empty field default → error naming it; no API key while graph has network nodes → error BEFORE any node runs.
- Unsupported node types (resize/vframes/combine/soundtrack/trim/extractaudio) and unknown types that must RUN → UnsupportedNodeError at run start (fail fast, before spending), naming node + type. Workflow.load only warns.
- No locale suffix, no catalog fetch, no seed skip-cache, no telemetry/analytics of ANY kind. Never log the API key. Media over 4.4MB inline → clear local error.
- Version: 0.1.0.

## Repo layout (each)
README.md (quickstart: download the save from https://nanoodle.io → 3-line usage; the starter-graph example; supported node matrix; cost note; link to nanoodle app)
LICENSE (MIT, "Copyright (c) 2026 nanoodle contributors")
src/... (js: src/*.mjs re-exported from src/index.mjs; py: src/nanoodle/*.py)
tests/ (harness + unit tests; node:test / unittest — NO third-party test deps)
tests/fixtures/*.json (starter graph copy + purpose-built graphs: join/choice chain, llm-vision, edit multi-image, video poll, tts binary, music async-poll, transcribe, unsupported-node, cycle, field-override, duplicate-names, unknown-key errors)
tests/harness/ mock NanoGPT server (js: node:http; py: http.server in a thread) — canned per-endpoint responses, records every request (method/path/headers/body) for payload assertions, scriptable sequences (video pending→pending→completed; audio runId→poll; 401/402 errors; binary audio response with x-cost header).
scripts/live-spot-check.{mjs,py} — OPT-IN live test: reads NANOGPT_API_KEY from env or --env-file, runs fixture text→llm (model zai-org/glm-5.2, maxTokens 60) and prints text + cost; --image flag additionally runs the starter graph image step. NEVER run by CI.
.github/workflows/test.yml — offline unit tests only, on push.
.gitignore (node_modules, dist, __pycache__, .env, out/)

## Test plan (harness-first; the workflow phase will extend it)
Cover at minimum: graph load/aliases/link-migration/unknown-type; topo order + cycle error; concurrency (two parallel lanes both hit server); wiring incl. field override via textarea port; input derivation + all key-resolution orders + ambiguity errors + bare-scalar single-input; settings override + wired-field-refused; every network node type's payload EXACTLY (assert recorded body against spec) + response parse incl. b64 mime sniff, draw message.images, video poll loop + failure + timeout, audio JSON-url/JSON-runId-poll/binary branches, transcribe multipart (assert field name "file"); cost extraction priority incl. zero-cost-kept and header fallback; 401/402/500 error mapping; RunError partial results; MediaRef bytes/save; unsupported node fail-fast BEFORE any network call; no Authorization header leak in errors/repr.
