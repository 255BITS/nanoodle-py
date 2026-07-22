"""Graph loading, materialization (aliases / migrations) and topological order.

Mirrors the loader semantics of the nanoodle editor (applyGraphData) and the
exported-app runtime (play.html RUNTIME_JS runGraph step 1).
"""

import json
import re

from .errors import NanoodleError

# Dynamic input-port families (SPEC-engine execution step 4).
IMG_PORT_RE = re.compile(r"^img\d+$")        # llm vision slots
EDIT_IMG_RE = re.compile(r"^image\d*$")      # edit multi-reference: image, image2, ...
VID_PORT_RE = re.compile(r"^vid\d+$")        # combine clips
CLIP_PORT_RE = re.compile(r"^clip\d+$")      # combine clips (spec alias)
REF_PORT_RE = re.compile(r"^ref\d+$")        # tvideo reference images
FRAME_PORT_RE = re.compile(r"^frame\d+$")    # vframes outputs
MAX_FRAMES = 12


def wired_frames_floor(graph, node_id):
    """Highest frameN port wired OUT of a vframes node (clamped 1..MAX_FRAMES).

    fields.frames is shape-affecting: run() emits frame1..frameN and downstream
    links read fixed frameK ports. A count below the highest wired port starves
    consumers mid-run (after upstream paid steps). Mirrors play.html
    wiredFramesFloor — floor is raised at run and in derive_settings.
    """
    floor = 1
    for link in (graph.links if graph is not None else []) or []:
        if link.from_node != node_id:
            continue
        m = re.match(r"^frame(\d+)$", str(link.from_port))
        if m:
            floor = max(floor, int(m.group(1)) or 1)
    return min(floor, MAX_FRAMES)


# Node type registry. outputs = [(port, kind)] with the PRIMARY port first.
# static = declared input port names; dynamic = port-name regexes.
# network nodes need an API key; local media ops need ffmpeg on PATH (soft dependency).
NODE_TYPES = {
    "text":       {"title": "Text",             "outputs": [("text", "text")]},
    "upload":     {"title": "Image input",      "outputs": [("image", "image")]},
    "aupload":    {"title": "Audio input",      "outputs": [("audio", "audio")]},
    "vupload":    {"title": "Video input",      "outputs": [("video", "video")]},
    "choice":     {"title": "Choice",           "outputs": [("text", "text")]},
    "join":       {"title": "Join",             "outputs": [("text", "text")], "static": ["a", "b"]},
    "llm":        {"title": "LLM",              "outputs": [("text", "text")], "static": ["audio"],
                   "dynamic": [IMG_PORT_RE], "network": True},
    "image":      {"title": "Image",            "outputs": [("image", "image")], "network": True},
    "edit":       {"title": "Edit",             "outputs": [("image", "image")],
                   "dynamic": [EDIT_IMG_RE], "network": True},
    "inpaint":    {"title": "Inpaint",          "outputs": [("image", "image")],
                   "static": ["image", "mask"], "network": True},
    "resize":     {"title": "Resize / crop",    "outputs": [("image", "image")],
                   "static": ["image"]},
    "vision":     {"title": "Vision",           "outputs": [("text", "text")],
                   "static": ["image"], "network": True},
    "tvideo":     {"title": "Text→Video",       "outputs": [("video", "video")],
                   "dynamic": [REF_PORT_RE], "network": True},
    "ivideo":     {"title": "Image→Video",      "outputs": [("video", "video")],
                   "static": ["image", "endframe"], "network": True},
    "vedit":      {"title": "Video edit",       "outputs": [("video", "video")],
                   "static": ["video"], "network": True},
    "vframes":    {"title": "Video → frames",   "outputs": [("frame1", "image")],
                   "static": ["video"]},
    "combine":    {"title": "Combine videos",   "outputs": [("video", "video")],
                   "dynamic": [VID_PORT_RE, CLIP_PORT_RE]},
    "soundtrack": {"title": "Soundtrack",       "outputs": [("video", "video")],
                   "static": ["video", "audio"]},
    "lipsync":    {"title": "Avatar / lipsync", "outputs": [("video", "video")],
                   "static": ["image", "audio"], "network": True},
    "music":      {"title": "Music",            "outputs": [("audio", "audio")], "network": True},
    "remix":      {"title": "Remix audio",      "outputs": [("audio", "audio")],
                   "static": ["audio"], "network": True},
    "tts":        {"title": "Speech",           "outputs": [("audio", "audio")], "network": True},
    "trim":       {"title": "Trim audio",       "outputs": [("audio", "audio")],
                   "static": ["audio"]},
    "extractaudio": {"title": "Extract audio",  "outputs": [("audio", "audio")],
                     "static": ["video"]},
    "transcribe": {"title": "Transcribe",       "outputs": [("text", "text")],
                   "static": ["audio"], "network": True},
    "comment":    {"title": "Comment",          "outputs": [], "note": True},
}

# Kept for import compatibility; empty — local media ops are implemented (need ffmpeg).
UNSUPPORTED_TYPES = tuple(t for t, s in NODE_TYPES.items() if s.get("unsupported"))


class Node(object):
    __slots__ = ("id", "type", "fields", "name")

    def __init__(self, id, type, fields, name=None):
        self.id = id
        self.type = type
        self.fields = fields
        self.name = name

    def spec(self):
        return NODE_TYPES.get(self.type, {})


class Link(object):
    __slots__ = ("id", "from_node", "from_port", "to_node", "to_port")

    def __init__(self, id, from_node, from_port, to_node, to_port):
        self.id = id
        self.from_node = from_node
        self.from_port = from_port
        self.to_node = to_node
        self.to_port = to_port


class Graph(object):
    def __init__(self, nodes, links, warnings):
        self.nodes = nodes          # ordered dict id -> Node
        self.links = links          # list of Link
        self.warnings = warnings    # list of str

    def node(self, node_id):
        return self.nodes.get(node_id)

    def inbound(self, node_id):
        return [l for l in self.links if l.to_node == node_id]

    def outbound(self, node_id):
        return [l for l in self.links if l.from_node == node_id]

    def port_is_fed(self, node_id, port):
        return any(l.to_node == node_id and l.to_port == port for l in self.links)


def display_name(node):
    """node.name (trimmed) -> type title -> type -> '?' (play.html displayName)."""
    if node is None:
        return "?"
    if node.name and str(node.name).strip():
        return str(node.name).strip()
    spec = NODE_TYPES.get(node.type)
    if spec and spec.get("title"):
        return spec["title"]
    return node.type or "?"


# Media fields hold a data: or http(s) URL (the editor writes data: URLs; hand-authored
# graphs may inline either). Agents sometimes leave prose placeholders ("[image will be
# provided at run time]") or bare file paths — those LOOK filled (inspect shows a default,
# the required-input check passes) but would post garbage to the API. Treat anything that
# isn't a real media URL as EMPTY at load, and say how to actually supply the media.
_MEDIA_FIELD_KEYS = ("image", "mask", "audio", "video")
_MEDIA_URL_RE = re.compile(r"^\s*(data:|https?:)", re.I)


def _scrub_media_placeholders(nid, fields, warnings):
    for key in _MEDIA_FIELD_KEYS:
        v = fields.get(key)
        if v is None or v == "" or (isinstance(v, str) and _MEDIA_URL_RE.match(v)):
            continue
        if isinstance(v, str):
            shown = '"%s"' % (v if len(v) <= 60 else v[:57] + "…")
        else:
            shown = "a non-string value (%s)" % type(v).__name__
        fields[key] = ""
        warnings.append(
            "node %s: fields.%s held %s — not a data: or http(s) URL, so it was treated as empty. "
            "Leave media fields \"\" in the graph and supply the %s at run time "
            "(CLI: --input \"<key>=@file\", library: run({\"<key>\": media_from_file(path)}), "
            "key from inspect/wf.inputs), or inline a data: URL." % (nid, key, shown, key))


def materialize(data, warnings=None):
    """Build a Graph from parsed noodle-graph.json (or a minimal {nodes, links}).

    Loader semantics replicated from the app:
      - type alias: audio -> tts (legacy)
      - links kept only when both endpoints exist
      - links into a music/tts node's "text" port are migrated to "prompt"
    Unknown node types are KEPT (with a load warning) — run() fails fast on them.
    """
    if isinstance(data, str):
        data = json.loads(data)
    if not isinstance(data, dict):
        raise NanoodleError("workflow JSON must be an object with a 'nodes' list")
    warnings = warnings if warnings is not None else []

    nodes = {}
    for raw in (data.get("nodes") or []):
        ntype = raw.get("type")
        if ntype == "audio":  # legacy alias
            ntype = "tts"
        nid = raw.get("id")
        if not nid:
            warnings.append("skipped a node with no id")
            continue
        if ntype not in NODE_TYPES:
            warnings.append("unknown node type %r (node %s) — it cannot be executed" % (ntype, nid))
        fields = dict(raw.get("fields") or {})
        if ntype in NODE_TYPES:
            _scrub_media_placeholders(nid, fields, warnings)
        nodes[nid] = Node(nid, ntype, fields, raw.get("name"))

    links = []
    for raw in (data.get("links") or []):
        f, t = raw.get("from") or {}, raw.get("to") or {}
        fn, fp, tn, tp = f.get("node"), f.get("port"), t.get("node"), t.get("port")
        if fn not in nodes or tn not in nodes:
            continue  # orphaned link
        # migration: legacy music/tts inbound "text" port is now "prompt"
        if tp == "text" and nodes[tn].type in ("music", "tts"):
            tp = "prompt"
        links.append(Link(raw.get("id"), fn, fp, tn, tp))

    return Graph(nodes, links, warnings)


def topo_order(graph):
    """Kahn topological order over non-comment nodes; cycles raise naming the nodes."""
    ids = [nid for nid, n in graph.nodes.items() if not n.spec().get("note")]
    idset = set(ids)
    indeg = {nid: 0 for nid in ids}
    down = {nid: [] for nid in ids}
    seen_edges = set()
    for l in graph.links:
        if l.from_node in idset and l.to_node in idset:
            edge = (l.from_node, l.to_node)
            if edge in seen_edges:
                continue
            seen_edges.add(edge)
            indeg[l.to_node] += 1
            down[l.from_node].append(l.to_node)
    queue = [nid for nid in ids if indeg[nid] == 0]
    order = []
    while queue:
        nid = queue.pop(0)
        order.append(nid)
        for d in down[nid]:
            indeg[d] -= 1
            if indeg[d] == 0:
                queue.append(d)
    if len(order) != len(ids):
        cyclic = sorted(nid for nid in ids if nid not in order)
        names = ", ".join("%s (%s)" % (nid, display_name(graph.node(nid))) for nid in cyclic)
        raise NanoodleError("workflow has a cycle involving: %s" % names)
    return order


def classify_inbound(node, links):
    """Split a node's inbound links into media/text INPUT ports vs FIELD OVERRIDES.

    Declared static ports and dynamic families feed the node's input dict;
    ANY other inbound link overrides the same-named field (that is how wired
    prompt/system/lyrics/q replace typed values). Returns (inputs, overrides)
    as {port_or_field: (from_node, from_port)}.
    """
    spec = NODE_TYPES.get(node.type, {})
    static = set(spec.get("static") or [])
    dynamic = spec.get("dynamic") or []
    inputs, overrides = {}, {}
    for l in links:
        port = l.to_port
        if port in static or any(rx.match(port) for rx in dynamic):
            inputs[port] = (l.from_node, l.from_port)
        else:
            overrides[port] = (l.from_node, l.from_port)
    return inputs, overrides
