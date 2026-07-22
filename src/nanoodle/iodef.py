"""Workflow public interface: derive inputs / outputs / settings and resolve
user-supplied keys. Mirrors play.html deriveInputs / deriveOutputs / deriveSettings."""

from dataclasses import dataclass, field as dc_field
from typing import Any, List, Optional

from .errors import NanoodleError
from .graph import (MAX_FRAMES, NODE_TYPES, display_name, topo_order,
                    wired_frames_floor)


@dataclass
class InputSpec:
    key: str
    node_id: str
    field: str
    kind: str                  # textarea | image | audio | video | choice
    label: str
    optional: bool = False
    default: Optional[str] = None
    options: Optional[List[str]] = None
    node_name: Optional[str] = None   # the node's custom name, if any


@dataclass
class OutputSpec:
    key: str
    node_id: str
    type: str
    ports: List[str] = dc_field(default_factory=list)


@dataclass
class SettingSpec:
    key: str
    node_id: str
    field: str
    kind: str                  # model | number | select | boolean | text | textarea
    label: str
    default: Optional[Any] = None
    options: Optional[List[str]] = None
    node_name: Optional[str] = None
    min: Optional[float] = None
    max: Optional[float] = None


# INPUT_SPECS (play.html 3140-3155) — field, label, kind, optional, default.
INPUT_SPECS = {
    "text":    [("text",   "Text",              "textarea", False, None)],
    "upload":  [("image",  "Image",             "image",    False, None)],
    "aupload": [("audio",  "Audio",             "audio",    False, None)],
    "vupload": [("video",  "Video",             "video",    False, None)],
    "llm":     [("prompt", "Prompt",            "textarea", False, None),
                ("system", "System prompt",     "textarea", True,
                 "You are a helpful, concise assistant.")],
    "image":   [("prompt", "Image prompt",      "textarea", False, None)],
    "tvideo":  [("prompt", "Video prompt",      "textarea", False, None)],
    "music":   [("prompt", "Style / prompt",    "textarea", False, None)],
    "remix":   [("prompt", "Style / direction", "textarea", False, None)],
    "tts":     [("prompt", "Text to speak",     "textarea", False, None)],
}

# SETTING_SPECS (play.html 3234-3310) — per-node knobs that are not IO shape.
_ASPECTS = ["16:9", "9:16", "1:1", "4:3", "3:4"]
_SIZES = ["1024x1024", "1024x1536", "1536x1024", "auto"]   # play.html SIZES verbatim
SETTING_SPECS = {
    "llm": [("model", "Model", "model", None, None),
            ("temperature", "Temperature", "number", "0.8", None),
            ("maxTokens", "Max tokens", "number", None, None),
            ("format", "Output format", "select", "Text", ["Text", "JSON"]),
            ("reasoningEffort", "Reasoning effort", "select", "default",
             ["default", "low", "medium", "high"]),
            ("showThinking", "Show thinking", "boolean", None, None)],
    "vision": [("model", "Model", "model", None, None),
               ("q", "Question", "textarea", "Describe this image.", None)],
    "image": [("model", "Model", "model", None, None),
              ("size", "Image size", "select", "1024x1024", _SIZES),
              ("variations", "Variations", "number", "1", None),
              ("seed", "Seed", "number", None, None)],
    "edit": [("model", "Model", "model", None, None),
             ("prompt", "Edit instruction", "textarea", None, None),
             ("size", "Image size", "select", "1024x1024", _SIZES),
             ("seed", "Seed", "number", None, None)],
    "tvideo": [("model", "Model", "model", None, None),
               ("resolution", "Resolution", "select", "", None),
               ("aspect", "Aspect ratio", "select", "16:9", _ASPECTS),
               ("duration", "Duration", "select", "5", ["5", "10"])],
    "ivideo": [("model", "Model", "model", None, None),
               ("prompt", "Motion prompt", "textarea", None, None),
               ("resolution", "Resolution", "select", "", None),
               ("aspect", "Aspect ratio", "select", "16:9", _ASPECTS),
               ("duration", "Duration", "select", "5", ["5", "10"])],
    "vedit": [("model", "Model", "model", None, None),
              ("prompt", "Edit instruction", "textarea", None, None),
              ("resolution", "Resolution", "select", "", None)],
    "lipsync": [("model", "Model", "model", None, None),
                ("prompt", "Guidance prompt", "textarea", None, None),
                ("resolution", "Resolution", "select", "", None)],
    "music": [("model", "Model", "model", None, None),
              ("lyrics", "Lyrics", "textarea", None, None),
              ("instrumental", "Instrumental", "boolean", None, None),
              ("duration", "Duration (s)", "number", None, None),
              ("negative_prompt", "Negative prompt", "textarea", None, None),
              ("seed", "Seed", "number", None, None)],
    "remix": [("model", "Model", "model", None, None),
              ("lyrics", "Lyrics", "textarea", None, None),
              ("duration", "Duration (s)", "number", None, None)],
    "tts": [("model", "Model", "model", None, None),
            ("voice", "Voice", "text", None, None),
            ("speed", "Speed", "number", "1", None),
            ("instructions", "Voice instructions", "textarea", None, None)],
    "transcribe": [("model", "Model", "model", None, None),
                   ("language", "Language", "text", "auto", None)],
    "join": [("sep", "Separator (use \\n for a line break)", "text", " ", None)],
    "inpaint": [("model", "Model", "model", None, None),
                ("size", "Image size", "select", "1024x1024", _SIZES),
                ("seed", "Seed", "number", None, None)],
    # local media knobs (play.html SETTING_SPECS) — shape-affecting for vframes
    "resize": [("mode", "Mode", "select", "fit", ["fit", "fill", "exact"]),
               ("width", "Width", "number", None, None),
               ("height", "Height", "number", None, None)],
    "vframes": [("dir", "Start from", "select", "end", ["end", "start"]),
                ("frames", "Frames", "number", "1", None),
                ("gap", "Gap (s)", "number", "0.5", None)],
    "combine": [("dedup", "Trim duplicate seam frame", "boolean", True, None)],
    "soundtrack": [("loop", "Loop audio to fill video", "boolean", False, None)],
    "trim": [("start", "Start (s)", "number", "0", None),
             ("length", "Length (s)", "number", "30", None)],
    "extractaudio": [("start", "Start (s)", "number", "0", None),
                     ("length", "Length (s)", "number", None, None)],
}


def _field_default(node, field, spec_default):
    v = node.fields.get(field)
    if v is not None and str(v) != "":
        return v if isinstance(v, str) else v
    return spec_default


def derive_inputs(graph):
    """Inputs = INPUT_SPECS fields not fed by a wire (+ inpaint/choice specials)."""
    out = []
    for node in graph.nodes.values():
        fed = lambda port: graph.port_is_fed(node.id, port)  # noqa: E731
        name = (str(node.name).strip() or None) if node.name else None
        if node.type == "inpaint":
            if not fed("prompt"):
                out.append(InputSpec("", node.id, "prompt", "textarea", "What to paint in",
                                     default=_field_default(node, "prompt", None), node_name=name))
            img_fed, mask_fed = fed("image"), fed("mask")
            if not img_fed:
                out.append(InputSpec("", node.id, "image", "image",
                                     "Image" if mask_fed else "Image — brush the area to repaint",
                                     node_name=name))
            if not mask_fed:
                # derived whenever the mask port is unwired — including the
                # neither-wired case (play.html's combined upload+brush control
                # writes BOTH fields.image and fields.mask); a baked fields.mask
                # satisfies the required-input check.
                out.append(InputSpec("", node.id, "mask", "image", "Mask (white = repaint)",
                                     node_name=name))
            continue
        if node.type == "choice":
            opts = [s.strip() for s in str(node.fields.get("options") or "").split("\n") if s.strip()]
            sel = node.fields.get("selected")
            out.append(InputSpec("", node.id, "selected", "choice", "Choice",
                                 default=sel if sel in opts else (opts[0] if opts else None),
                                 options=opts, node_name=name))
            continue
        for (field, label, kind, optional, spec_def) in INPUT_SPECS.get(node.type, []):
            if fed(field):
                continue  # a wire feeds this field — hide the control
            out.append(InputSpec("", node.id, field, kind, label, optional=optional,
                                 default=_field_default(node, field, spec_def), node_name=name))
    _assign_input_keys(out)
    return out


def _assign_input_keys(inputs):
    """key = friendly name; duplicates get ' 2', ' 3' suffixes (case-insensitive),
    matching JS deriveInputs and SPEC-io's duplicate-key suffixing.

    Friendly name: the node's custom name when the node contributes exactly one
    REQUIRED input (PR #138 flat-label rule), else the generic label.
    """
    per_node = {}
    for spec in inputs:
        per_node.setdefault(spec.node_id, []).append(spec)
    used = {}
    for spec in inputs:
        node_inputs = per_node[spec.node_id]
        required = [s for s in node_inputs if not s.optional]
        if spec.node_name and len(required) == 1 and required[0] is spec:
            cand = spec.node_name
        else:
            cand = spec.label
        low = cand.strip().lower()
        count = used.get(low, 0) + 1
        used[low] = count
        spec.key = cand if count == 1 else "%s %d" % (cand, count)


def derive_outputs(graph):
    """Sinks (non-empty outputs, no outgoing link) keyed by display name;
    duplicates suffixed ' 2', ' 3' in topo order; primary port first."""
    order = topo_order(graph)
    sinks = []
    for nid in order:
        node = graph.node(nid)
        spec = node.spec()
        if not spec.get("outputs"):
            continue
        if graph.outbound(nid):
            continue
        sinks.append(node)
    used = {}
    out = []
    for node in sinks:
        base = display_name(node)
        used[base] = used.get(base, 0) + 1
        key = base if used[base] == 1 else "%s %d" % (base, used[base])
        ports = [p for (p, _kind) in NODE_TYPES.get(node.type, {}).get("outputs", [])]
        # vframes grows frame1..frameN from max(fields.frames, wired floor)
        if node.type == "vframes":
            try:
                authored = max(1, min(MAX_FRAMES, int(node.fields.get("frames") or 1)))
            except (TypeError, ValueError):
                authored = 1
            count = max(authored, wired_frames_floor(graph, node.id))
            ports = ["frame%d" % i for i in range(1, count + 1)]
        out.append(OutputSpec(key, node.id, node.type, ports))
    return out


def derive_settings(graph):
    out = []
    for node in graph.nodes.values():
        name = (str(node.name).strip() or None) if node.name else None
        for (field, label, kind, default, options) in SETTING_SPECS.get(node.type, []):
            if graph.port_is_fed(node.id, field):
                continue  # a knob fed by a link is decided upstream
            # vframes frames is shape-affecting — never offer a floor below the
            # highest wired frameK port (mirrors play.html wiredFramesFloor)
            smin = smax = None
            if node.type == "vframes" and field == "frames":
                smin = max(1, wired_frames_floor(graph, node.id))
                smax = MAX_FRAMES
            out.append(SettingSpec("%s.%s" % (node.id, field), node.id, field, kind, label,
                                   default=_field_default(node, field, default),
                                   options=options, node_name=name,
                                   min=smin, max=smax))
        if node.type == "image" and node.fields.get("model") == "custom-civitai":
            out.append(SettingSpec("%s.customCivitaiAir" % node.id, node.id, "customCivitaiAir",
                                   "text", "CivitAI model", default=node.fields.get("customCivitaiAir"),
                                   node_name=name))
    return out


def _norm(s):
    return str(s).strip().lower()


def resolve_input_key(inputs, key, graph):
    """Resolution order (case-insensitive, trimmed):
    1. the input's assigned key (the very name wf.inputs advertises)
    2. exact node custom name (node has exactly one derived input -> it; else ambiguous)
    3. "nodeId.field", and bare nodeId when the node has a single input
    4. the input's label / field name when unique across inputs
    """
    k = _norm(key)
    if not k:
        raise NanoodleError("empty input name")
    # 1. assigned key (unique modulo case; suffixed keys like "Text 2" live here)
    by_key = [s for s in inputs if _norm(s.key) == k]
    if len(by_key) == 1:
        return by_key[0]
    # 2. custom node name
    named = [s for s in inputs if s.node_name and _norm(s.node_name) == k]
    if named:
        if len(named) == 1:
            return named[0]
        # Several inputs share the node name (e.g. llm prompt + optional system).
        # Prefer the input whose ASSIGNED key is exactly this name (the PR #138
        # flat-label input), then the node's single required input, before
        # declaring ambiguity — otherwise the very key wf.inputs advertises
        # would be unresolvable.
        exact = [s for s in named if _norm(s.key) == k]
        if len(exact) == 1:
            return exact[0]
        required = [s for s in named if not s.optional]
        if len(required) == 1:
            return required[0]
        raise NanoodleError(
            "input name %r is ambiguous — use one of: %s"
            % (key, ", ".join("%s.%s" % (s.node_id, s.field) for s in named)))
    # 3. nodeId.field / bare nodeId
    if "." in k:
        nid, _, fld = k.partition(".")
        hit = [s for s in inputs if _norm(s.node_id) == nid and _norm(s.field) == fld]
        if len(hit) == 1:
            return hit[0]
    by_node = [s for s in inputs if _norm(s.node_id) == k]
    if len(by_node) == 1:
        return by_node[0]
    if len(by_node) > 1:
        raise NanoodleError(
            "input name %r is ambiguous — use one of: %s"
            % (key, ", ".join("%s.%s" % (s.node_id, s.field) for s in by_node)))
    # 4. key / label / field name when unique
    for attr in ("key", "label", "field"):
        hit = [s for s in inputs if _norm(getattr(s, attr)) == k]
        if len(hit) == 1:
            return hit[0]
        if len(hit) > 1:
            raise NanoodleError(
                "input name %r is ambiguous — use one of: %s"
                % (key, ", ".join("%s.%s" % (s.node_id, s.field) for s in hit)))
    raise NanoodleError(
        "unknown input %r — available inputs: %s"
        % (key, ", ".join(sorted(s.key for s in inputs)) or "(none)"))


def resolve_setting_key(settings, key, graph):
    k = _norm(key)
    if "." in k:
        # the dot-form head matches node id, custom name AND display name
        # ("Writer.model" / "Speech.voice"), mirroring JS resolveSettingKey
        nid, _, fld = k.partition(".")
        nodes = [n for n in graph.nodes.values()
                 if _norm(n.id) == nid
                 or (n.name and _norm(n.name) == nid)
                 or _norm(display_name(n)) == nid]
        node_ids = set(n.id for n in nodes)
        hit = [s for s in settings if s.node_id in node_ids and _norm(s.field) == fld]
        if len(hit) == 1:
            return hit[0]
        if len(hit) > 1:
            raise NanoodleError(
                "setting name %r is ambiguous — use one of: %s"
                % (key, ", ".join(s.key for s in hit)))
        # a real node.field that is wired gets a dedicated refusal
        for node in nodes:
            if any(l.to_node == node.id and _norm(l.to_port) == fld for l in graph.links):
                raise NanoodleError(
                    "setting %r is wired — a link decides it upstream, it cannot be overridden" % key)
    named = [s for s in settings
             if (s.node_name and _norm(s.node_name) == k) or _norm(s.key) == k]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        raise NanoodleError(
            "setting name %r is ambiguous — use one of: %s"
            % (key, ", ".join(s.key for s in named)))
    hit = [s for s in settings if _norm(s.field) == k or _norm(s.label) == k]
    if len(hit) == 1:
        return hit[0]
    if len(hit) > 1:
        raise NanoodleError(
            "setting name %r is ambiguous — use one of: %s"
            % (key, ", ".join(s.key for s in hit)))
    raise NanoodleError(
        "unknown setting %r — available settings: %s"
        % (key, ", ".join(sorted(s.key for s in settings)) or "(none)"))
