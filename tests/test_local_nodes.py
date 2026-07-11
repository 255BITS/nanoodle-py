"""Local (no-network) nodes, upfront validation, media-input coercion, and the
unsupported/unknown-node fail-fast contract (zero network calls before it)."""

import os
import unittest
import unittest.mock as mock

from tests import fixture
from tests._util import tripwire_http

from nanoodle import (MediaRef, NanoodleError, UnsupportedNodeError, Workflow,
                      media_from_file)
from nanoodle.graph import UNSUPPORTED_TYPES

_UNSUPPORTED_STATIC = {   # a port the fixture graph can leave unwired
    "resize": {"mode": "fit"}, "vframes": {"frames": "2"}, "combine": {},
    "soundtrack": {}, "trim": {"start": "0"}, "extractaudio": {},
}


class LocalNodesTest(unittest.TestCase):
    def test_join_choice_chain_runs_offline(self):
        wf = Workflow.load(fixture("join-choice.json"), api_key=None)
        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("NANOGPT_API_KEY", None)
            result = wf.run()   # local-only graph needs no API key
        self.assertEqual(result["Join"], "hello - blue")
        result2 = wf.run({"Choice": "green", "Text": "hey"})
        self.assertEqual(result2["Join"], "hey - green")

    def test_join_sep_backslash_n_and_empty_filter(self):
        # n2 is an inputless join -> emits "" at runtime, which b must filter out
        wf = Workflow.from_dict({"nodes": [
            {"id": "n1", "type": "text", "fields": {"text": "a"}},
            {"id": "n2", "type": "join", "fields": {}},
            {"id": "n3", "type": "join", "fields": {"sep": "\\n"}},
        ], "links": [
            {"id": "l1", "from": {"node": "n1", "port": "text"}, "to": {"node": "n3", "port": "a"}},
            {"id": "l2", "from": {"node": "n2", "port": "text"}, "to": {"node": "n3", "port": "b"}},
        ]})
        self.assertEqual(wf.run()["Join"], "a")   # empty b filtered, literal \n sep

    def test_join_default_space_separator(self):
        wf = Workflow.from_dict({"nodes": [
            {"id": "n1", "type": "text", "fields": {"text": "a"}},
            {"id": "n2", "type": "text", "fields": {"text": "b"}},
            {"id": "n3", "type": "join", "fields": {}},
        ], "links": [
            {"id": "l1", "from": {"node": "n1", "port": "text"}, "to": {"node": "n3", "port": "a"}},
            {"id": "l2", "from": {"node": "n2", "port": "text"}, "to": {"node": "n3", "port": "b"}},
        ]})
        self.assertEqual(wf.run()["Join"], "a b")

    def test_join_newline_sep_joins_both(self):
        wf = Workflow.from_dict({"nodes": [
            {"id": "n1", "type": "text", "fields": {"text": "a"}},
            {"id": "n2", "type": "text", "fields": {"text": "b"}},
            {"id": "n3", "type": "join", "fields": {"sep": "\\n"}},
        ], "links": [
            {"id": "l1", "from": {"node": "n1", "port": "text"}, "to": {"node": "n3", "port": "a"}},
            {"id": "l2", "from": {"node": "n2", "port": "text"}, "to": {"node": "n3", "port": "b"}},
        ]})
        self.assertEqual(wf.run()["Join"], "a\nb")

    def test_choice_bad_selected_falls_back_to_first(self):
        wf = Workflow.from_dict({"nodes": [
            {"id": "n1", "type": "choice", "fields": {"options": "x\ny", "selected": "gone"}},
        ]})
        self.assertEqual(wf.run()["Choice"], "x")

    def test_choice_without_options_fails_named(self):
        wf = Workflow.from_dict({"nodes": [
            {"id": "n1", "type": "choice", "fields": {"options": ""}},
        ]})
        from nanoodle import RunError
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("no options", str(ctx.exception))

    def test_empty_required_text_input_is_named_error(self):
        wf = Workflow.from_dict({"nodes": [
            {"id": "n1", "type": "text", "fields": {"text": ""}},
        ]})
        with self.assertRaises(NanoodleError) as ctx:
            wf.run()
        self.assertIn("missing required input", str(ctx.exception))
        self.assertIn("Text", str(ctx.exception))

    def test_upload_without_media_is_named_error(self):
        wf = Workflow.from_dict({"nodes": [
            {"id": "n1", "type": "upload", "fields": {}},
        ]})
        with self.assertRaises(NanoodleError) as ctx:
            wf.run()
        self.assertIn("missing required input", str(ctx.exception))

    def test_upload_output_is_media_ref(self):
        url = "data:image/png;base64,AAA="
        wf = Workflow.from_dict({"nodes": [
            {"id": "n1", "type": "upload", "fields": {"image": url}},
        ]})
        out = wf.run()["Image input"]
        self.assertIsInstance(out, MediaRef)
        self.assertEqual(out.url, url)


class MediaInputCoercionTest(unittest.TestCase):
    def _upload_wf(self):
        return Workflow.from_dict({"nodes": [
            {"id": "n1", "type": "aupload", "fields": {}},
        ]})

    def test_bytes_input_becomes_data_url(self):
        wav = b"RIFF\x00\x00\x00\x00WAVEdata"
        result = self._upload_wf().run(wav)   # bare scalar, single required input
        self.assertEqual(result["Audio input"].mime, "audio/wav")   # sniffed
        self.assertEqual(result["Audio input"].bytes(), wav)

    def test_dict_data_mime_input(self):
        result = self._upload_wf().run({"Audio": {"data": b"\x00\x01", "mime": "audio/flac"}})
        self.assertTrue(result["Audio input"].url.startswith("data:audio/flac;base64,"))

    def test_media_ref_input_passes_url(self):
        ref = MediaRef("data:audio/mpeg;base64,AAA=")
        result = self._upload_wf().run({"Audio": ref})
        self.assertEqual(result["Audio input"].url, "data:audio/mpeg;base64,AAA=")

    def test_media_from_file_input(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "clip.mp3")
            with open(p, "wb") as f:
                f.write(b"ID3\x03\x00body")
            result = self._upload_wf().run(media_from_file(p))
            self.assertEqual(result["Audio input"].mime, "audio/mpeg")

    def test_non_string_media_input_rejected_clearly(self):
        with self.assertRaises(NanoodleError) as ctx:
            self._upload_wf().run({"Audio": 42})
        self.assertIn("expects media", str(ctx.exception))

    def test_plain_string_media_input_refused_before_any_network_call(self):
        # "clip.mp3" riding verbatim into a paid request body is a charged garbage
        # call — refuse at coercion, before anything runs (JS coerceMediaInput parity)
        wf = Workflow.from_dict({"nodes": [
            {"id": "n1", "type": "aupload", "fields": {}},
        ]}, http=tripwire_http)
        with self.assertRaises(NanoodleError) as ctx:
            wf.run({"Audio": "clip.mp3"})
        self.assertIn("media_from_file", str(ctx.exception))
        self.assertIn("clip.mp3", str(ctx.exception))
        # data:/http(s) URL strings still pass verbatim
        result = self._upload_wf().run({"Audio": "https://cdn.example/clip.mp3"})
        self.assertEqual(result["Audio input"].url, "https://cdn.example/clip.mp3")

    def test_oversized_data_url_media_input_refused_at_coercion(self):
        from nanoodle.media import MEDIA_INLINE_MAX
        big = "data:audio/mpeg;base64," + "A" * MEDIA_INLINE_MAX
        with self.assertRaises(NanoodleError) as ctx:
            self._upload_wf().run({"Audio": big})
        self.assertIn("too large", str(ctx.exception))


class FailFastTest(unittest.TestCase):
    def test_no_api_key_with_network_nodes_fails_upfront(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("NANOGPT_API_KEY", None)
            wf = Workflow.load(fixture("starter-graph.json"), api_key=None,
                               http=tripwire_http)
            with self.assertRaises(NanoodleError) as ctx:
                wf.run({"Text": "x"})
        self.assertIn("API key", str(ctx.exception))

    def test_env_api_key_fallback(self):
        with mock.patch.dict("os.environ", {"NANOGPT_API_KEY": "env-key"}):
            wf = Workflow.load(fixture("starter-graph.json"), api_key=None)
        self.assertEqual(wf._api_key, "env-key")

    def test_unsupported_node_fails_fast_before_any_network_call(self):
        calls = []

        def spy_http(*a, **kw):
            calls.append(a)
            raise AssertionError("network call before fail-fast check")

        wf = Workflow.load(fixture("unsupported-node.json"), api_key="k", http=spy_http)
        # DESIGN: Workflow.load only WARNS for unsupported types (run fails fast)
        self.assertEqual(len(wf.warnings), 1)
        self.assertIn("resize", wf.warnings[0])
        self.assertIn("not supported by this library yet", wf.warnings[0])
        with self.assertRaises(UnsupportedNodeError) as ctx:
            wf.run()
        msg = str(ctx.exception)
        self.assertIn("node type 'resize' does local media processing that requires "
                      "the nanoodle browser app; not supported by this library yet", msg)
        self.assertEqual(ctx.exception.node_type, "resize")
        self.assertEqual(calls, [])   # fail-fast happened BEFORE any network call

    def test_every_unsupported_type_raises_with_spec_message(self):
        self.assertEqual(sorted(UNSUPPORTED_TYPES),
                         ["combine", "extractaudio", "resize", "soundtrack", "trim", "vframes"])
        for ntype in UNSUPPORTED_TYPES:
            wf = Workflow.from_dict({"nodes": [
                {"id": "n1", "type": ntype, "fields": dict(_UNSUPPORTED_STATIC[ntype]),
                 "name": "My %s" % ntype},
            ]}, api_key="k", http=tripwire_http)
            with self.assertRaises(UnsupportedNodeError) as ctx:
                wf.run()
            msg = str(ctx.exception)
            self.assertIn("node type '%s' does local media processing" % ntype, msg)
            self.assertIn("My %s" % ntype, msg)   # names the node
            self.assertEqual(ctx.exception.node_id, "n1")

    def test_unknown_type_fails_fast_before_any_network_call(self):
        wf = Workflow.from_dict({"nodes": [
            {"id": "n1", "type": "text", "fields": {"text": "x"}},
            {"id": "n2", "type": "llm", "fields": {"model": "m", "prompt": "p"}},
            {"id": "n3", "type": "wormhole", "fields": {}},
        ]}, api_key="k", http=tripwire_http)
        with self.assertRaises(UnsupportedNodeError) as ctx:
            wf.run()
        self.assertIn("wormhole", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
