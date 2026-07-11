# Nanoodle Workflow Public Interface (inputs / outputs / naming)

## Inputs — deriveInputs (play.html 3140-3206)
An input = an INPUT_SPECS field on a node that is NOT fed by a wire (`links.some(l => l.to.node===id && l.to.port===field)` → hidden).

INPUT_SPECS table (only these node types contribute; kind in parens):
- text: text (textarea)
- upload: image (image) · aupload: audio (audio) · vupload: video (video)
- llm: prompt (textarea, required); system (textarea, OPTIONAL, def "You are a helpful, concise assistant.")
- image: prompt · tvideo: prompt · music: prompt · remix: prompt · tts: prompt (all textarea, required)
- draw: prompt (required); system (optional)
- inpaint (special): prompt ("What to paint in"); image and/or mask when not wired
- choice (special): field `selected`, kind choice, options from fields.options newline-split

Required unless marked optional. `def` = prefilled default.

## Input NAMING (for run({name: value}) resolution)
- Node display name = node.name (trimmed) → NODE_TYPES[type].title → type.
- The app labels an input with its generic spec label ("Image prompt", "Text", ...), EXCEPT:
  when a node contributes exactly ONE required input AND has a custom name, that custom name is the label (PR #138).
- upload nodes feeding role ports get role labels ("End frame", "Reference N", "Image N") — UI nicety; library can skip.
- Unique key = (nodeId, field). LIBRARY RESOLUTION ORDER for a user-supplied key (case-insensitive, trimmed):
  1. exact node custom name (if that node has exactly one derived input → that input; ambiguous → error listing candidates)
  2. "nodeId.field" (e.g. "n2.prompt") and bare nodeId (if single input on the node)
  3. the input's label / field name if unique across inputs
  Unknown key → error listing available input names.
- If the workflow has exactly one required input, allow a bare scalar: run("hello").

## Settings — deriveSettings (play.html 3229-3328)
Per-node knobs (model, size, temperature, duration, voice, seed, ...) that are NOT part of IO shape.
Library: expose overrides via run(..., settings={...}) resolved the same way (name/nodeId.field), applied as
node.fields[field] = value before execution. Same wired-hides rule (cannot override a wired field via settings).

## Outputs — deriveOutputs (play.html 3208-3214)
Output nodes = nodes with a non-empty outputs list AND no outgoing link (sinks). No explicit marker.
Result value = node.out[NODE_TYPES[type].outputs[0].name] (primary port). draw also has secondary text; expose all ports.
Result keying: displayName(node) (custom name → type title); on duplicate display names, suffix " 2", " 3" in topo order; always ALSO keyed by node id.
Intermediate nodes still run; expose them under result.nodes / result.steps for debugging.

## Key & auth
- One key: NanoGPT API key (or OAuth access token — identical usage).
- Headers on every call: Authorization: Bearer <key> AND x-api-key: <key>.
- Library: constructor arg api_key, fallback env NANOGPT_API_KEY. No key + graph has network nodes → clear upfront error.
- Balance: /api/check-balance POST {} returns {usd_balance} with ACAO * (works server-side fine). Optional helper.
- Whole-graph runs only (like exported apps). No subset/runGroup in v1.
