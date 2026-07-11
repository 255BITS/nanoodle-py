# Nanoodle Run Engine — server-side re-implementation spec

Blueprint = play.html RUNTIME_JS (the exported-app runtime). Line refs are play.html.

## Endpoints & auth
```
NANOGPT = https://nano-gpt.com
IMG_ENDPOINT   POST {NANOGPT}/v1/images/generations      (note: NOT /api/v1)
CHAT_ENDPOINT  POST {NANOGPT}/api/v1/chat/completions
VIDEO submit   POST {NANOGPT}/api/generate-video
VIDEO poll     GET  {NANOGPT}/api/video/status?requestId=<id>
AUDIO speech   POST {NANOGPT}/api/v1/audio/speech        (music + tts + remix)
AUDIO poll     GET  {NANOGPT}/api/tts/status?<qs>
TRANSCRIBE     POST {NANOGPT}/api/v1/audio/transcriptions   (multipart)
Catalogs (public, no key): /api/v1/models?detailed=true, /api/v1/image-models,
                           /api/v1/video-models, /api/v1/audio-models
```
Every JSON call: `Content-Type: application/json` + BOTH `Authorization: Bearer <key>` and `x-api-key: <key>`.
Multipart transcribe: the two auth headers, NO explicit Content-Type (let the http lib set boundary). File form field MUST be named "file"; also fields `model`, optional `language`.

All media is inlined as base64 `data:` URLs in JSON bodies (no upload endpoint). MEDIA_INLINE_MAX = 4.4MB — guard locally and raise a clear error above it. Chat runs NON-STREAMING (no `stream` key); parse `r.json()` once.

Model strings pass through VERBATIM (`body.model = node.fields.model`). Missing model → error "pick a model first". Endpoint choice is by node TYPE, never by model lookup. The catalog is optional/best-effort (capability gating + clamps); the library must run fine with no catalog fetch.

## Per-node payloads

### llm → CHAT_ENDPOINT (genChat 1481-1504, run 2574-2603)
```
body = { model, messages, temperature: 0.8 }
if maxTokens          body.max_tokens = +maxTokens
if format == "JSON"   body.response_format = { type: "json_object" }
if reasoningEffort set and != "default"  body.reasoning_effort = value
```
messages: optional {role:"system", content: fields.system} then user message.
User content = plain string prompt, UNLESS wired images (img1,img2,... sorted by index) or wired audio:
then array `[{type:"text",text:prompt}, {type:"image_url",image_url:{url:<value verbatim>}}..., audioPart?]`.
audioPart = `{type:"input_audio", input_audio:{data:<base64 body, no data: prefix>, format:"mp3"|"wav"|...}}`.
prompt = wired `prompt` port value ?? fields.prompt; error "no prompt" if empty.
Parse: `j.choices[0].message.content` (if array, join `.map(p=>p.text)`); throw "no text in response" if null/empty.
(withLocale non-English system suffix: SKIP in the library / make opt-in.)

### vision → same as llm: one user message [{text: q||"Describe this image."}, {image_url: inp.image}]

### image / edit / inpaint → IMG_ENDPOINT (genImage 1465-1480)
```
body = { model, size: fields.size || "1024x1024", n: variations||1, response_format: "b64_json" }
if prompt        body.prompt = prompt
if source image  body.imageDataUrl = <string OR array of strings (edit multi-ref)>
if mask          body.maskDataUrl = <mask data URL; white = repaint>
+ seed (when numeric), + customCivitaiAir (model "custom-civitai"), + LoRA params
```
- image node: `variations` → n (multi output = j.data list).
- edit: sources from wired `image, image2, ...` ports; single → string, multiple → array. Prompt may be empty for upscaler models — do not hard-require.
- inpaint: source+mask from ports or fields. (Browser composites mask onto black at source size via canvas; library v1: pass mask through verbatim and document the caveat.)
Parse: `j.data[]` → `d.b64_json ? "data:<sniffed mime>;base64,"+b64 : d.url`. Sniff mime from magic bytes (PNG \x89PNG, JPEG \xFF\xD8, GIF, WEBP RIFF....WEBP; default image/png). Throw "no image in response" if empty.

### draw → CHAT_ENDPOINT (genChatImage 1508-1523)
body = {model, messages, temperature:0.8} (no response_format). Wired images like llm.
Parse: images from `j.choices[0].message.images[]` → `im.image_url.url || im.url || im`; text from message.content (may be null — fine).

### tvideo / ivideo / vedit / lipsync → submit + poll (genVideo 1527-1588)
```
body = { model, prompt }
+ dims from node fields: aspect → aspect_ratio, duration → duration, resolution → resolution
  (catalog can rename e.g. aspect→orientation / duration→seconds; WITHOUT catalog use the standard names)
+ ivideo/lipsync source image → body.imageDataUrl (data: or https, verbatim)
+ ivideo wired endframe → body.last_image
+ vedit source: https URL → body.videoUrl ; local data → body.videoDataUrl
+ lipsync audio: https → body.audioUrl ; local → body.audioDataUrl
+ Object.assign(body, fields.modelOpts || {})   (per-model knobs incl. seed)
+ tvideo wired ref1.. → body.reference_images (array) — catalog may rename key; default "reference_images"
```
Submit response: `runId = j.runId || j.id`. Then poll every 5s: GET /api/video/status?requestId=<runId>.
status = (s.data?.status || s.status).toUpperCase().
COMPLETED|SUCCEEDED → url = s.data?.output?.video?.url || out.url || out.video?.[0]?.url (out = s.data?.output or s).
FAILED|ERROR|CANCELED → raise "video failed: " + (s.data?.error || status). Timeout 600s.

### music / tts / remix → AUDIO speech endpoint (genAudio 1591-1637)
```
body = { model, input: <wired text ?? fields.prompt> } + params
music params: lyrics, instrumental(bool), duration(number), negative_prompt, seed, response_format(default "mp3")
tts params:   voice, speed(omit when 1), instructions, response_format(default "mp3")
remix params: lyrics, duration, response_format + body.audio = <source: https as-is | local data URL>
+ merge fields.extraJson (parsed object) verbatim last
Omit empty params. (Catalog gating of voice/duration: skip in v1 — send what the node has.)
```
Response handling:
- content-type JSON → url = j.url||j.audioUrl||j.data?.url||j.data?.audioUrl; if none but j.runId||j.id → poll
  GET /api/tts/status?runId=..&model=..&cost=..&paymentSource=..&isApiRequest=true every 3s;
  status lowercase: completed|succeeded → s.audioUrl||s.url||s.data?.audioUrl||s.data?.url;
  error|failed|content_policy_violation → raise. Timeout 300s.
- else BINARY body → the audio bytes; mime from response content-type (pin from requested format if generic).
  Library returns bytes+mime (a data: URL or MediaRef), not an object URL.

### transcribe → multipart (1641-1666)
FormData: file=<audio blob> (field name "file"), model, language?. Local guard: >3.5MB raise.
Parse: `j.transcription ?? j.text ?? j.data?.transcription ?? j.data?.text`.

## Cost extraction (costFromJson 998-1013)
USD priority: j.cost (if >0) → j.x_nanogpt_pricing.(costUsd|cost|amount) → j.metadata?.cost → header x-cost / x-nano-cost.
Balance: header x-remaining-balance (wins) → j.remainingBalance → x_nanogpt_pricing.remainingBalance.
Present-but-zero = known-free, keep 0. Absent → cost unknown (mark total inexact).

## HTTP errors (922-935)
- 401/403 → auth error ("API key rejected").
- 402 OR body matching /insufficient|balance|funds|not enough|payment required/i → out-of-funds error.
- else → error "<status>: <body first 160 chars>".
No streaming retries needed (engine is non-streaming). Poll GET failures: silently continue the loop until timeout.

## Execution (runGraph 3000-3133)
1. Alias/filter nodes (materialize): audio→tts, drop unknown types + orphaned links, migrate music/tts inbound "text" port → "prompt".
2. Kahn topological order; cyclic → error naming the cyclic nodes.
3. Concurrency: node starts when ITS deps finish (siblings run concurrently). Library: same semantics (asyncio / Promise per node).
4. Input resolution per node: for declared ports, value = srcNode.out[from.port]. Dynamic families: img\d+ / image\d*, vid\d+, clip\d+, audio, endframe, ref\d+, frame\d+ (vframes outputs). ANY other inbound link = FIELD OVERRIDE: run with fields = {...fields, [port]: value} (that's how wired prompt/system/lyrics/q override typed values).
5. Node failure: record error for that node, continue independent lanes (library default: collect; raise at end if a sink failed — see DESIGN).
6. comment nodes never run. Fixed-seed skip-cache: optional for a library (stateless one-shot runs don't need it) — SKIP in v1.

## Local nodes to IMPLEMENT (pure logic)
- text: out.text = fields.text
- upload/aupload/vupload: out = the stored/provided data URL
- choice: options = fields.options.split("\n") non-empty trimmed; out = fields.selected if in options else first; error if no options
- join: [a,b].filter(non-empty).join(sep) where sep = fields.sep ?? " ", literal "\\n" in sep means newline
- comment: skip

## Local nodes UNSUPPORTED in v1 (browser media ops — raise UnsupportedNodeError naming node + type)
resize, vframes, combine, soundtrack, trim, extractaudio.
Error message must say: "node type 'X' does local media processing that requires the nanoodle browser app; not supported by this library yet".
