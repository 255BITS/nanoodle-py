# nanoodle

Run [nanoodle](https://nanoodle.io) AI workflows server-side. Build a workflow visually in the
nanoodle editor, hit 💾 to download `noodle-graph.json`, and re-execute it anywhere Python runs —
against the same [NanoGPT](https://nano-gpt.com) API the app uses.

- **Zero runtime dependencies** (Python >= 3.9, stdlib only)
- Same execution semantics as the app: topological order, concurrent lanes, wired-field overrides
- Text, image, video (submit + poll), audio (sync + async poll), vision, transcription
- Cost tracking per node and per run

## Quickstart

```bash
pip install nanoodle
export NANOGPT_API_KEY=...   # nano-gpt.com API key (or OAuth access token)
```

```python
from nanoodle import Workflow

wf = Workflow.load("noodle-graph.json")
result = wf.run({"Text": "a cozy ramen shop on a rainy night"})
result["Image"].save("ramen.png")            # media outputs are MediaRef (url + bytes()/save())
print(result.cost_usd, result.remaining_balance)
```

With the starter graph from the app (text → LLM prompt-writer → image), that's the whole program.

### Discover a workflow's interface

```python
wf.inputs    # [InputSpec(key="Text", node_id="n1", field="text", kind="textarea", ...)]
wf.outputs   # [OutputSpec(key="Image", node_id="n3", type="image", ports=["image"])]
wf.settings  # [SettingSpec(key="n3.size", kind="select", default="1k", ...)]
```

Input keys resolve flexibly (case-insensitive): the node's custom name, `nodeId.field`
(`"n2.system"`), or the input's label when unique. A workflow with exactly one required
input also accepts a bare value: `wf.run("hello")`.

### Media inputs

```python
from nanoodle import media_from_file

wf.run({"Image": media_from_file("photo.jpg")})            # local file
wf.run({"Image": "https://example.com/photo.jpg"})         # hosted or data: URL
wf.run({"Image": raw_bytes})                               # raw bytes (MIME sniffed)
```

Media is sent inline as base64 (NanoGPT has no upload endpoint); files over ~4.4 MB
(~3.5 MB for transcription) are refused locally with a clear error before any paid call.

### Settings, progress, errors

```python
result = wf.run(
    {"Text": "sunset harbor"},
    settings={"n3.model": "flux-dev", "n3.size": "1k"},
    timeout=600,
    on_progress=lambda evt: print(evt["type"], evt.get("name", "")),
)
```

`run()` raises `RunError` when an output (sink) node failed — `error.result` still carries
the partial results, per-node statuses, and cost so far. Failures in lanes no output depends
on only surface in `result.errors`. Unknown/unsupported node types, missing required inputs,
bad keys, and a missing API key all fail **before** anything is spent.

### CLI

Installed as `nanoodle-py` (and `python -m nanoodle` always works):

```bash
nanoodle-py inspect graph.json
nanoodle-py run graph.json --input Text="a cozy ramen shop" --set n3.size=1k --out ./out
nanoodle-py run graph.json --input n2.system=@style.txt --json
nanoodle-py run graph.json --env-file .env --input Text="hello"   # NANOGPT_API_KEY from a .env file
```

`--out DIR` saves media outputs to files; `--json` prints a machine-readable result;
`--env-file PATH` loads `.env`-style `KEY=VALUE` lines (existing environment variables win).

## Supported nodes

| runs | node types |
|---|---|
| local | text, upload (image/audio/video), choice, join, comment |
| NanoGPT | llm (incl. vision + audio input), image, draw, edit, inpaint*, vision, tvideo, ivideo, vedit, lipsync, music, remix, tts, transcribe |
| **not supported** (browser-only media processing) | resize, vframes, combine, soundtrack, trim, extractaudio |

Workflows containing unsupported node types load with a warning and fail fast at `run()` with
`UnsupportedNodeError` — before any network call.

\* inpaint caveat: the browser app composites the mask onto black at the source's pixel size;
this library passes your mask through verbatim, so supply a black/white mask matching the
source dimensions.

## Cost

You bring your own NanoGPT API key; NanoGPT bills your balance per generation and reports the
price on each response. `result.cost_usd` totals it and `result.cost_exact` turns `False` when
any call omitted a price (the total is then a floor). `result.remaining_balance` is the freshest
balance the API reported. A price of 0 means known-free (subscription-included), not unknown.
No telemetry, no analytics, and your API key is never logged.

## Testing

Tests run fully offline against a mock NanoGPT server (`tests/harness/`):

```bash
python -m unittest discover -s tests -t .
```

An opt-in live probe (spends a fraction of a cent) exists for hand-verification:
`python3 scripts/live-spot-check.py` (add `--image` to also run the starter graph's image step).

## Docs

Design contract and format/engine/io specs live in [`docs/`](docs/): `DESIGN.md`,
`SPEC-format.md`, `SPEC-engine.md`, `SPEC-io.md`.

## License

MIT — see [LICENSE](LICENSE). Not affiliated with NanoGPT. Build workflows at
[nanoodle.io](https://nanoodle.io).
