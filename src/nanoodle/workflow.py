"""Workflow: load a noodle-graph.json save and re-execute it against NanoGPT."""

import copy
import json
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from .engine import Engine
from .errors import NanoodleError, RunError, UnsupportedNodeError
from .graph import (NODE_TYPES, classify_inbound, display_name, materialize,
                    topo_order)
from .iodef import (derive_inputs, derive_outputs, derive_settings,
                    resolve_input_key, resolve_setting_key)
from .media import MEDIA_INLINE_MAX, MediaRef, make_data_url
from .share import decode_share_url, is_share_ref
from .transport import default_http, resolve_api_key
from .x402 import assert_payment_option

_UNSUPPORTED_MSG = ("node type '%s' does local media processing that requires the "
                    "nanoodle browser app; not supported by this library yet")


class NodeRun(object):
    """Per-node run record: status ('done'|'error'|'skipped'), out, error, cost, ms."""

    __slots__ = ("status", "out", "error", "cost_usd", "ms")

    def __init__(self):
        self.status = "pending"
        self.out = None
        self.error = None
        self.cost_usd = None
        self.ms = None

    def __repr__(self):
        return "NodeRun(status=%r, error=%r, cost_usd=%r, ms=%r)" % (
            self.status, self.error, self.cost_usd, self.ms)


class RunResult(object):
    def __init__(self, outputs, nodes, errors, cost_usd, cost_exact, remaining_balance):
        self.outputs = outputs                    # friendly key AND node-id key -> value
        self.nodes = nodes                        # node id -> NodeRun
        self.errors = errors                      # [{node_id, name, message}]
        self.cost_usd = cost_usd
        self.cost_exact = cost_exact
        self.remaining_balance = remaining_balance

    def __getitem__(self, key):
        if key in self.outputs:
            return self.outputs[key]
        for k in self.outputs:
            if k.strip().lower() == str(key).strip().lower():
                return self.outputs[k]
        raise KeyError("no output %r — available: %s" % (key, ", ".join(sorted(self.outputs))))

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key):
        return self.get(key, _MISSING) is not _MISSING

    def __repr__(self):
        return "RunResult(outputs=%r, cost_usd=%r, errors=%d)" % (
            sorted(self.outputs), self.cost_usd, len(self.errors))


_MISSING = object()


class Workflow(object):
    """A loaded nanoodle workflow (the downloadable noodle-graph.json save).

    >>> wf = Workflow.load("noodle-graph.json", api_key="...")
    >>> result = wf.run({"Text": "a cozy ramen shop"})
    >>> result["Image"].save("out.png")
    """

    def __init__(self, data, api_key=None, base_url="https://nano-gpt.com",
                 http=None, poll_intervals=None, timeouts=None, payment=None):
        self.warnings = []
        self.graph = materialize(data, self.warnings)
        # accountless x402: a callback that sends the Nano invoice (never a seed).
        # api_key="" stays explicitly keyless — only None falls back to the env var.
        assert_payment_option(payment)
        self._payment = payment
        self._api_key = resolve_api_key(api_key)
        self.base_url = base_url
        self.http = http or default_http
        self.poll_intervals = poll_intervals or {}
        self.timeouts = timeouts or {}
        self._inputs = None
        self._outputs = None
        self._settings = None

    # ---- constructors -------------------------------------------------------

    @classmethod
    def load(cls, src, **opts):
        """Load a workflow from a noodle-graph.json file on disk, or from any
        nanoodle share link — a full URL (nanoodle.com/#g=…, /play.html#a=…, a
        da.gd/TinyURL short link) or a bare #g=/#j=/#a= fragment. Direct
        fragment links decode offline; only fragment-less short links touch the
        network (redirect-header reads, no credentials attached).

        Share options ``opener`` (injectable redirect hook) and ``max_hops``
        are consumed here and never reach the constructor.
        """
        if is_share_ref(src):
            share_opts = {}
            if "opener" in opts:
                share_opts["opener"] = opts.pop("opener")
            if "max_hops" in opts:
                share_opts["max_hops"] = opts.pop("max_hops")
            graph = decode_share_url(src, **share_opts)["graph"]
            return cls(graph, **opts)
        with open(src, "r", encoding="utf-8") as f:
            return cls(json.load(f), **opts)

    @classmethod
    def from_dict(cls, data, **opts):
        """Accepts a parsed dict or a JSON string."""
        if isinstance(data, str):
            data = json.loads(data)
        return cls(data, **opts)

    # ---- public interface ---------------------------------------------------

    @property
    def inputs(self):
        if self._inputs is None:
            self._inputs = derive_inputs(self.graph)
        return self._inputs

    @property
    def outputs(self):
        if self._outputs is None:
            self._outputs = derive_outputs(self.graph)
        return self._outputs

    @property
    def settings(self):
        if self._settings is None:
            self._settings = derive_settings(self.graph)
        return self._settings

    def check_balance(self):
        """Optional helper: POST /api/check-balance -> usd balance (float)."""
        engine = self._make_engine(None)
        resp = engine._post_json("/api/check-balance", {})
        j = json.loads(resp.text())
        return float(j.get("usd_balance")) if j.get("usd_balance") is not None else None

    # ---- run ----------------------------------------------------------------

    def run(self, inputs=None, settings=None, timeout=None, on_progress=None):
        graph = self._copy_graph()

        # 1. upfront input validation & application
        specs = self.inputs
        supplied = self._normalize_inputs(inputs, specs)
        explicit = set()   # id() of every spec the caller supplied a value for
        for key, value in supplied.items():
            spec = resolve_input_key(specs, key, graph)
            graph.node(spec.node_id).fields[spec.field] = self._coerce_input(spec, value)
            explicit.add(id(spec))
        for key, value in (settings or {}).items():
            sspec = resolve_setting_key(self.settings, key, graph)
            graph.node(sspec.node_id).fields[sspec.field] = value

        # 2. backfill spec defaults into empty fields (play.html run() applies
        #    it.def — e.g. the llm default system prompt), then error on missing
        #    required inputs
        missing = []
        for spec in specs:
            fields = graph.node(spec.node_id).fields
            v = fields.get(spec.field)
            if v is None or str(v).strip() == "":
                # an EXPLICIT empty value clears an optional input (e.g. run with
                # no system prompt) — the default only backfills when the key
                # wasn't supplied at all (the app's prefilled textarea)
                if spec.optional and id(spec) in explicit:
                    continue
                if spec.default is not None and str(spec.default) != "":
                    fields[spec.field] = spec.default
                elif not spec.optional and spec.kind != "choice":
                    # choice falls back to the first option at run time
                    missing.append(spec.key)
        if missing:
            raise NanoodleError("missing required input%s: %s"
                                % ("s" if len(missing) > 1 else "", ", ".join(missing)))

        # 3. fail fast BEFORE spending: unsupported/unknown node types, API key
        has_network = False
        for node in graph.nodes.values():
            tspec = NODE_TYPES.get(node.type)
            if tspec is None:
                raise UnsupportedNodeError(
                    node.id, node.type,
                    "unknown node type '%s' (node %s '%s') — this workflow needs a newer "
                    "library or the nanoodle browser app" % (node.type, node.id, display_name(node)))
            if tspec.get("unsupported"):
                raise UnsupportedNodeError(
                    node.id, node.type,
                    (_UNSUPPORTED_MSG % node.type) + " (node %s '%s')" % (node.id, display_name(node)))
            if tspec.get("network"):
                has_network = True
        if has_network and not self._api_key and self._payment is None:
            raise NanoodleError("no API key — pass api_key=, set the NANOGPT_API_KEY "
                                "environment variable, or pass payment= for accountless "
                                "x402 runs (this workflow calls NanoGPT)")

        order = topo_order(graph)  # raises on cycles, naming the cyclic nodes
        return self._execute(graph, order, timeout, on_progress)

    # ---- internals ------------------------------------------------------------

    def _make_engine(self, on_progress):
        return Engine(self._api_key, self.base_url, self.http,
                      poll_intervals=self.poll_intervals, timeouts=self.timeouts,
                      on_progress=on_progress, payment=self._payment)

    def _copy_graph(self):
        g = materialize({"nodes": [], "links": []})
        g.nodes = {nid: copy.deepcopy(n) for nid, n in self.graph.nodes.items()}
        g.links = list(self.graph.links)
        g.warnings = list(self.graph.warnings)
        return g

    def _normalize_inputs(self, inputs, specs):
        if inputs is None:
            return {}
        if isinstance(inputs, dict):
            return inputs
        # bare scalar: allowed when the workflow has exactly one required input
        required = [s for s in specs if not s.optional]
        if len(required) == 1:
            return {required[0].key: inputs}
        raise NanoodleError(
            "a bare input value needs a workflow with exactly one required input — "
            "this one has %d (%s); pass a dict instead"
            % (len(required), ", ".join(s.key for s in required) or "none"))

    @staticmethod
    def _coerce_input(spec, value):
        if isinstance(value, MediaRef):
            value = value.url
        elif isinstance(value, (bytes, bytearray)):
            value = make_data_url(bytes(value))
        elif isinstance(value, dict) and "data" in value:
            value = make_data_url(value["data"], value.get("mime"))
        if spec.kind == "choice":
            v = str(value)
            if spec.options and v not in spec.options:
                raise NanoodleError("invalid choice %r for %s — options: %s"
                                    % (v, spec.key, ", ".join(spec.options)))
            return v
        if spec.kind in ("image", "audio", "video"):
            if not isinstance(value, str):
                raise NanoodleError(
                    "input %s expects media: pass a data:/https URL string, bytes, "
                    "{'data': bytes, 'mime': ...} or media_from_file(path)" % spec.key)
            # a bare filename/path would ride verbatim into a PAID request body —
            # refuse anything that isn't a data:/http(s) URL before spending
            if not re.match(r"^(data:|https?:)", value, re.I):
                raise NanoodleError(
                    "input %s: expected a data: URL, an http(s) URL, bytes, or "
                    "media_from_file(path) — got a plain string. For a local file use "
                    "media_from_file(%r)." % (spec.key, value[:60]))
            if value[:5].lower() == "data:" and len(value) > MEDIA_INLINE_MAX:
                raise NanoodleError(
                    "input %s: media is too large to send inline (~4 MB max). nanoodle "
                    "sends media as base64 in the request body (NanoGPT has no upload "
                    "endpoint) — use a smaller file." % spec.key)
            return value
        return str(value)

    def _execute(self, graph, order, timeout, on_progress):
        deadline = (time.monotonic() + timeout) if timeout else None
        runs = {nid: NodeRun() for nid in order}
        for nid, n in graph.nodes.items():
            # comment nodes never run but ARE recorded (status 'skipped'),
            # matching the JS result.nodes shape
            if nid not in runs and n.spec().get("note"):
                rec = NodeRun()
                rec.status = "skipped"
                runs[nid] = rec
        lock = threading.Lock()
        cost = {"total": 0.0, "exact": True, "balance": None, "any": False}

        def progress(evt):
            if on_progress:
                try:
                    on_progress(evt)
                except Exception:
                    pass

        engine = self._make_engine(progress)

        deps = {nid: set() for nid in order}
        for link in graph.links:
            if link.from_node in deps and link.to_node in deps:
                deps[link.to_node].add(link.from_node)

        def make_on_cost(nid):
            def on_cost(usd, balance):
                with lock:
                    cost["any"] = True
                    if usd is None:
                        cost["exact"] = False
                    else:
                        cost["total"] += usd
                        run = runs[nid]
                        run.cost_usd = (run.cost_usd or 0.0) + usd
                    if balance is not None:
                        cost["balance"] = balance
            return on_cost

        def exec_node(nid):
            node = graph.node(nid)
            name = display_name(node)
            progress({"type": "node-start", "node_id": nid, "name": name})
            t0 = time.monotonic()
            inputs, overrides = classify_inbound(node, graph.inbound(nid))
            inp = {}
            for port, (src, sport) in inputs.items():
                out = runs[src].out or {}
                inp[port] = out.get(sport)
            if overrides:
                node = copy.deepcopy(node)
                for port, (src, sport) in overrides.items():
                    v = (runs[src].out or {}).get(sport)
                    if v is None:
                        continue  # play.html: if(v!=null) — a null upstream value
                                  # leaves the typed field value in effect
                    node.fields[port] = v.url if isinstance(v, MediaRef) else v
            out = engine.run_node(node, inp, make_on_cost(nid))
            return out, time.monotonic() - t0

        pool = ThreadPoolExecutor(max_workers=max(1, min(8, len(order))))
        pending = {}   # future -> node id
        settled = set()
        abandoned = False   # deadline hit with nodes still in flight
        try:
            while len(settled) < len(order):
                timed_out = deadline is not None and time.monotonic() > deadline
                progressed = True
                while progressed:
                    progressed = False
                    for nid in order:
                        if nid in settled or nid in pending.values():
                            continue
                        if not deps[nid] <= settled:
                            continue
                        run = runs[nid]
                        failed_dep = next((d for d in deps[nid]
                                           if runs[d].status == "error"), None)
                        if timed_out:
                            # timeout wins the message — an upstream marked
                            # failed BY the timeout must not relabel this node
                            run.status = "error"
                            run.error = "run timed out after %ss" % timeout
                            settled.add(nid)
                            progressed = True
                        elif failed_dep is not None:
                            run.status = "error"
                            run.error = "upstream failed: " + display_name(graph.node(failed_dep))
                            settled.add(nid)
                            progress({"type": "node-error", "node_id": nid,
                                      "name": display_name(graph.node(nid)), "error": run.error})
                            progressed = True
                        else:
                            fut = pool.submit(exec_node, nid)
                            pending[fut] = nid
                if not pending:
                    if len(settled) < len(order):
                        break  # nothing runnable left (should not happen: topo checked)
                    continue
                remaining = None
                if deadline is not None:
                    remaining = max(0.0, deadline - time.monotonic())
                done, _ = wait(list(pending.keys()), timeout=remaining,
                               return_when=FIRST_COMPLETED)
                if not done:
                    # deadline expired while nodes were in flight: reflect the
                    # timeout in the result NOW; the worker threads are left to
                    # finish in the pool but their results are discarded.
                    abandoned = True
                    for fut, nid in list(pending.items()):
                        run = runs[nid]
                        run.status = "error"
                        run.error = "run timed out after %ss" % timeout
                        settled.add(nid)
                        progress({"type": "node-error", "node_id": nid,
                                  "name": display_name(graph.node(nid)),
                                  "error": run.error})
                    pending.clear()
                    continue  # the scheduling pass marks the rest timed out
                for fut in done:
                    nid = pending.pop(fut)
                    run = runs[nid]
                    node = graph.node(nid)
                    try:
                        out, secs = fut.result()
                        run.status = "done"
                        run.out = out
                        run.ms = int(secs * 1000)
                        progress({"type": "node-done", "node_id": nid,
                                  "name": display_name(node), "ms": run.ms,
                                  "cost_usd": run.cost_usd})
                    except Exception as e:  # noqa: BLE001 - collected per node
                        run.status = "error"
                        run.error = str(e)
                        progress({"type": "node-error", "node_id": nid,
                                  "name": display_name(node), "error": run.error})
                    settled.add(nid)
        finally:
            # after a timeout, do NOT block on in-flight nodes — return the
            # timed-out result promptly and let the threads drain in background
            pool.shutdown(wait=not abandoned)

        # ---- assemble result -------------------------------------------------
        outputs = {}
        failed_sinks = []
        out_specs = derive_outputs(graph)
        for ospec in out_specs:
            run = runs.get(ospec.node_id)
            if run is not None and run.status == "done":
                primary = NODE_TYPES[ospec.type]["outputs"][0][0]
                value = (run.out or {}).get(primary)
                outputs[ospec.key] = value
                outputs[ospec.node_id] = value
            else:
                failed_sinks.append((ospec, run))
        errors = [{"node_id": nid, "name": display_name(graph.node(nid)),
                   "message": runs[nid].error}
                  for nid in order if runs[nid].status == "error"]
        result = RunResult(outputs, runs, errors,
                           cost_usd=cost["total"],
                           cost_exact=cost["exact"],
                           remaining_balance=cost["balance"])
        if failed_sinks:
            parts = []
            for ospec, run in failed_sinks:
                parts.append("%s (%s)" % (ospec.key, (run.error if run else None) or "did not run"))
            raise RunError("output node%s failed: %s"
                           % ("s" if len(failed_sinks) > 1 else "", "; ".join(parts)), result)
        return result
