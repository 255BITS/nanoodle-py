"""CLI: python -m nanoodle run|inspect graph.json [...]

  python -m nanoodle inspect graph.json
  python -m nanoodle run graph.json --input Text="a cozy ramen shop" \
      --input n2.system=@sys.txt --set n3.size=1k --out ./out [--json]

The graph argument is also "the URL is the package": pass any nanoodle share
link where a graph.json path is accepted — a full URL, a bare #g=/#j=/#a=
fragment, or a da.gd/TinyURL short link. Quote the URL — # starts a comment in
most shells.

  python -m nanoodle inspect "https://nanoodle.com/#g=..."
  python -m nanoodle run "https://nanoodle.com/play.html#a=..." --input Text=hi
"""

import argparse
import json
import os
import re
import sys

from . import MediaRef, NanoodleError, Workflow, __version__, media_from_file

_MEDIA_EXT = re.compile(r"\.(png|jpe?g|gif|webp|bmp|mp3|wav|ogg|oga|opus|flac|aac|m4a|mp4|webm|mov)$", re.I)


def _parse_kv(pairs, what):
    out = {}
    for item in pairs or []:
        if "=" not in item:
            raise NanoodleError("bad --%s %r — expected NAME=VALUE" % (what, item))
        key, _, value = item.partition("=")
        if value.startswith("@"):
            path = value[1:]
            if _MEDIA_EXT.search(path):
                out[key] = media_from_file(path)
            else:
                with open(path, "r", encoding="utf-8") as f:
                    out[key] = f.read()
        else:
            out[key] = value
    return out


def _load_env_file(path):
    """Load KEY=VALUE lines (.env style) into os.environ; existing env wins."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        raise NanoodleError("cannot read --env-file %r: %s" % (path, e.strerror or e))
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.startswith("export "):
            key = key[len("export "):]
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _fmt_default(v):
    if v is None:
        return ""
    v = str(v)
    return v if len(v) <= 48 else v[:45] + "..."


_MEDIA_INPUT_KINDS = ("image", "audio", "video")


def _fmt_media_default(v, kind):
    """Summarize a media default instead of echoing base64 (data: URLs run to megabytes)."""
    s = str(v)
    if s.lower().startswith("data:"):
        mime = s[5:].split(";", 1)[0].split(",", 1)[0] or kind
        size = len(s)
        human = "%.1f MB" % (size / 1048576.0) if size >= 1048576 else "%d KB" % max(1, size // 1024)
        return "prefilled inline %s (%s)" % (mime, human)
    return "prefilled %s" % _fmt_default(s)


def cmd_inspect(args):
    wf = Workflow.load(args.graph, api_key=args.api_key or "unused-for-inspect")
    for w in wf.warnings:
        print("warning: %s" % w, file=sys.stderr)
    print("Inputs:")
    for s in wf.inputs:
        bits = ["  %-24s %s.%s  kind=%s" % (s.key, s.node_id, s.field, s.kind)]
        if s.optional:
            bits.append("optional")
        if s.default:
            bits.append(_fmt_media_default(s.default, s.kind) if s.kind in _MEDIA_INPUT_KINDS
                        else "default=%r" % _fmt_default(s.default))
        elif s.kind in _MEDIA_INPUT_KINDS and not s.optional:
            bits.append('required — supply: --input "%s=@file"' % s.key)
        if s.options:
            bits.append("options=%s" % "|".join(s.options))
        print("  ".join(bits))
    if not wf.inputs:
        print("  (none)")
    print("Outputs:")
    for o in wf.outputs:
        print("  %-24s %s  type=%s  ports=%s" % (o.key, o.node_id, o.type, ",".join(o.ports)))
    if not wf.outputs:
        print("  (none)")
    print("Settings:")
    for s in wf.settings:
        line = "  %-24s kind=%s" % (s.key, s.kind)
        if s.default not in (None, ""):
            line += "  current=%r" % _fmt_default(s.default)
        print(line)
    if not wf.settings:
        print("  (none)")
    print("Nodes:")
    for node in wf.graph.nodes.values():
        from .graph import display_name
        print("  %-6s %-12s %s" % (node.id, node.type, display_name(node)))
    return 0


def _save_outputs(result, keys, out_dir):
    saved = {}
    os.makedirs(out_dir, exist_ok=True)
    for key in keys:
        value = result.outputs.get(key)
        if isinstance(value, MediaRef):
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", key).strip("_") or "output"
            path = os.path.join(out_dir, "%s.%s" % (safe, value.suggested_extension()))
            value.save(path)
            saved[key] = path
    return saved


def _pay_printer(inv):
    # accountless x402: print the Nano invoice on stderr and let the engine wait
    # for the deposit. The send happens in the user's own wallet — never here.
    print("", file=sys.stderr)
    usd = " (~$%s)" % inv["amountUsd"] if inv.get("amountUsd") is not None else ""
    print("⚡ payment required: %s%s" % (inv.get("amount") or inv.get("amountRaw") + " raw", usd), file=sys.stderr)
    print("send with your Nano wallet:", file=sys.stderr)
    print("  %s" % inv.get("payTo"), file=sys.stderr)
    print("  %s" % inv.get("uri"), file=sys.stderr)
    if inv.get("explorerUrl"):
        print("  explorer: %s" % inv["explorerUrl"], file=sys.stderr)
    print("waiting for the deposit… (Ctrl-C aborts)\n", file=sys.stderr)


def cmd_run(args):
    # --pay = accountless x402: the key is deliberately dropped (api_key="" stays
    # explicitly keyless — None would fall back to $NANOGPT_API_KEY and charge an account).
    pay = getattr(args, "pay", False)
    wf = Workflow.load(args.graph,
                       api_key="" if pay else args.api_key,
                       payment=_pay_printer if pay else None,
                       base_url=args.base_url or "https://nano-gpt.com")
    inputs = _parse_kv(args.input, "input")
    settings = _parse_kv(args.set, "set") or None

    def progress(evt):
        if args.json:
            return
        if evt["type"] == "node-start":
            print("▶ %s (%s)" % (evt["name"], evt["node_id"]), file=sys.stderr)
        elif evt["type"] == "node-done":
            print("✓ %s — %d ms" % (evt["name"], evt.get("ms") or 0), file=sys.stderr)
        elif evt["type"] == "node-error":
            print("✗ %s — %s" % (evt["name"], evt.get("error")), file=sys.stderr)

    result = wf.run(inputs, settings=settings, timeout=args.timeout, on_progress=progress)

    friendly = [o.key for o in wf.outputs]
    saved = _save_outputs(result, friendly, args.out) if args.out else {}
    if args.json:
        payload = {"outputs": {}, "costUsd": result.cost_usd, "costExact": result.cost_exact,
                   "remainingBalance": result.remaining_balance,
                   "nodes": {nid: {"status": r.status, "error": r.error,
                                   "costUsd": r.cost_usd, "ms": r.ms}
                             for nid, r in result.nodes.items()},
                   "errors": result.errors}
        for key in friendly:
            value = result.outputs.get(key)
            if isinstance(value, MediaRef):
                payload["outputs"][key] = {"url": value.url if not args.out else None,
                                           "mime": value.mime, "file": saved.get(key)}
            else:
                payload["outputs"][key] = value
        print(json.dumps(payload, indent=2))
    else:
        for key in friendly:
            value = result.outputs.get(key)
            if isinstance(value, MediaRef):
                print("%s: %s" % (key, saved.get(key) or value.url[:96]))
            else:
                print("%s:\n%s" % (key, value))
        approx = "" if result.cost_exact else "~"
        cost_line = "cost: %s$%.4f" % (approx, result.cost_usd)
        if result.remaining_balance is not None:
            cost_line += " · balance: $%s" % result.remaining_balance
        print(cost_line, file=sys.stderr)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="nanoodle",
                                     description="Run or inspect a nanoodle workflow save")
    parser.add_argument("--version", action="version", version="nanoodle " + __version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="execute a workflow")
    p_run.add_argument("graph", metavar="GRAPH",
                       help="a noodle-graph.json save, or a nanoodle share link "
                            "(URL or #g=/#j=/#a= fragment)")
    p_run.add_argument("--input", action="append", metavar="NAME=VALUE",
                       help="input value; @file reads a file (repeatable)")
    p_run.add_argument("--set", action="append", metavar="NODE.FIELD=VALUE",
                       help="setting override (repeatable)")
    p_run.add_argument("--out", metavar="DIR", help="save media outputs into DIR")
    p_run.add_argument("--json", action="store_true", help="print a machine-readable result")
    p_run.add_argument("--api-key", default=None, help="NanoGPT API key (default: $NANOGPT_API_KEY)")
    p_run.add_argument("--env-file", default=None, metavar="PATH",
                       help="read NANOGPT_API_KEY (and other vars) from a .env-style file; "
                            "existing environment variables win")
    p_run.add_argument("--base-url", default=None)
    p_run.add_argument("--pay", action="store_true",
                       help="accountless run — no API key or account: each paid call prints a "
                            "Nano (XNO) invoice (nano: URI + address) on stderr and waits for "
                            "the deposit (x402; ignores any configured key; your own "
                            "self-custody wallet does the send)")
    p_run.add_argument("--timeout", type=float, default=None, help="overall run timeout (seconds)")
    p_run.set_defaults(fn=cmd_run)

    p_ins = sub.add_parser("inspect", help="print inputs/outputs/settings + node table")
    p_ins.add_argument("graph", metavar="GRAPH",
                       help="a noodle-graph.json save, or a nanoodle share link "
                            "(URL or #g=/#j=/#a= fragment)")
    p_ins.add_argument("--api-key", default=None)
    p_ins.add_argument("--env-file", default=None, metavar="PATH",
                       help="read NANOGPT_API_KEY (and other vars) from a .env-style file; "
                            "existing environment variables win")
    p_ins.set_defaults(fn=cmd_inspect)

    args = parser.parse_args(argv)
    try:
        if getattr(args, "env_file", None):
            _load_env_file(args.env_file)
        return args.fn(args)
    except NanoodleError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
