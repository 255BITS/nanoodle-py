"""Node execution engine — server-side re-implementation of play.html RUNTIME_JS.

Endpoints, payloads, polling and error mapping follow SPEC-engine.md verbatim.
The catalog is never fetched: model ids pass through as typed, endpoint choice
is by node TYPE only.
"""

import json
import re
import time
import urllib.parse

from .errors import NanoodleError
from .graph import EDIT_IMG_RE, IMG_PORT_RE, REF_PORT_RE, display_name
from .media import (MEDIA_INLINE_MAX, TRANSCRIBE_MAX_BYTES, MediaRef,
                    b64_image_mime, make_data_url, parse_data_url)
from .transport import HttpResponse, encode_multipart
from .x402 import assert_payment_option, looks_like_result, parse_json, parse_nano_invoice

IMG_ENDPOINT = "/v1/images/generations"          # note: NOT /api/v1
CHAT_ENDPOINT = "/api/v1/chat/completions"
VIDEO_SUBMIT = "/api/generate-video"
VIDEO_STATUS = "/api/video/status"
AUDIO_ENDPOINT = "/api/v1/audio/speech"
AUDIO_STATUS = "/api/tts/status"
TRANSCRIBE_ENDPOINT = "/api/v1/audio/transcriptions"

_FUNDS_RE = re.compile(r"insufficient|balance|funds|not enough|payment required", re.I)
_AUDIO_MIME = {"mp3": "audio/mpeg", "opus": "audio/ogg", "aac": "audio/aac",
               "flac": "audio/flac", "wav": "audio/wav", "pcm": "audio/wav"}
_SONG_COUNT_RE = re.compile(
    r"^(number_of_songs|n|num_songs|song_count|generation_count|generation_count_parameter)$", re.I)
_SONG_COUNT_LOOSE_RE = re.compile(r"generation_count|num_?songs|song_?count", re.I)


def _as_url(value):
    """Accept str or MediaRef for a media value; return the URL string."""
    if isinstance(value, MediaRef):
        return value.url
    return value


class NodeCancelled(NanoodleError):
    pass


class Engine(object):
    def __init__(self, api_key, base_url, http, poll_intervals=None, timeouts=None,
                 on_progress=None, payment=None):
        self._api_key = api_key
        assert_payment_option(payment)  # a callback or nothing — never a seed/private key
        self._payment = payment
        self.base_url = (base_url or "https://nano-gpt.com").rstrip("/")
        self.http = http
        pi = poll_intervals or {}
        to = timeouts or {}
        self.poll_video = pi.get("video", 5.0)
        self.poll_audio = pi.get("audio", 3.0)
        self.poll_x402 = pi.get("x402", 3.0)
        self.timeout_video = to.get("video", 600.0)
        self.timeout_audio = to.get("audio", 300.0)
        self.http_timeout = to.get("http", 120.0)
        self.on_progress = on_progress
        # Set by Workflow._execute when a run-level timeout is active so local
        # media / long poll loops can stop promptly.
        self._deadline = None          # time.monotonic() absolute, or None
        self._timeout_secs = None      # original timeout for error messages

    def set_run_deadline(self, deadline, timeout_secs=None):
        self._deadline = deadline
        self._timeout_secs = timeout_secs

    def check_cancel(self):
        """Raise if the workflow deadline has passed. Safe no-op when no deadline."""
        if self._deadline is not None and time.monotonic() > self._deadline:
            msg = ("run timed out after %ss" % self._timeout_secs
                   if self._timeout_secs is not None else "run cancelled")
            raise NodeCancelled(msg)

    def local_fetcher(self):
        """Adapter for local_media: callable(url) -> bytes (drops content-type)."""
        def fetch(url):
            data, _ctype = self.fetch_media(url)
            return data
        return fetch

    def _local_opts(self):
        """Shared kwargs for local_media ops (fetcher + cancel/deadline)."""
        return {
            "fetcher": self.local_fetcher(),
            "cancel_check": self.check_cancel,
            "deadline": self._deadline,
        }

    # ---- transport helpers -------------------------------------------------

    def _auth_headers(self):
        # Every keyed JSON call carries BOTH headers. The key must never be
        # logged or echoed into error messages. Keyless with a payment
        # callback opts into accountless x402 invoices instead.
        if self._api_key:
            return {"Authorization": "Bearer " + self._api_key,
                    "x-api-key": self._api_key}
        if self._payment is not None:
            return {"x-x402": "true"}
        return {"Authorization": "Bearer ", "x-api-key": ""}

    def _paid_send(self, method, url, headers, body):
        """Send a request, settling HTTP 402 via x402 when running keyless with a
        payment callback: parse the Nano invoice → callback sends XNO (its own
        wallet/signer — this library never touches funds) → poll the complete
        URL until the deposit is seen → return the replayed result, or re-send
        the original request stamped with the settled payment id. Each API call
        pays at most once; a second 402 after settling is an error, never a
        second send."""
        resp = self.http(method, url, headers=headers, body=body, timeout=self.http_timeout)
        if resp.status != 402 or self._payment is None or self._api_key:
            return resp
        settled = self._settle_402(resp)
        if settled.get("response") is not None:
            return settled["response"]  # complete replayed the stored request
        retry_headers = dict(headers)
        retry_headers["x-x402-payment-id"] = settled["paymentId"]
        resp2 = self.http(method, url, headers=retry_headers, body=body, timeout=self.http_timeout)
        if resp2.status == 402:
            raise NanoodleError(
                "payment %s settled, but the API still answered 402 on retry — check %s "
                "before paying again" % (settled["paymentId"], settled.get("statusUrl") or "the payment status"))
        return resp2

    def _settle_402(self, resp):
        body = parse_json(resp.text())
        invoice = parse_nano_invoice(body, self.base_url) if body else None
        if not invoice or not invoice.get("paymentId") or not invoice.get("completeUrl"):
            raise NanoodleError(
                "payment required, but the 402 response offered no usable Nano option"
                + (" — " + resp.text()[:200] if body else ""))
        self._payment(invoice)  # ← the callback does the actual XNO send
        # The complete endpoint doubles as the poll: 402 = not seen on-chain yet.
        deadline = (invoice["expiresAt"] / 1000.0) if invoice.get("expiresAt") else time.time() + 15 * 60
        while True:
            cr = self.http("POST", invoice["completeUrl"],
                           headers={"Content-Type": "application/json", "x-x402": "true"},
                           body="{}", timeout=self.http_timeout)
            if 200 <= cr.status < 300:
                cj = parse_json(cr.text()) if "json" in (cr.header("content-type") or "") else None
                if looks_like_result(cj):
                    # re-wrap so call sites keep their HttpResponse contract
                    replay = HttpResponse(200, cr.headers, json.dumps(cj).encode("utf-8"))
                    return {"paymentId": invoice["paymentId"], "statusUrl": invoice.get("statusUrl"),
                            "response": replay}
                return {"paymentId": invoice["paymentId"], "statusUrl": invoice.get("statusUrl")}
            if cr.status != 402:
                self._raise_http(cr)
            if time.time() >= deadline:
                raise NanoodleError(
                    "payment window expired before the Nano deposit was detected (payment %s, %s to %s) "
                    "— if you already sent it, check %s"
                    % (invoice["paymentId"], invoice.get("amount") or invoice.get("amountRaw"),
                       invoice.get("payTo"), invoice.get("explorerUrl") or invoice.get("statusUrl")))
            time.sleep(self.poll_x402)

    def _raise_http(self, resp):
        body = resp.text()
        if resp.status in (401, 403):
            raise NanoodleError("API key rejected (HTTP %d) — sign in at nano-gpt.com and use a valid key"
                                % resp.status)
        if resp.status == 402 or _FUNDS_RE.search(body or ""):
            raise NanoodleError("out of balance — this run needs more credit. "
                                "Top up at nano-gpt.com, then run again.")
        raise NanoodleError("%d: %s" % (resp.status, (body or "")[:160]))

    def _post_json(self, path, body):
        payload = json.dumps(body)
        if len(payload) > MEDIA_INLINE_MAX:
            raise NanoodleError(
                "request media is too large (~4 MB inline limit) — nanoodle sends media "
                "inline rather than uploading it; use smaller/shorter media")
        headers = self._auth_headers()
        headers["Content-Type"] = "application/json"
        resp = self._paid_send("POST", self.base_url + path, headers, payload)
        if not (200 <= resp.status < 300):
            self._raise_http(resp)
        return resp

    def _get(self, path):
        return self.http("GET", self.base_url + path, headers=self._auth_headers(),
                         timeout=self.http_timeout)

    def fetch_media(self, url):
        """Download bytes of an https media URL (no auth headers — provider CDNs)."""
        resp = self.http("GET", url, headers={}, timeout=self.http_timeout)
        if not (200 <= resp.status < 300):
            raise NanoodleError("could not download media (%d)" % resp.status)
        return resp.body, resp.header("content-type") or ""

    def _media_ref(self, url, mime=None):
        return MediaRef(url, mime=mime, fetcher=self.fetch_media)

    # ---- cost extraction (play.html costFromJson / costFromHeaders) --------

    @staticmethod
    def _cost_from_json(j):
        if not isinstance(j, dict):
            return None, None
        usd = None
        p = j.get("x_nanogpt_pricing") or None
        p_usd = None
        if isinstance(p, dict):
            for k in ("costUsd", "cost", "amount"):
                if p.get(k) is not None:
                    p_usd = p[k]
                    break
        m_usd = (j.get("metadata") or {}).get("cost") if isinstance(j.get("metadata"), dict) else None
        jc = j.get("cost")
        if isinstance(jc, (int, float)) and not isinstance(jc, bool) and jc > 0:
            usd = float(jc)
        elif p_usd is not None and _is_num(p_usd):
            usd = float(p_usd)
        elif m_usd is not None and _is_num(m_usd):
            usd = float(m_usd)
        elif isinstance(jc, (int, float)) and not isinstance(jc, bool):
            usd = float(jc)  # present-but-zero = known-free, keep 0
        balance = None
        if isinstance(j.get("remainingBalance"), (int, float)) and not isinstance(j.get("remainingBalance"), bool):
            balance = float(j["remainingBalance"])
        elif isinstance(p, dict) and isinstance(p.get("remainingBalance"), (int, float)):
            balance = float(p["remainingBalance"])
        return usd, balance

    @staticmethod
    def _cost_from_headers(resp):
        def num(name):
            v = resp.header(name)
            try:
                return float(v) if v not in (None, "") else None
            except ValueError:
                return None
        return (num("x-cost") if num("x-cost") is not None else num("x-nano-cost"),
                num("x-remaining-balance"))

    def _cost_with_headers(self, j, resp):
        usd, balance = self._cost_from_json(j)
        h_usd, h_balance = self._cost_from_headers(resp)
        if h_balance is not None:
            balance = h_balance  # header wins
        if usd is None and h_usd is not None:
            usd = h_usd
        return usd, balance

    # ---- progress ----------------------------------------------------------

    def _progress(self, evt):
        if self.on_progress:
            try:
                self.on_progress(evt)
            except Exception:
                pass  # a broken progress callback must never kill the run

    # ---- node dispatch -----------------------------------------------------

    def run_node(self, node, inp, on_cost):
        """Execute one node. ``inp`` = declared/dynamic port values; field
        overrides are already merged into node.fields by the workflow layer.
        Returns the node's out dict (primary output under its port name)."""
        fn = _EXECUTORS.get(node.type)
        if fn is None:
            raise NanoodleError("node type %r cannot be executed" % node.type)
        return fn(self, node, inp, on_cost)


def _is_num(v):
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _mdl(node):
    m = node.fields.get("model")
    if not m:
        raise NanoodleError("pick a model first")
    return m


def _fstr(node, field, default=""):
    v = node.fields.get(field)
    return default if v is None else str(v)


def _prompt_of(node, required=True, err="no prompt"):
    p = _fstr(node, "prompt").strip()
    if required and not p:
        raise NanoodleError(err)
    return p


def _sel_index(node, count):
    """Baked gallery pick: clamp(parseInt(fields.sel)||0, 0, count-1) (play.html)."""
    try:
        sel = int(float(node.fields.get("sel")))
    except (TypeError, ValueError):
        sel = 0
    return min(max(0, sel), count - 1)


# ---- LoRA params (play.html normalizeLoraUrl / loraFamily / loraParams) ------

def _normalize_lora_url(raw):
    u = str(raw or "").strip()
    if not u:
        return ""
    if re.search(r"\b(civitai\.com|civitai\.red|civit\.ai)\b", u, re.I):
        raise NanoodleError("CivitAI links can't be fetched directly — download the "
                            ".safetensors and re-host it (e.g. on HuggingFace), then paste that URL.")
    if re.search(r"(^|//|\.)huggingface\.co/", u, re.I):
        u = u.replace("/blob/", "/resolve/")
        if not re.search(r"/resolve/.+\.safetensors(\?|$)", u, re.I):
            raise NanoodleError("Link the .safetensors file on HuggingFace: open it and use "
                                "Copy download link (…/resolve/main/your-lora.safetensors).")
        return u
    if re.match(r"^[\w.-]+/[\w.-]+$", u):
        raise NanoodleError("That looks like a HuggingFace repo id — open the .safetensors file "
                            "and copy its download link (…/resolve/main/your-lora.safetensors).")
    if not re.match(r"^https?://", u, re.I):
        raise NanoodleError("LoRA must be a direct https URL to a .safetensors file "
                            "(HuggingFace or any host).")
    return u


def _lora_family(model):
    m = str(model or "")
    if re.search(r"spicy", m, re.I):
        return None
    if re.search(r"p-image", m, re.I):
        return "pimage"
    if re.search(r"klein", m, re.I):
        return "flux2klein"
    if re.search(r"flux-2", m, re.I):
        return "flux2dev"
    if re.search(r"z-image", m, re.I):
        return "zimage"
    if re.search(r"ltx", m, re.I):
        return "ltx"
    if re.search(r"lora", m, re.I):
        return "flux"
    return None


def _image_takes_lora(model_id):
    mid = str(model_id or "")
    if re.search(r"inpaint", mid, re.I):
        return False
    if re.search(r"klein", mid, re.I):
        return True
    return re.search(r"(^|[-/])lora($|[-/])", mid, re.I) is not None


def _model_takes_lora(kind, model_id):
    if not model_id or _lora_family(model_id) is None:
        return False
    return True if kind == "video" else _image_takes_lora(model_id)


def _lora_cap(model):
    fam = _lora_family(model)
    if fam == "flux2dev":
        return 4
    if fam in ("flux2klein", "zimage", "ltx"):
        return 3
    return 1  # flux-lora, pimage — single slot


def _node_loras(node):
    f = node.fields
    if isinstance(f.get("loras"), list):
        return f["loras"]
    if str(f.get("loraUrl") or "").strip() or (f.get("loraStrength") or "") != "":
        return [{"url": f.get("loraUrl") or "", "strength": f.get("loraStrength") or ""}]
    return []


def _lora_scale(strength):
    if strength is None or strength == "":
        return 1
    try:
        n = float(strength)
    except (TypeError, ValueError):
        return 1  # play.html: isNaN → 1
    return int(n) if n == int(n) else n


def _lora_body_for(model, items):
    fam = _lora_family(model)
    if fam == "pimage":
        return {"lora_weights": items[0]["url"], "lora_scale": items[0]["scale"]}
    if fam in ("flux2dev", "flux2klein", "zimage", "ltx"):
        body = {}
        for i, it in enumerate(items):
            body["lora_url_%d" % (i + 1)] = it["url"]
            body["lora_scale_%d" % (i + 1)] = it["scale"]
        return body
    if len(items) == 1:
        return {"lora_url": items[0]["url"], "lora_strength": items[0]["scale"]}
    return {"loras": [{"path": it["url"], "scale": it["scale"]} for it in items]}


def _lora_kind(node_type):
    return "image" if node_type in ("image", "edit", "inpaint") else "video"


def _lora_params(node):
    """Authored LoRAs -> the per-family request keys (play.html loraParams)."""
    model = node.fields.get("model")
    if not _model_takes_lora(_lora_kind(node.type), model):
        return {}
    rows = [r for r in _node_loras(node)
            if isinstance(r, dict) and str(r.get("url") or "").strip()]
    if not rows:
        return {}
    items = [{"url": _normalize_lora_url(r.get("url")), "scale": _lora_scale(r.get("strength"))}
             for r in rows[:_lora_cap(model)]]
    return _lora_body_for(model, items)


# ---- custom-civitai AIR (play.html normalizeCustomCivitaiAir / isValidCustomAir)

_AIR_VALID_RE = re.compile(
    r"^(civitai:\d+@\d+|persona:\d+@\d+|runware:[^\s@]+@[^\s@]+)$", re.I)


def _normalize_custom_civitai_air(raw):
    s = str(raw or "").strip()
    if not s:
        return ""
    if re.match(r"^civitai:\d+@\d+", s, re.I):
        return re.sub(r"^civitai:", "civitai:", s, flags=re.I)
    if re.match(r"^persona:\d+@\d+", s, re.I):
        return re.sub(r"^persona:", "persona:", s, flags=re.I)
    if re.match(r"^runware:[^\s@]+@[^\s@]+$", s, re.I):
        return re.sub(r"^runware:", "runware:", s, flags=re.I)
    bare = re.match(r"^(\d+)@(\d+)$", s)
    if bare:
        return "civitai:%s@%s" % (bare.group(1), bare.group(2))
    mid = re.search(r"civitai\.com/models/(\d+)", s, re.I)
    vid = re.search(r"[?&]modelVersionId=(\d+)", s, re.I)
    if mid and vid:
        return "civitai:%s@%s" % (mid.group(1), vid.group(1))
    return s


def _collect_ports(inp, rx):
    def idx(name):
        m = re.search(r"(\d+)$", name)
        return int(m.group(1)) if m else 1
    keys = sorted((k for k in inp if rx.match(k)), key=idx)
    return [_as_url(inp[k]) for k in keys if inp[k]]


def _audio_input_part(url):
    """Wired audio data: URL -> OpenAI-style input_audio part (play.html audioInputPart).

    Callers must inline https URLs first (engine.fetch_media) — SPEC-engine
    mandates base64 bytes ("data:<base64 body, no data: prefix>"), and shipping
    a raw URL string as "base64 data" makes a paid call with garbage audio.
    """
    url = _as_url(url)
    if not url:
        return None
    if not re.match(r"^data:", url, re.I):
        raise NanoodleError("audio input must be a data: URL — download the clip "
                            "and inline it before building the chat part")
    if len(url) > MEDIA_INLINE_MAX:
        raise NanoodleError("audio clip is too large to inline (~4 MB send limit) — use a shorter clip")
    comma = url.find(",")
    head = url[:comma] if comma >= 0 else ""
    data = url[comma + 1:] if comma >= 0 else url
    m = re.match(r"data:([^;]+)", head)
    fmt = ((m.group(1).split("/")[1] if m else "") or "wav").lower()
    if fmt in ("mpeg", "mp3"):
        fmt = "mp3"
    elif fmt in ("x-wav", "wave"):
        fmt = "wav"
    return {"type": "input_audio", "input_audio": {"data": data, "format": fmt}}


# ---- chat family -----------------------------------------------------------

def _chat_body(node, messages):
    f = node.fields
    body = {"model": _mdl(node), "messages": messages}
    t = f.get("temperature")
    if t is not None and str(t) != "":
        # int-normalize whole values so a typed "1" serializes as 1, not 1.0
        # (JS `+temperature` -> 1) — keeps request bodies byte-identical
        num = float(t)
        body["temperature"] = int(num) if num == int(num) else num
    else:
        body["temperature"] = 0.8
    return body


def _build_user_message(prompt, imgs, audio_part=None):
    parts_needed = bool(imgs) or audio_part is not None
    if not parts_needed:
        return {"role": "user", "content": prompt}
    content = [{"type": "text", "text": prompt}]
    content += [{"type": "image_url", "image_url": {"url": u}} for u in imgs]
    if audio_part is not None:
        content.append(audio_part)
    return {"role": "user", "content": content}


def _parse_chat_text(j, show_thinking=False):
    msg = ((j.get("choices") or [{}])[0] or {}).get("message") or {}
    txt = msg.get("content")
    if txt is None or txt == "":  # SPEC-engine: null/EMPTY content both error
        raise NanoodleError("no text in response")
    if isinstance(txt, list):
        txt = "".join(p.get("text") or "" for p in txt)
    if show_thinking and msg.get("reasoning"):
        txt = "```thinking\n%s\n```\n\n%s" % (msg["reasoning"], txt)
    return txt


def _inline_hosted_audio(engine, url):
    """Hosted audio (music/tts nodes return https CDN URLs verbatim) -> download
    and inline as a data: URL: the chat input_audio part carries bytes, never a
    URL (mirrors JS client.fetchMediaDataUrl)."""
    data, ctype = engine.fetch_media(url)
    mime = (ctype or "").split(";")[0].strip().lower()
    if not mime or mime in ("application/octet-stream", "binary/octet-stream"):
        mime = None  # make_data_url sniffs magic bytes when the CDN's type is generic
    return make_data_url(data, mime)


def _run_llm(engine, node, inp, on_cost):
    f = node.fields
    prompt = _prompt_of(node)
    imgs = _collect_ports(inp, IMG_PORT_RE)
    audio_src = _as_url(inp["audio"]) if inp.get("audio") else None
    if audio_src and re.match(r"^https?:", audio_src, re.I):
        audio_src = _inline_hosted_audio(engine, audio_src)
    audio_part = _audio_input_part(audio_src) if audio_src else None
    messages = []
    if _fstr(node, "system").strip():
        messages.append({"role": "system", "content": _fstr(node, "system").strip()})
    messages.append(_build_user_message(prompt, imgs, audio_part))
    body = _chat_body(node, messages)
    if f.get("maxTokens") and _is_num(f["maxTokens"]):
        # non-numeric degrades gracefully (play.html: +maxTokens -> NaN -> null)
        body["max_tokens"] = int(float(f["maxTokens"]))
    if f.get("format") == "JSON":
        body["response_format"] = {"type": "json_object"}
    if f.get("reasoningEffort") and f["reasoningEffort"] != "default":
        body["reasoning_effort"] = f["reasoningEffort"]
    resp = engine._post_json(CHAT_ENDPOINT, body)
    j = json.loads(resp.text())
    on_cost(*engine._cost_with_headers(j, resp))
    show = f.get("showThinking") in (True, "true")
    return {"text": _parse_chat_text(j, show_thinking=show)}


def _run_vision(engine, node, inp, on_cost):
    if not inp.get("image"):
        raise NanoodleError("no image input")
    q = (_fstr(node, "q") or "Describe this image.").strip() or "Describe this image."
    messages = [{"role": "user", "content": [
        {"type": "text", "text": q},
        {"type": "image_url", "image_url": {"url": _as_url(inp["image"])}},
    ]}]
    resp = engine._post_json(CHAT_ENDPOINT, _chat_body(node, messages))
    j = json.loads(resp.text())
    on_cost(*engine._cost_with_headers(j, resp))
    return {"text": _parse_chat_text(j)}


# ---- image family (/v1/images/generations) ---------------------------------

def _gen_image(engine, node, on_cost, prompt, n=1, image_data_url=None, mask_data_url=None):
    f = node.fields
    body = {"model": _mdl(node), "size": f.get("size") or "1024x1024",
            "n": n, "response_format": "b64_json"}
    if prompt:
        body["prompt"] = prompt  # omit when blank — upscalers run with no instruction
    if image_data_url:
        body["imageDataUrl"] = image_data_url
    if mask_data_url:
        body["maskDataUrl"] = mask_data_url
    body.update(_lora_params(node))  # authored LoRAs ride on image/edit/inpaint (imgExtra)
    seed = f.get("seed")
    if seed is not None and str(seed).strip() != "" and _is_num(seed):
        body["seed"] = float(seed) if "." in str(seed) else int(float(seed))
    if f.get("model") == "custom-civitai":
        # normalize + validate the AIR BEFORE the paid call (play.html imgExtra)
        air = _normalize_custom_civitai_air(f.get("customCivitaiAir"))
        if not air:
            raise NanoodleError("select an AIR model — pick a Runware preset or paste "
                                "civitai:/runware:/persona:…")
        if not _AIR_VALID_RE.match(air):
            raise NanoodleError("AIR must look like civitai:MODEL@VERSION, runware:id@rev, "
                                "or persona:MODEL@VERSION")
        body["customCivitaiAir"] = air
    resp = engine._post_json(IMG_ENDPOINT, body)
    j = json.loads(resp.text())
    urls = []
    for d in (j.get("data") or []):
        if d.get("b64_json"):
            urls.append("data:%s;base64,%s" % (b64_image_mime(d["b64_json"]), d["b64_json"]))
        elif d.get("url"):
            urls.append(d["url"])
    if not urls:
        raise NanoodleError("no image in response")
    on_cost(*engine._cost_with_headers(j, resp))
    return [engine._media_ref(u) for u in urls]


def _run_image(engine, node, inp, on_cost):
    prompt = _prompt_of(node)
    try:
        want = max(1, int(float(node.fields.get("variations") or 1)))
    except (TypeError, ValueError):
        want = 1
    urls = _gen_image(engine, node, on_cost, prompt, n=want)
    return {"image": urls[_sel_index(node, len(urls))], "images": urls}


def _run_edit(engine, node, inp, on_cost):
    imgs = _collect_ports(inp, EDIT_IMG_RE)
    if not imgs:
        raise NanoodleError("no image input")
    prompt = _fstr(node, "prompt").strip()
    if not prompt and not re.search(r"upscal", node.fields.get("model") or "", re.I):
        raise NanoodleError("no edit instruction")
    if sum(len(u) for u in imgs) > MEDIA_INLINE_MAX:
        raise NanoodleError("reference images too large (~4 MB combined limit) — use fewer or smaller images")
    src = imgs if len(imgs) > 1 else imgs[0]
    urls = _gen_image(engine, node, on_cost, prompt, image_data_url=src)
    return {"image": urls[0]}


def _run_inpaint(engine, node, inp, on_cost):
    source = _as_url(inp.get("image")) or node.fields.get("image")
    mask = _as_url(inp.get("mask")) or node.fields.get("mask")
    if not source:
        raise NanoodleError("no image — upload one, then brush the area to repaint")
    if not mask:
        raise NanoodleError("no mask — brush the area to repaint (white)")
    prompt = _prompt_of(node, err="no prompt — say what to paint into the brushed area")
    # v1 caveat: the browser composites the mask onto black at source size; this
    # library passes the mask through verbatim (white = repaint).
    urls = _gen_image(engine, node, on_cost, prompt, image_data_url=source, mask_data_url=mask)
    return {"image": urls[0]}


# ---- video family (submit + poll) ------------------------------------------

def _video_dims(node):
    out = {}
    f = node.fields
    if f.get("resolution"):
        out["resolution"] = f["resolution"]
    if f.get("aspect"):
        out["aspect_ratio"] = f["aspect"]
    if f.get("duration"):
        out["duration"] = f["duration"]
    return out


def _gen_video(engine, node, on_cost, prompt, extra_body):
    body = {"model": _mdl(node), "prompt": prompt}
    dims = _video_dims(node)
    body.update(dims)
    body.update(extra_body.pop("_sources", {}))
    if node.type in ("tvideo", "ivideo", "vedit"):
        # authored LoRAs (play.html: only these runs pass opts.lora to genVideo)
        body.update(_lora_params(node))
    model_opts = node.fields.get("modelOpts") or {}
    if isinstance(model_opts, dict):
        body.update(model_opts)
    body.update(dims)  # node-owned dims win over stale modelOpts keys
    body.update(extra_body)  # wired refs LAST — a wired port always wins the key
    resp = engine._post_json(VIDEO_SUBMIT, body)
    j = json.loads(resp.text())
    on_cost(*engine._cost_with_headers(j, resp))
    run_id = j.get("runId") or j.get("id")
    if not run_id:
        raise NanoodleError("no runId returned")
    t0 = time.monotonic()
    while time.monotonic() - t0 < engine.timeout_video:
        time.sleep(engine.poll_video)
        try:
            resp = engine._get(VIDEO_STATUS + "?requestId=" + urllib.parse.quote(str(run_id)))
            s = json.loads(resp.text())
        except (NanoodleError, ValueError):
            continue  # poll failures (transport OR body): silently continue until timeout
        if not (200 <= resp.status < 300):
            continue
        data = s.get("data") if isinstance(s.get("data"), dict) else None
        st = str((data or {}).get("status") or s.get("status") or "").upper()
        engine._progress({"type": "poll", "node_id": node.id, "name": display_name(node),
                          "status": st, "elapsed": time.monotonic() - t0})
        if st in ("COMPLETED", "SUCCEEDED"):
            out = (data or {}).get("output") or s.get("output") or {}
            url = None
            video = out.get("video")
            if isinstance(video, dict):
                url = video.get("url")
            if not url:
                url = out.get("url")
            if not url and isinstance(video, list) and video:
                url = (video[0] or {}).get("url")
            if not url:
                raise NanoodleError("completed but no video url")
            return engine._media_ref(url)
        if st in ("FAILED", "ERROR", "CANCELED"):
            raise NanoodleError("video failed: " + str((data or {}).get("error") or st))
    raise NanoodleError("video timed out (%d s) — the job may still be running on NanoGPT's side"
                        % int(engine.timeout_video))


def _run_tvideo(engine, node, inp, on_cost):
    prompt = _prompt_of(node)
    extra = {}
    refs = _collect_ports(inp, REF_PORT_RE)
    if refs:
        extra["reference_images"] = refs
    return {"video": _gen_video(engine, node, on_cost, prompt, extra)}


def _run_ivideo(engine, node, inp, on_cost):
    if not inp.get("image"):
        raise NanoodleError("no image input")
    prompt = _fstr(node, "prompt").strip()
    extra = {"_sources": {"imageDataUrl": _as_url(inp["image"])}}
    if inp.get("endframe"):
        extra["_sources"]["last_image"] = _as_url(inp["endframe"])
    return {"video": _gen_video(engine, node, on_cost, prompt, extra)}


def _run_vedit(engine, node, inp, on_cost):
    if not inp.get("video"):
        raise NanoodleError("no video input")
    prompt = _fstr(node, "prompt").strip()
    src = _as_url(inp["video"])
    sources = {"videoUrl": src} if re.match(r"^https?:", src) else {"videoDataUrl": src}
    return {"video": _gen_video(engine, node, on_cost, prompt, {"_sources": sources})}


def _run_lipsync(engine, node, inp, on_cost):
    if not inp.get("image"):
        raise NanoodleError("no image input")
    if not inp.get("audio"):
        raise NanoodleError("no audio input")
    prompt = _fstr(node, "prompt").strip()
    aud = _as_url(inp["audio"])
    sources = {"imageDataUrl": _as_url(inp["image"])}
    sources.update({"audioUrl": aud} if re.match(r"^https?:", aud) else {"audioDataUrl": aud})
    return {"video": _gen_video(engine, node, on_cost, prompt, {"_sources": sources})}


# ---- audio family (music / tts / remix) -------------------------------------

# (field, type, default) — sent only when non-empty and != default (play.html AUDIO_PARAMS)
_AUDIO_PARAMS = {
    "music": [("lyrics", "text", None), ("instrumental", "boolean", None),
              ("duration", "number", None), ("negative_prompt", "text", None),
              ("seed", "number", None), ("response_format", "text", "mp3")],
    "tts": [("voice", "text", None), ("speed", "number", 1),
            ("instructions", "text", None), ("response_format", "text", "mp3")],
    "remix": [("lyrics", "text", None), ("duration", "number", None),
              ("response_format", "text", "mp3")],
}


def _collect_audio_params(node):
    body = {}
    for (fid, ftype, default) in _AUDIO_PARAMS.get(node.type, []):
        v = node.fields.get(fid)
        if ftype == "boolean":
            if v in (True, "true"):
                body[fid] = True
            continue
        if v is None or v == "":
            continue
        if ftype == "number":
            if not _is_num(v):
                continue
            num = float(v)
            num = int(num) if num == int(num) else num
            if default is not None and num == default:
                continue
            body[fid] = num
            continue
        if default is not None and v == default:
            continue
        body[fid] = v
    extra = str(node.fields.get("extraJson") or "").strip()
    if extra:
        try:
            body.update(json.loads(extra))
        except ValueError:
            raise NanoodleError("advanced params: invalid JSON")
    if node.type in ("music", "remix"):
        # surface-one-track contract: drop every song-count key (omit = one track)
        for k in list(body.keys()):
            if _SONG_COUNT_RE.match(k) or _SONG_COUNT_LOOSE_RE.search(k):
                del body[k]
    return body


def _poll_audio(engine, node, model, submit_json):
    run_id = submit_json.get("runId") or submit_json.get("id")
    qs = {"runId": str(run_id), "model": model}
    if submit_json.get("cost") is not None:
        qs["cost"] = str(submit_json["cost"])
    if submit_json.get("paymentSource"):
        qs["paymentSource"] = str(submit_json["paymentSource"])
    if submit_json.get("isApiRequest") is not None:
        v = submit_json["isApiRequest"]
        qs["isApiRequest"] = "true" if v is True else ("false" if v is False else str(v))
    query = urllib.parse.urlencode(qs)
    t0 = time.monotonic()
    while time.monotonic() - t0 < engine.timeout_audio:
        time.sleep(engine.poll_audio)
        try:
            resp = engine._get(AUDIO_STATUS + "?" + query)
            s = json.loads(resp.text())
        except (NanoodleError, ValueError):
            continue  # poll failures (transport OR body): silently continue until timeout
        st = str(s.get("status") or "").lower()
        engine._progress({"type": "poll", "node_id": node.id, "name": display_name(node),
                          "status": st, "queue": s.get("queuePosition"),
                          "elapsed": time.monotonic() - t0})
        if st in ("completed", "succeeded"):
            data = s.get("data") if isinstance(s.get("data"), dict) else {}
            url = s.get("audioUrl") or s.get("url") or data.get("audioUrl") or data.get("url")
            if not url:
                raise NanoodleError("completed but no audio url")
            return url
        if st in ("error", "failed", "content_policy_violation"):
            raise NanoodleError("audio failed: " + str(s.get("error") or s.get("message") or st))
    raise NanoodleError("audio timed out (%d s)" % int(engine.timeout_audio))


def _gen_audio(engine, node, on_cost, text, extra):
    body = dict({"model": _mdl(node), "input": text})
    body.update(extra)
    resp = engine._post_json(AUDIO_ENDPOINT, body)
    ctype = (resp.header("content-type") or "").lower()
    if "application/json" in ctype:
        j = json.loads(resp.text())
        on_cost(*engine._cost_with_headers(j, resp))
        data = j.get("data") if isinstance(j.get("data"), dict) else {}
        url = j.get("url") or j.get("audioUrl") or data.get("url") or data.get("audioUrl")
        if not url and (j.get("runId") or j.get("id")):
            url = _poll_audio(engine, node, body["model"], j)
        if not url:
            raise NanoodleError("no audio url in response")
        return engine._media_ref(url)
    # binary body: the audio bytes; pin the mime from the requested format when
    # the response type is missing or generic
    on_cost(*engine._cost_from_headers(resp))
    mime = ctype.split(";")[0].strip()
    if not mime or mime in ("application/octet-stream", "binary/octet-stream"):
        fmt = str(extra.get("response_format") or "mp3")
        mime = _AUDIO_MIME.get(fmt, "audio/mpeg")
    return MediaRef(make_data_url(resp.body, mime), mime=mime, fetcher=engine.fetch_media)


def _run_music(engine, node, inp, on_cost):
    text = _fstr(node, "prompt").strip()
    if not text:
        raise NanoodleError("no text")
    return {"audio": _gen_audio(engine, node, on_cost, text, _collect_audio_params(node))}


def _run_tts(engine, node, inp, on_cost):
    text = _fstr(node, "prompt").strip()
    if not text:
        raise NanoodleError("no text")
    return {"audio": _gen_audio(engine, node, on_cost, text, _collect_audio_params(node))}


def _run_remix(engine, node, inp, on_cost):
    if not inp.get("audio"):
        raise NanoodleError("no audio — wire a source track into the audio port")
    text = _fstr(node, "prompt").strip()
    if not text:
        raise NanoodleError("no prompt — describe the cover / extension first")
    extra = _collect_audio_params(node)
    src = _as_url(inp["audio"])
    if not re.match(r"^https?:", src) and len(src) > MEDIA_INLINE_MAX:
        raise NanoodleError("source audio is too large to inline (~4 MB send limit) — use a shorter clip")
    extra["audio"] = src
    return {"audio": _gen_audio(engine, node, on_cost, text, extra)}


# ---- transcribe --------------------------------------------------------------

def _run_transcribe(engine, node, inp, on_cost):
    if not inp.get("audio"):
        raise NanoodleError("no audio input")
    src = _as_url(inp["audio"])
    if src.startswith("data:"):
        mime, data = parse_data_url(src)
    else:
        data, ctype = engine.fetch_media(src)
        mime = (ctype or "audio/mpeg").split(";")[0].strip() or "audio/mpeg"
    if len(data) > TRANSCRIBE_MAX_BYTES:
        raise NanoodleError("this clip is too big to transcribe directly (~3.5 MB max) — use a shorter clip")
    ext = (mime.split("/")[1] if "/" in mime else "mp3").split(";")[0] or "mp3"
    fields = {"model": _mdl(node)}
    language = (_fstr(node, "language") or "auto").strip()
    if language:
        fields["language"] = language
    ctype, body = encode_multipart(fields, "file", "audio." + ext, data, mime)
    headers = engine._auth_headers()
    headers["Content-Type"] = ctype  # boundary set by our encoder; no other Content-Type
    resp = engine._paid_send("POST", engine.base_url + TRANSCRIBE_ENDPOINT, headers, body)
    if not (200 <= resp.status < 300):
        engine._raise_http(resp)
    j = json.loads(resp.text())
    on_cost(*engine._cost_with_headers(j, resp))
    data_obj = j.get("data") if isinstance(j.get("data"), dict) else {}
    txt = j.get("transcription")
    if txt is None:
        txt = j.get("text")
    if txt is None:
        txt = data_obj.get("transcription")
    if txt is None:
        txt = data_obj.get("text")
    if txt is None:
        raise NanoodleError("no transcription in response")
    return {"text": txt}


# ---- local nodes --------------------------------------------------------------

def _run_text(engine, node, inp, on_cost):
    return {"text": _fstr(node, "text")}


def _run_upload(field):
    def run(engine, node, inp, on_cost):
        v = node.fields.get(field)
        if not v:
            raise NanoodleError("no %s provided — supply it as a run input" % field)
        url = _as_url(v)
        return {field: engine._media_ref(url) if not isinstance(v, MediaRef) else v}
    return run


def _run_choice(engine, node, inp, on_cost):
    opts = [s.strip() for s in _fstr(node, "options").split("\n") if s.strip()]
    if not opts:
        raise NanoodleError("no options — this Choice has no options to pick from")
    sel = node.fields.get("selected")
    return {"text": sel if sel in opts else opts[0]}


def _run_join(engine, node, inp, on_cost):
    sep = node.fields.get("sep")
    sep = " " if sep is None else str(sep)
    sep = sep.replace("\\n", "\n")
    parts = [v for v in (inp.get("a"), inp.get("b")) if v is not None and v != ""]
    return {"text": sep.join(str(v) for v in parts)}


# ---- local media (ffmpeg on PATH) ---------------------------------------------

def _media_url(v):
    if isinstance(v, MediaRef):
        return v.url
    return v


def _run_resize(engine, node, inp, on_cost):
    from .local_media import resize_crop_image
    engine.check_cancel()
    img = inp.get("image")
    if not img:
        raise NanoodleError("no image input")
    return {"image": engine._media_ref(resize_crop_image(
        _media_url(img), node.fields.get("mode") or "fit",
        node.fields.get("width"), node.fields.get("height"),
        **engine._local_opts()))}


def _run_vframes(engine, node, inp, on_cost):
    from .local_media import extract_video_frames
    from .graph import MAX_FRAMES
    engine.check_cancel()
    vid = inp.get("video")
    if not vid:
        raise NanoodleError("no video input")
    # fields.frames is already raised to wired_frames_floor by Workflow.run;
    # re-clamp here so a direct engine.run_node call stays safe too.
    try:
        count = max(1, min(MAX_FRAMES, int(node.fields.get("frames") or 1)))
    except (TypeError, ValueError):
        count = 1
    frames = extract_video_frames(
        _media_url(vid),
        count=count,
        gap=node.fields.get("gap") if node.fields.get("gap") is not None else 0.5,
        dir=node.fields.get("dir") or "end",
        **engine._local_opts())
    return {k: engine._media_ref(v) for k, v in frames.items()}


def _run_combine(engine, node, inp, on_cost):
    from .local_media import concat_videos
    from .graph import CLIP_PORT_RE, VID_PORT_RE

    engine.check_cancel()

    def port_idx(name):
        m = re.search(r"(\d+)$", name)
        return int(m.group(1)) if m else 1

    # Sort by port number then name so clip1/vid1 order is stable across families;
    # de-dupe values while preserving first-seen order.
    keys = sorted(
        [k for k in inp if CLIP_PORT_RE.match(k) or VID_PORT_RE.match(k)],
        key=lambda k: (port_idx(k), k))
    ordered = []
    seen = set()
    for k in keys:
        c = _media_url(inp.get(k))
        if c and c not in seen:
            seen.add(c)
            ordered.append(c)
    if len(ordered) < 2:
        raise NanoodleError("wire at least two clips to combine")
    dedup_raw = node.fields.get("dedup")
    dedup = True if dedup_raw is None else str(dedup_raw).lower() not in ("false", "0", "")
    return {"video": engine._media_ref(concat_videos(
        ordered, dedup=dedup, **engine._local_opts()))}


def _run_soundtrack(engine, node, inp, on_cost):
    from .local_media import mux_soundtrack
    engine.check_cancel()
    if not inp.get("video"):
        raise NanoodleError("no video input")
    if not inp.get("audio"):
        raise NanoodleError("no audio input")
    loop_raw = node.fields.get("loop")
    loop = str(loop_raw).lower() in ("true", "1") if loop_raw is not None else False
    return {"video": engine._media_ref(mux_soundtrack(
        _media_url(inp["video"]), _media_url(inp["audio"]), loop=loop,
        **engine._local_opts()))}


def _run_trim(engine, node, inp, on_cost):
    from .local_media import trim_audio_to_wav
    engine.check_cancel()
    if not inp.get("audio"):
        raise NanoodleError("no audio input")
    start = float(node.fields.get("start") or 0)
    try:
        length = float(node.fields.get("length"))
    except (TypeError, ValueError):
        length = 30.0
    if not (length > 0):
        length = 30.0
    return {"audio": engine._media_ref(trim_audio_to_wav(
        _media_url(inp["audio"]), start, length, 16000, **engine._local_opts()))}


def _run_extractaudio(engine, node, inp, on_cost):
    from .local_media import extract_audio_to_wav
    engine.check_cancel()
    if not inp.get("video"):
        raise NanoodleError("no video input")
    start = float(node.fields.get("start") or 0)
    try:
        length = float(node.fields.get("length"))
    except (TypeError, ValueError):
        length = 0
    if not (length > 0):
        length = 0
    return {"audio": engine._media_ref(extract_audio_to_wav(
        _media_url(inp["video"]), start, length, 16000, **engine._local_opts()))}


_EXECUTORS = {
    "text": _run_text,
    "upload": _run_upload("image"),
    "aupload": _run_upload("audio"),
    "vupload": _run_upload("video"),
    "choice": _run_choice,
    "join": _run_join,
    "resize": _run_resize,
    "vframes": _run_vframes,
    "combine": _run_combine,
    "soundtrack": _run_soundtrack,
    "trim": _run_trim,
    "extractaudio": _run_extractaudio,
    "llm": _run_llm,
    "vision": _run_vision,
    "image": _run_image,
    "edit": _run_edit,
    "inpaint": _run_inpaint,
    "tvideo": _run_tvideo,
    "ivideo": _run_ivideo,
    "vedit": _run_vedit,
    "lipsync": _run_lipsync,
    "music": _run_music,
    "tts": _run_tts,
    "remix": _run_remix,
    "transcribe": _run_transcribe,
}
