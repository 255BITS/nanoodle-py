# Nanoodle Workflow JSON Format (the downloadable "save")

Verified against /home/ntc/dev/nanoodlecom/nanoodle/index.html (editor). The 💾 Save button writes
`noodle-graph.json` = exactly `JSON.stringify(serializeGraph(), null, 2)` — no wrapper.

## Top-level shape
```json
{
  "v": 1,
  "nodes": [ { "id":"n1", "type":"text", "x":60, "y":130, "fields":{...}, "w":220, "sizes":{"system":120}, "name":"optional custom name" } ],
  "links": [ { "id":"l1", "from":{"node":"n1","port":"text"}, "to":{"node":"n2","port":"prompt"} } ],
  "nid": 4, "lid": 3,
  "view": { "panX":40, "panY":60, "scale":1 }
}
```
- `v` — always 1.
- Node: `id` (string "nN"), `type` (NODE_TYPES key), `x/y/w/sizes` layout-only (ignore), `fields` (param bag, type-specific), `name` (optional user label — the display name).
- Link: `{id, from:{node,port}, to:{node,port}}`.
- `nid/lid/view` — editor counters/camera; ignore for execution.
- ALL keys except `nodes` are optional to a loader: `d.nodes||[]`, `d.links||[]`. Accept `{nodes, links}` minimal form.

## Loader semantics (applyGraphData, index.html:8815-8860) — REPLICATE THESE
- Type alias: `audio` → `tts` (legacy).
- Unknown node type → node silently skipped (library: keep node but error only if it must run — see DESIGN; simplest faithful behavior: drop unknown-type nodes AND their links, but surface a warning).
- Links kept only if BOTH endpoints exist after node filtering.
- Migration: links into a `music`/`tts` node with `to.port === "text"` are rewritten to `"prompt"`.
- Media fields are `data:` URLs inline in `fields` (image/audio/video/mask). Share links may have them blanked to "".

## Node type registry (NODE_TYPES, index.html:5015-5679)
Port kinds: text | image | audio | video. Only matching kinds connect.
Every `<textarea>` field is also a wireable text input port with port name == field name
(EXCEPT on node types `text`, `choice`, comments, and the `extraJson` field). A wire into a
field-port overrides the typed field value at run time.

| type | title | static inputs | dynamic inputs | outputs | key fields |
|---|---|---|---|---|---|
| text | Text | — | — | text:text | text (the literal value; NOT wireable) |
| upload | Image input | — | — | image:image | image (data: URL) |
| aupload | Audio input | — | — | audio:audio | audio (data: URL) |
| vupload | Video input | — | — | video:video | video (data: URL) |
| choice | Choice | — | — | text:text | options (newline-separated), selected |
| join | Join | a:text, b:text | — | text:text | sep (default " "; literal "\n" means newline) |
| llm | LLM | — | img1..:image (vision), audio:audio | text:text | model, system, prompt, temperature, maxTokens, format(Text\|JSON), reasoningEffort, showThinking |
| image | Image | — | — | image:image | model, prompt, size, variations, seed, customCivitaiAir |
| draw | Draw | — | img1..:image | image:image, text:text | model, system, prompt, showThinking |
| edit | Edit | — | image,image2..:image | image:image | model, prompt, size, seed |
| inpaint | Inpaint | image:image, mask:image | — | image:image | model, prompt, size, seed, brush |
| resize | Resize/crop | image:image | — | image:image | mode(fit\|fill\|exact), width, height (LOCAL) |
| vision | Vision | image:image | — | text:text | model, q |
| tvideo | Text→Video | — | ref1..:image | video:video | model, prompt, duration, aspect, resolution, modelOpts |
| ivideo | Image→Video | image:image | endframe:image | video:video | model, prompt, duration, aspect, resolution, modelOpts |
| vedit | Video edit | video:video | — | video:video | model, prompt, resolution, modelOpts |
| vframes | Video→frames | video:video | — | frame1..frameN:image (dynamic, fields.frames) | frames(1-12), gap, dir(end\|start) (LOCAL) |
| combine | Combine videos | — | clip1..:video | video:video | dedup (LOCAL) |
| soundtrack | Soundtrack | video:video, audio:audio | — | video:video | loop (LOCAL) |
| lipsync | Avatar/lipsync | image:image, audio:audio | — | video:video | model, prompt, resolution, modelOpts |
| music | Music | — | — | audio:audio | model, prompt, lyrics, instrumental, duration, negative_prompt, seed, extraJson |
| remix | Remix audio | audio:audio | — | audio:audio | model, prompt, lyrics, duration, extraJson |
| tts | Speech | — | — | audio:audio | model, prompt, voice, speed, instructions, extraJson |
| trim | Trim audio | audio:audio | — | audio:audio | start, length (LOCAL) |
| extractaudio | Extract audio | video:video | — | audio:audio | start, length (LOCAL) |
| transcribe | Transcribe | audio:audio | — | text:text | model, language |
| comment | Comment | none (note:true, never runs) | — | — | text, color |

Display name resolution (play.html `displayName`): `node.name` (trimmed, if set) → NODE_TYPES[type].title → type → "?".

## Canonical fixture
/home/ntc/dev/nanoodlecom/nanoodle/noodle-graph.json — starter graph: text("a cozy ramen shop on a rainy night")
→ llm(model "zai-org/glm-5.2", system prompt-writer) → image(model "nano-banana-2-lite", size "1k", variations "1").
Wire n1.text→n2.prompt, n2.text→n3.prompt.
