"""Chat-endpoint nodes: llm / vision — exact payloads per SPEC-engine
plus response parsing (content parts, reasoning)."""

import base64
import unittest

from tests._util import FAST, MockedTest  # noqa: F401
from tests.harness import chat_response

from nanoodle import RunError, Workflow


class LlmPayloadTest(MockedTest):
    def test_llm_vision_payload(self):
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("a red fox"))
        wf = self.wf("llm-vision.json")
        result = wf.run()
        req = self.mock.requests_to("/api/v1/chat/completions")[0]
        self.assertEqual(req.json, {
            "model": "gpt-5o",
            "messages": [
                # empty system field -> the spec default backfills (JS parity)
                {"role": "system", "content": "You are a helpful, concise assistant."},
                {"role": "user", "content": [
                    {"type": "text", "text": "What is in this picture?"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
                ]}],
            "temperature": 0.8,
        })
        self.assertNotIn("stream", req.json)   # NON-streaming engine
        self.assertEqual(req.headers.get("authorization"), "Bearer test-key")
        self.assertEqual(req.headers.get("x-api-key"), "test-key")
        self.assertEqual(result["LLM"], "a red fox")

    def test_llm_json_mode_reasoning_effort_max_tokens_temperature(self):
        self.mock.script("POST", "/api/v1/chat/completions", chat_response('{"ok":1}'))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "llm",
             "fields": {"model": "m", "prompt": "p", "format": "JSON",
                        "reasoningEffort": "high", "maxTokens": "60",
                        "temperature": "0.2"}},
        ]})
        wf.run()
        req = self.mock.requests_to("/api/v1/chat/completions")[0]
        self.assertEqual(req.json, {
            "model": "m",
            "messages": [
                {"role": "system", "content": "You are a helpful, concise assistant."},
                {"role": "user", "content": "p"}],
            "temperature": 0.2,
            "max_tokens": 60,
            "response_format": {"type": "json_object"},
            "reasoning_effort": "high",
        })

    def test_llm_default_reasoning_effort_and_text_format_omitted(self):
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("ok"))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "llm",
             "fields": {"model": "m", "prompt": "p", "format": "Text",
                        "reasoningEffort": "default"}}]})
        wf.run()
        req = self.mock.requests_to("/api/v1/chat/completions")[0]
        self.assertNotIn("reasoning_effort", req.json)
        self.assertNotIn("response_format", req.json)
        self.assertNotIn("max_tokens", req.json)

    def test_llm_system_message_included_then_skipped_when_blank(self):
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("ok"))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "llm",
             "fields": {"model": "m", "prompt": "p", "system": "Be brief."}}]})
        wf.run()
        req = self.mock.requests_to("/api/v1/chat/completions")[0]
        self.assertEqual(req.json["messages"][0], {"role": "system", "content": "Be brief."})

        self.mock.reset()
        wf2 = self.wf_dict({"nodes": [
            {"id": "n1", "type": "llm",
             "fields": {"model": "m", "prompt": "p", "system": "   "}}]})
        wf2.run()
        req2 = self.mock.requests_to("/api/v1/chat/completions")[0]
        self.assertEqual(req2.json["messages"], [{"role": "user", "content": "p"}])

    def test_llm_wired_images_sorted_by_index(self):
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("both"))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "upload", "fields": {"image": "data:image/png;base64,ONE"}},
            {"id": "n2", "type": "upload", "fields": {"image": "data:image/png;base64,TWO"}},
            {"id": "n3", "type": "llm", "fields": {"model": "m", "prompt": "compare"}},
        ], "links": [
            # wired out of order on purpose: img2 first
            {"id": "l1", "from": {"node": "n2", "port": "image"}, "to": {"node": "n3", "port": "img2"}},
            {"id": "l2", "from": {"node": "n1", "port": "image"}, "to": {"node": "n3", "port": "img1"}},
        ]})
        wf.run()
        content = self.mock.requests_to("/api/v1/chat/completions")[0].json["messages"][-1]["content"]
        self.assertEqual([p["image_url"]["url"] for p in content if p["type"] == "image_url"],
                         ["data:image/png;base64,ONE", "data:image/png;base64,TWO"])

    def test_llm_wired_audio_becomes_input_audio_part(self):
        wav = "data:audio/wav;base64,UklGRgAAAABXQVZF"
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("heard it"))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "aupload", "fields": {"audio": wav}},
            {"id": "n2", "type": "llm", "fields": {"model": "m", "prompt": "what is this?"}},
        ], "links": [{"id": "l1", "from": {"node": "n1", "port": "audio"},
                      "to": {"node": "n2", "port": "audio"}}]})
        wf.run()
        content = self.mock.requests_to("/api/v1/chat/completions")[0].json["messages"][-1]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "what is this?"})
        self.assertEqual(content[1], {"type": "input_audio",
                                      "input_audio": {"data": "UklGRgAAAABXQVZF",
                                                      "format": "wav"}})

    def test_llm_audio_format_mapping_mpeg_to_mp3(self):
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("ok"))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "aupload", "fields": {"audio": "data:audio/mpeg;base64,QUJD"}},
            {"id": "n2", "type": "llm", "fields": {"model": "m", "prompt": "p"}},
        ], "links": [{"id": "l1", "from": {"node": "n1", "port": "audio"},
                      "to": {"node": "n2", "port": "audio"}}]})
        wf.run()
        content = self.mock.requests_to("/api/v1/chat/completions")[0].json["messages"][-1]["content"]
        self.assertEqual(content[1]["input_audio"]["format"], "mp3")

    def test_llm_empty_wired_prompt_is_no_prompt_error(self):
        # a wired prompt that resolves to "" fails in the engine (not upfront:
        # the field is wire-fed so it is not a derived input)
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "join", "fields": {}},   # inputless join -> ""
            {"id": "n2", "type": "llm", "fields": {"model": "m"}},
        ], "links": [{"id": "l1", "from": {"node": "n1", "port": "text"},
                      "to": {"node": "n2", "port": "prompt"}}]})
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("no prompt", str(ctx.exception))
        self.assertEqual(self.mock.requests, [])   # never reached the network

    def test_llm_missing_model_error(self):
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "llm", "fields": {"prompt": "p"}}]})
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("pick a model first", str(ctx.exception))
        self.assertEqual(self.mock.requests, [])


class LlmHostedAudioTest(MockedTest):
    """Cross-language parity (critical): an llm audio port fed by a music/tts
    node that returned a hosted https URL must download + inline the clip —
    SPEC-engine mandates input_audio.data = base64 bytes, never a URL."""

    WAV = b"RIFF\x24\x00\x00\x00WAVEfmt trailing-bytes"

    def _run(self, media_response):
        self.mock.script("POST", "/api/v1/audio/speech",
                         {"status": 200, "json": {"url": self.mock.base_url + "/media/song.wav"}})
        self.mock.script("GET", "/media/song.wav", media_response)
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("heard it"))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "tts", "fields": {"model": "m", "prompt": "sing"}},
            {"id": "n2", "type": "llm", "fields": {"model": "m", "prompt": "what is this?"}},
        ], "links": [{"id": "l1", "from": {"node": "n1", "port": "audio"},
                      "to": {"node": "n2", "port": "audio"}}]}, **FAST)
        wf.run()
        content = self.mock.requests_to("/api/v1/chat/completions")[0].json["messages"][-1]["content"]
        return content[1]

    def test_https_audio_is_downloaded_and_inlined_as_base64(self):
        from tests.harness import binary_response
        part = self._run(binary_response(self.WAV, mime="audio/wav"))
        self.assertEqual(part["type"], "input_audio")
        self.assertEqual(part["input_audio"]["format"], "wav")
        self.assertEqual(part["input_audio"]["data"],
                         base64.b64encode(self.WAV).decode("ascii"))
        # the paid chat call must NEVER carry a raw URL as "base64 data"
        self.assertNotIn("http", part["input_audio"]["data"])
        # the CDN download carries no auth headers
        media_req = self.mock.requests_to("/media/song.wav")[0]
        self.assertNotIn("authorization", media_req.headers)
        self.assertNotIn("x-api-key", media_req.headers)

    def test_generic_content_type_falls_back_to_magic_byte_sniff(self):
        from tests.harness import binary_response
        part = self._run(binary_response(self.WAV, mime="application/octet-stream"))
        self.assertEqual(part["input_audio"]["format"], "wav")   # sniffed RIFF/WAVE


class LlmSystemDefaultTest(MockedTest):
    """Cross-language parity: run() backfills optional-input spec defaults —
    an llm with an empty system field sends the app's default system message."""

    def _graph(self):
        return {"nodes": [
            {"id": "n1", "type": "llm", "fields": {"model": "m", "prompt": "p"}}]}

    def test_absent_system_field_backfills_spec_default(self):
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("ok"))
        wf = self.wf_dict(self._graph())
        wf.run()
        req = self.mock.requests_to("/api/v1/chat/completions")[0]
        self.assertEqual(req.json["messages"][0],
                         {"role": "system", "content": "You are a helpful, concise assistant."})

    def test_explicit_empty_system_input_clears_the_default(self):
        # an EXPLICIT empty value clears an optional input — no system message
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("ok"))
        wf = self.wf_dict(self._graph())
        wf.run({"System prompt": ""})
        req = self.mock.requests_to("/api/v1/chat/completions")[0]
        self.assertEqual(req.json["messages"], [{"role": "user", "content": "p"}])

    def test_whole_number_temperature_serializes_as_int(self):
        # parity nit: JS `+temperature` sends 1, not 1.0 — keep bodies byte-identical
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("ok"))
        graph = self._graph()
        graph["nodes"][0]["fields"]["temperature"] = "1"
        wf = self.wf_dict(graph)
        wf.run()
        body = self.mock.requests_to("/api/v1/chat/completions")[0].body.decode("utf-8")
        self.assertIn('"temperature": 1', body)
        self.assertNotIn('"temperature": 1.0', body)


class LlmParseTest(MockedTest):
    def test_content_parts_joined(self):
        self.mock.script("POST", "/api/v1/chat/completions",
                         chat_response(None, content_parts=[{"type": "text", "text": "he"},
                                                            {"type": "text", "text": "llo"}]))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "llm", "fields": {"model": "m", "prompt": "p"}}]})
        self.assertEqual(wf.run()["LLM"], "hello")

    def test_null_content_is_error(self):
        self.mock.script("POST", "/api/v1/chat/completions",
                         {"status": 200, "json": {"choices": [{"message": {"content": None}}]}})
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "llm", "fields": {"model": "m", "prompt": "p"}}]})
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("no text in response", str(ctx.exception))

    def test_empty_string_content_is_error(self):
        # SPEC-engine: null/EMPTY content both throw (JS client.chat parity)
        self.mock.script("POST", "/api/v1/chat/completions", chat_response(""))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "llm", "fields": {"model": "m", "prompt": "p"}}]})
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("no text in response", str(ctx.exception))

    def test_content_parts_joining_to_empty_still_passes(self):
        # the empty check runs BEFORE the list-join (JS parity): [] -> "" is allowed
        self.mock.script("POST", "/api/v1/chat/completions",
                         chat_response(None, content_parts=[{"type": "text", "text": ""}]))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "llm", "fields": {"model": "m", "prompt": "p"}}]})
        self.assertEqual(wf.run()["LLM"], "")

    def test_show_thinking_wraps_reasoning(self):
        self.mock.script("POST", "/api/v1/chat/completions",
                         chat_response("answer", reasoning="chain of thought"))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "llm",
             "fields": {"model": "m", "prompt": "p", "showThinking": "true"}}]})
        out = wf.run()["LLM"]
        self.assertTrue(out.startswith("```thinking\nchain of thought\n```"))
        self.assertTrue(out.endswith("answer"))

    def test_show_thinking_off_by_default(self):
        self.mock.script("POST", "/api/v1/chat/completions",
                         chat_response("answer", reasoning="hidden"))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "llm", "fields": {"model": "m", "prompt": "p"}}]})
        self.assertEqual(wf.run()["LLM"], "answer")


class VisionTest(MockedTest):
    def test_vision_default_question_payload(self):
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("a cat"))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "upload", "fields": {"image": "data:image/png;base64,AAA="}},
            {"id": "n2", "type": "vision", "fields": {"model": "m"}},
        ], "links": [{"id": "l1", "from": {"node": "n1", "port": "image"},
                      "to": {"node": "n2", "port": "image"}}]})
        result = wf.run()
        req = self.mock.requests_to("/api/v1/chat/completions")[0]
        self.assertEqual(req.json["messages"], [{"role": "user", "content": [
            {"type": "text", "text": "Describe this image."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA="}},
        ]}])
        self.assertEqual(result["Vision"], "a cat")

    def test_vision_custom_question(self):
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("blue"))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "upload", "fields": {"image": "data:image/png;base64,AAA="}},
            {"id": "n2", "type": "vision", "fields": {"model": "m", "q": "What color?"}},
        ], "links": [{"id": "l1", "from": {"node": "n1", "port": "image"},
                      "to": {"node": "n2", "port": "image"}}]})
        wf.run()
        req = self.mock.requests_to("/api/v1/chat/completions")[0]
        self.assertEqual(req.json["messages"][0]["content"][0]["text"], "What color?")

    def test_vision_without_image_errors(self):
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "vision", "fields": {"model": "m"}}]})
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("no image input", str(ctx.exception))
        self.assertEqual(self.mock.requests, [])


class FieldOverrideTest(MockedTest):
    def test_wire_into_textarea_field_overrides_typed_value(self):
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("arr"))
        wf = self.wf("field-override.json")
        wf.run()
        req = self.mock.requests_to("/api/v1/chat/completions")[0]
        self.assertEqual(req.json["messages"][0],
                         {"role": "system", "content": "You are a pirate."})
        # override the Persona (text node) input -> new system prompt
        self.mock.reset()
        wf.run({"Persona": "You are a knight."})
        req2 = self.mock.requests_to("/api/v1/chat/completions")[0]
        self.assertEqual(req2.json["messages"][0]["content"], "You are a knight.")

    def test_wired_text_into_tts_text_port_feeds_input(self):
        # legacy "text" port on tts is migrated to prompt and overrides the field
        self.mock.script("POST", "/api/v1/audio/speech",
                         {"status": 200, "json": {"url": "https://cdn/a.mp3"}})
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "text", "fields": {"text": "read me"}},
            {"id": "n2", "type": "tts", "fields": {"model": "m", "prompt": "typed, ignored"}},
        ], "links": [{"id": "l1", "from": {"node": "n1", "port": "text"},
                      "to": {"node": "n2", "port": "text"}}]}, **FAST)
        wf.run()
        req = self.mock.requests_to("/api/v1/audio/speech")[0]
        self.assertEqual(req.json["input"], "read me")

    def test_none_upstream_value_leaves_typed_field(self):
        # regression: play.html only overrides when v != null — a link from a
        # nonexistent out port must not clobber the typed prompt with None
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("ok"))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "text", "fields": {"text": "x"}},
            {"id": "n2", "type": "llm", "fields": {"model": "m", "prompt": "typed prompt"}},
        ], "links": [{"id": "l1", "from": {"node": "n1", "port": "nosuchport"},
                      "to": {"node": "n2", "port": "prompt"}}]})
        result = wf.run()
        req = self.mock.requests_to("/api/v1/chat/completions")[0]
        self.assertEqual(req.json["messages"][-1]["content"], "typed prompt")
        self.assertEqual(result["LLM"], "ok")


class MaxTokensDegradeTest(MockedTest):
    def test_non_numeric_max_tokens_degrades_instead_of_crashing(self):
        # regression: play.html (+maxTokens -> NaN -> null) still completes the
        # call; a hand-edited graph must not die on ValueError
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("ok"))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "llm",
             "fields": {"model": "m", "prompt": "p", "maxTokens": "lots"}}]})
        result = wf.run()
        self.assertNotIn("max_tokens", self.mock.requests_to("/api/v1/chat/completions")[0].json)
        self.assertEqual(result["LLM"], "ok")


class NamedNodeRunTest(MockedTest):
    """Regression: the very key wf.inputs advertises for a custom-named node
    (with an unfed optional system input) must run — and so must a bare scalar."""

    def _wf(self):
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("ok"))
        return self.wf_dict({"nodes": [
            {"id": "n1", "type": "llm", "fields": {"model": "m"}, "name": "Writer"}]})

    def test_advertised_custom_name_key_runs(self):
        wf = self._wf()
        self.assertIn("Writer", [s.key for s in wf.inputs])
        result = wf.run({"Writer": "hello"})
        req = self.mock.requests_to("/api/v1/chat/completions")[0]
        self.assertEqual(req.json["messages"][-1], {"role": "user", "content": "hello"})
        self.assertEqual(result["Writer"], "ok")

    def test_bare_scalar_runs_the_single_required_input(self):
        wf = self._wf()
        result = wf.run("hello")
        req = self.mock.requests_to("/api/v1/chat/completions")[0]
        self.assertEqual(req.json["messages"][-1], {"role": "user", "content": "hello"})
        self.assertEqual(result["Writer"], "ok")


if __name__ == "__main__":
    unittest.main()
