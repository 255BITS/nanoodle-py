"""Decode-only codec for nanoodle share links — the editor stays the single
encoder of record; these functions only ever read.

Wire formats (mirrors index.html's loadFromHash / buildShareUrl and the
nanoodle-js src/share.mjs twin, locked by the golden fixtures in
tests/fixtures/share/ — those are minted by a REAL editor and shared
byte-identically between the JS and Python libraries):

    #g=<b64url(gzip(graph JSON))>          workflow link (editor 🔗 Share)
    #j=<b64url(graph JSON)>                uncompressed fallback (no CompressionStream)
    #a=<b64url(gzip(app payload))>         app link (play.html); payload = { v, graph, files?, name?, lang?, ... }
    #a=u<b64url(app payload)>              uncompressed app fallback ('u' tag inside the value)
    #ga=…                                  editor↔play handoff — internal transport, deliberately NOT supported

Stdlib only (zlib for gzip, base64 for base64url, urllib for the by-hand
redirect reads on short links). Direct fragment links decode with zero network
I/O; only fragment-less http(s) URLs (short links) ever touch the network.
"""

import base64
import binascii
import re
import urllib.error
import urllib.request
import zlib
from urllib.parse import urljoin

from .errors import NanoodleError

_URL_RE = re.compile(r"^https?://", re.I)
_FRAG_RE = re.compile(r"^#?(ga|[gja])=")
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def is_share_ref(s):
    """True when a string is addressable as a share link: an http(s) URL, or a
    bare #g=/#j=/#a= (or tail g=/j=/a=) fragment."""
    return isinstance(s, str) and bool(_URL_RE.match(s) or _FRAG_RE.match(s))


def _b64url_to_bytes(s, what):
    if not _B64URL_RE.match(s):
        raise NanoodleError(
            "share link: %s payload is not base64url data — is the URL complete?" % what)
    # restore the '=' padding urlsafe_b64decode requires (share links drop it);
    # a truncated link can still be an impossible base64 length → friendly error
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (binascii.Error, ValueError):
        raise NanoodleError(
            "share link: %s payload is not base64url data — the link may be truncated" % what)


def _parse_json(text, what):
    import json
    try:
        return json.loads(text)
    except ValueError:
        raise NanoodleError(
            "share link: %s payload decoded but is not valid JSON — the link may be truncated"
            % what)


def _gunzip_text(buf, what):
    try:
        # wbits 16 + MAX_WBITS decodes a gzip container (matches the editor's
        # CompressionStream('gzip') and nanoodle-js's gunzipSync)
        return zlib.decompress(buf, 16 + zlib.MAX_WBITS).decode("utf-8")
    except (zlib.error, UnicodeDecodeError):
        raise NanoodleError(
            "share link: %s payload is not valid gzip data — the link may be truncated" % what)


# ---- best-effort salvage for damaged links ----------------------------------
# Links get mangled in transit all the time — chat apps, line wraps, and manual
# copy/paste flip or drop a character, which breaks the gzip CRC (and often a
# few JSON characters) while leaving most of the payload intact. Executors only
# need `nodes` and `links`, so when strict decoding fails we lax-decompress
# (trailer ignored, partial output kept) and pull those two arrays out of the
# damaged text. Cosmetic editor state (view, nid/lid) is sacrificed; damage
# inside the graph itself still fails with the original error. Results carry
# ``"recovered": True`` so callers can warn. (Mirrors nanoodle-js share.mjs.)


def _gzip_body_start(b):
    """Offset of the deflate body inside a gzip member, or -1 when not gzip."""
    if len(b) < 11 or b[0] != 0x1F or b[1] != 0x8B or b[2] != 8:
        return -1
    flg = b[3]
    i = 10
    if flg & 4:  # FEXTRA
        if i + 2 > len(b):
            return -1
        i += 2 + (b[i] | (b[i + 1] << 8))
    if flg & 8:  # FNAME
        while i < len(b) and b[i] != 0:
            i += 1
        i += 1
    if flg & 16:  # FCOMMENT
        while i < len(b) and b[i] != 0:
            i += 1
        i += 1
    if flg & 2:  # FHCRC
        i += 2
    return i if i < len(b) else -1


def _gunzip_lax(buf):
    """Best-effort gunzip: raw-inflate the body, ignore the CRC32/ISIZE
    trailer, keep partial output on truncation. None when nothing
    decompressible remains."""
    start = _gzip_body_start(buf)
    if start < 0:
        return None
    # The 8-byte trailer is junk to a raw-deflate decoder; drop it up front
    # (mirrors the JS twin — on a truncated payload this trims real data, but
    # the chunked loop below already keeps everything before the damage).
    body = buf[start:max(start + 1, len(buf) - 8)]
    d = zlib.decompressobj(-zlib.MAX_WBITS)
    out = []
    try:
        for i in range(0, len(body), 1024):  # chunked so a late error keeps earlier output
            out.append(d.decompress(body[i:i + 1024]))
        out.append(d.flush())
    except zlib.error:
        pass
    data = b"".join(out)
    return data or None


def _match_bracket(text, i):
    """Index of the bracket closing text[i] (a "[" or "{"), string-aware; -1
    when unbalanced."""
    if i >= len(text) or text[i] not in "[{":
        return -1
    depth = 0
    in_str = False
    j = i
    while j < len(text):
        c = text[j]
        if in_str:
            if c == "\\":
                j += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if not depth:
                return j
        j += 1
    return -1


def _extract_json_value(text, key):
    """Parse the value of ``"key": …`` out of possibly-damaged JSON text; None
    when no occurrence parses."""
    import json
    needle = '"%s"' % key
    frm = 0
    while True:
        at = text.find(needle, frm)
        if at == -1:
            return None
        frm = at + 1
        j = at + len(needle)
        while j < len(text) and text[j].isspace():
            j += 1
        if j >= len(text) or text[j] != ":":
            continue
        j += 1
        while j < len(text) and text[j].isspace():
            j += 1
        end = _match_bracket(text, j)
        if end == -1:
            continue
        try:
            return json.loads(text[j:end + 1])
        except ValueError:
            continue  # damaged here — try the next occurrence


def _salvage_graph(text):
    if not text:
        return None
    nodes = _extract_json_value(text, "nodes")
    if not (isinstance(nodes, list) and nodes
            and all(isinstance(n, dict) and isinstance(n.get("type"), str) for n in nodes)):
        return None
    links = _extract_json_value(text, "links")
    return {"v": 1, "nodes": nodes, "links": links if isinstance(links, list) else []}


def _lax_text(data):
    return data.decode("utf-8", errors="replace") if data else None


def decode_share_fragment(fragment):
    """Decode a share fragment ("#g=…", "g=…", "#a=…", …) to its graph.

    Returns a dict ``{"graph": dict, "kind": "g"|"j"|"a", "app": dict|None}``.
    ``app`` is present only for #a= links: ``{name?, lang?, has_files: bool}``
    (files/samples/lang are play.html presentation — executors run graphs, not
    apps). A damaged link whose graph was salvaged best-effort additionally
    carries ``"recovered": True`` (nodes + links only — cosmetic editor state
    is dropped); warn the user and suggest re-copying the link.
    """
    f = str(fragment)
    if f.startswith("#"):
        f = f[1:]
    if f.startswith("ga="):
        raise NanoodleError(
            "share link: #ga= is the editor↔app-builder handoff — an internal, unstable "
            "format. Open the link in a browser and use 🔗 Share to mint a #g= workflow "
            "link instead.")
    if f.startswith("g="):
        buf = _b64url_to_bytes(f[2:], "#g=")
        text = None
        try:
            text = _gunzip_text(buf, "#g=")
            return {"graph": _parse_json(text, "#g="), "kind": "g", "app": None}
        except NanoodleError as e:
            strict_err = e
        if text is None:
            text = _lax_text(_gunzip_lax(buf))
        graph = _salvage_graph(text)
        if not graph:
            raise strict_err
        return {"graph": graph, "kind": "g", "app": None, "recovered": True}
    if f.startswith("j="):
        text = _b64url_to_bytes(f[2:], "#j=").decode("utf-8", errors="replace")
        try:
            return {"graph": _parse_json(text, "#j="), "kind": "j", "app": None}
        except NanoodleError as e:
            graph = _salvage_graph(text)
            if not graph:
                raise e
            return {"graph": graph, "kind": "j", "app": None, "recovered": True}
    if f.startswith("a="):
        tag = f[2:]
        strict_err = None
        if tag[:1] == "u":
            json_text = _b64url_to_bytes(tag[1:], "#a=u").decode("utf-8", errors="replace")
        else:
            buf = _b64url_to_bytes(tag, "#a=")
            try:
                json_text = _gunzip_text(buf, "#a=")
            except NanoodleError as e:
                strict_err = e
                json_text = _lax_text(_gunzip_lax(buf))
        if strict_err is None:
            try:
                payload = _parse_json(json_text, "#a=")
            except NanoodleError as e:
                strict_err = e
                payload = None
            if payload is not None:
                if not isinstance(payload, dict) or not payload.get("graph"):
                    raise NanoodleError("share link: #a= app payload has no graph in it")
                app = {"has_files": bool(payload.get("files"))}
                name = payload.get("name")
                if isinstance(name, str) and name:
                    app["name"] = name
                lang = payload.get("lang")
                if isinstance(lang, str) and lang:
                    app["lang"] = lang
                return {"graph": payload["graph"], "kind": "a", "app": app}
        # salvage: the app payload nests its graph — prefer the intact "graph"
        # object, else its nodes/links
        nested = _extract_json_value(json_text, "graph") if json_text else None
        if isinstance(nested, dict) and isinstance(nested.get("nodes"), list):
            graph = nested
        else:
            graph = _salvage_graph(json_text)
        if not graph:
            raise strict_err
        return {"graph": graph, "kind": "a", "app": {"has_files": False}, "recovered": True}
    raise NanoodleError(
        'share link: no #g=/#j=/#a= fragment found in "%s"' % fragment)


def _fragment_of(url):
    i = url.find("#")
    return None if i == -1 else url[i:]


def _default_opener(url):
    """Read one redirect hop without following it (fragments ride the Location
    header, so we must read it by hand) and without attaching any credentials.

    Returns ``(status, location_or_none)``.
    """
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None  # surface the 3xx to us instead of auto-following

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, method="GET")
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.status, resp.headers.get("location")
    except urllib.error.HTTPError as e:
        return e.code, (e.headers.get("location") if e.headers else None)
    except urllib.error.URLError as e:
        raise NanoodleError("share link: could not resolve %s: %s" % (url, e.reason))
    except OSError as e:
        raise NanoodleError("share link: could not resolve %s: %s" % (url, e))


def decode_share_url(ref, opener=None, max_hops=5):
    """Decode any nanoodle share reference — a full URL, a bare fragment, or a
    shortener link (da.gd/TinyURL/…) whose redirect target carries the fragment.

    Direct fragment links decode with ZERO network calls. Only fragment-less
    http(s) URLs trigger reads, and those are redirect-header reads with no
    credentials attached (the codec never sees an API key by construction).

    ``opener`` is an injectable hook ``opener(url) -> (status, location)`` used
    for the redirect reads (tests stub it; production uses urllib). Returns a
    dict ``{"graph", "kind", "app", "url"}``.
    """
    s = str(ref).strip()
    if not _URL_RE.match(s):
        result = decode_share_fragment(s)
        result["url"] = s
        return result

    url = s
    frag = _fragment_of(url)
    if frag and _FRAG_RE.match(frag):
        result = decode_share_fragment(frag)
        result["url"] = url
        return result

    # No fragment on the URL itself → treat it as a short link and follow
    # redirects by hand: fragments ride in the Location header, which automatic
    # redirect handling would consume before we could read it.
    fetch = opener or _default_opener
    for _ in range(max_hops):
        status, loc = fetch(url)
        loc = loc if (loc and 300 <= status < 400) else None
        if not loc:
            raise NanoodleError(
                "share link: %s answered %s with no #g=/#j=/#a= fragment and no redirect — "
                "open it in a browser and share the long nanoodle.com URL instead"
                % (url, status))
        url = urljoin(url, loc)
        hop_frag = _fragment_of(url)
        if hop_frag and _FRAG_RE.match(hop_frag):
            result = decode_share_fragment(hop_frag)
            result["url"] = url
            return result
    raise NanoodleError(
        "share link: gave up after %d redirects without finding a share fragment" % max_hops)
