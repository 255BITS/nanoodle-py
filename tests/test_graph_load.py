"""Graph loading: aliases, link migration, orphan drop, unknown types, topo
order, cycle errors, comment nodes (SPEC-format loader semantics)."""

import unittest

from tests import fixture

from nanoodle import NanoodleError, UnsupportedNodeError, Workflow
from nanoodle.graph import display_name, materialize, topo_order


class LoaderSemanticsTest(unittest.TestCase):
    def test_audio_alias_and_text_port_migration(self):
        wf = Workflow.from_dict({
            "nodes": [
                {"id": "n1", "type": "text", "fields": {"text": "hi"}},
                {"id": "n2", "type": "audio", "fields": {"model": "m"}},  # legacy alias
            ],
            "links": [
                {"id": "l1", "from": {"node": "n1", "port": "text"},
                 "to": {"node": "n2", "port": "text"}},  # legacy port
            ],
        }, api_key="k")
        self.assertEqual(wf.graph.node("n2").type, "tts")
        self.assertEqual(wf.graph.links[0].to_port, "prompt")  # migrated

    def test_music_text_port_migration(self):
        wf = Workflow.from_dict({
            "nodes": [
                {"id": "n1", "type": "text", "fields": {"text": "hi"}},
                {"id": "n2", "type": "music", "fields": {"model": "m"}},
            ],
            "links": [{"id": "l1", "from": {"node": "n1", "port": "text"},
                       "to": {"node": "n2", "port": "text"}}],
        }, api_key="k")
        self.assertEqual(wf.graph.links[0].to_port, "prompt")

    def test_llm_text_port_not_migrated(self):
        # the migration is scoped to music/tts targets only
        wf = Workflow.from_dict({
            "nodes": [
                {"id": "n1", "type": "text", "fields": {"text": "hi"}},
                {"id": "n2", "type": "llm", "fields": {"model": "m", "prompt": "p"}},
            ],
            "links": [{"id": "l1", "from": {"node": "n1", "port": "text"},
                       "to": {"node": "n2", "port": "text"}}],
        }, api_key="k")
        self.assertEqual(wf.graph.links[0].to_port, "text")

    def test_orphaned_links_dropped_and_minimal_form(self):
        wf = Workflow.from_dict({"nodes": [{"id": "n1", "type": "text", "fields": {}}],
                                 "links": [{"id": "l1", "from": {"node": "nX", "port": "text"},
                                            "to": {"node": "n1", "port": "text"}}]}, api_key="k")
        self.assertEqual(wf.graph.links, [])
        # {nodes} only (no links/v/nid/lid/view) is a valid save
        wf2 = Workflow.from_dict({"nodes": [{"id": "n1", "type": "text",
                                             "fields": {"text": "x"}}]}, api_key="k")
        self.assertEqual(wf2.graph.node("n1").fields["text"], "x")
        self.assertEqual(wf2.graph.links, [])

    def test_layout_and_editor_keys_ignored(self):
        wf = Workflow.load(fixture("starter-graph.json"), api_key="k")
        self.assertEqual(wf.warnings, [])
        self.assertEqual(list(wf.graph.nodes), ["n1", "n2", "n3"])
        self.assertEqual(len(wf.graph.links), 2)

    def test_unknown_type_warns_on_load_fails_on_run(self):
        wf = Workflow.from_dict({"nodes": [{"id": "n1", "type": "flurble", "fields": {}}]},
                                api_key="k")
        self.assertTrue(any("flurble" in w for w in wf.warnings))
        with self.assertRaises(UnsupportedNodeError) as ctx:
            wf.run()
        self.assertIn("flurble", str(ctx.exception))
        self.assertEqual(ctx.exception.node_id, "n1")

    def test_draw_node_now_fails_as_unknown_type(self):
        # draw was removed (NanoGPT retired the chat-image contract) — a graph
        # carrying one must warn on load and fail fast BEFORE any API spend.
        wf = Workflow.from_dict({"nodes": [
            {"id": "n1", "type": "draw", "fields": {"model": "m", "prompt": "p"}}]},
            api_key="k")
        self.assertTrue(any("draw" in w for w in wf.warnings))
        with self.assertRaises(UnsupportedNodeError) as ctx:
            wf.run()
        self.assertEqual(ctx.exception.node_id, "n1")

    def test_node_without_id_skipped_with_warning(self):
        wf = Workflow.from_dict({"nodes": [
            {"type": "text", "fields": {"text": "lost"}},
            {"id": "n1", "type": "text", "fields": {"text": "kept"}},
        ]}, api_key="k")
        self.assertEqual(list(wf.graph.nodes), ["n1"])
        self.assertTrue(any("no id" in w for w in wf.warnings))

    def test_non_object_json_rejected(self):
        with self.assertRaises(NanoodleError):
            materialize([1, 2, 3])

    def test_json_string_accepted(self):
        wf = Workflow.from_dict('{"nodes": [{"id":"n1","type":"text","fields":{"text":"x"}}]}',
                                api_key="k")
        self.assertEqual(wf.graph.node("n1").fields["text"], "x")

    def test_display_name_resolution(self):
        wf = Workflow.from_dict({"nodes": [
            {"id": "n1", "type": "llm", "fields": {}, "name": "  Poet  "},
            {"id": "n2", "type": "llm", "fields": {}},
            {"id": "n3", "type": "llm", "fields": {}, "name": "   "},
        ]}, api_key="k")
        self.assertEqual(display_name(wf.graph.node("n1")), "Poet")   # custom, trimmed
        self.assertEqual(display_name(wf.graph.node("n2")), "LLM")    # type title
        self.assertEqual(display_name(wf.graph.node("n3")), "LLM")    # blank name -> title


class TopoOrderTest(unittest.TestCase):
    def test_topo_order_respects_deps(self):
        wf = Workflow.load(fixture("starter-graph.json"), api_key="k")
        self.assertEqual(topo_order(wf.graph), ["n1", "n2", "n3"])

    def test_cycle_error_names_nodes(self):
        wf = Workflow.load(fixture("cycle.json"), api_key="k")
        with self.assertRaises(NanoodleError) as ctx:
            wf.run()
        msg = str(ctx.exception)
        self.assertIn("cycle", msg)
        self.assertIn("n1", msg)
        self.assertIn("n2", msg)

    def test_two_links_between_same_pair(self):
        # text feeding two fields of the same node is one topo edge, both overrides apply
        wf = Workflow.from_dict({"nodes": [
            {"id": "n1", "type": "text", "fields": {"text": "hi"}},
            {"id": "n2", "type": "join", "fields": {"sep": "+"}},
        ], "links": [
            {"id": "l1", "from": {"node": "n1", "port": "text"}, "to": {"node": "n2", "port": "a"}},
            {"id": "l2", "from": {"node": "n1", "port": "text"}, "to": {"node": "n2", "port": "b"}},
        ]})
        self.assertEqual(topo_order(wf.graph), ["n1", "n2"])
        self.assertEqual(wf.run()["Join"], "hi+hi")

    def test_comment_nodes_never_run_but_are_recorded_skipped(self):
        # cross-language parity: comment nodes appear in result.nodes with
        # status 'skipped' (never executed), same as the JS library
        wf = Workflow.from_dict({"nodes": [
            {"id": "n1", "type": "text", "fields": {"text": "hi"}},
            {"id": "n2", "type": "comment", "fields": {"text": "a note"}},
        ]}, api_key="k")
        result = wf.run()
        self.assertIn("n2", result.nodes)
        self.assertEqual(result.nodes["n2"].status, "skipped")
        self.assertIsNone(result.nodes["n2"].out)
        self.assertIsNone(result.nodes["n2"].error)
        self.assertEqual(result["Text"], "hi")


if __name__ == "__main__":
    unittest.main()
