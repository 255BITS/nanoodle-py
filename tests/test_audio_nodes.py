"""Audio family (music / tts / remix): payload params, extraJson merge and
song-count purge, and all three response branches (JSON url / runId poll /
binary body with header cost)."""

import unittest

from tests._util import FAST, MockedTest
from tests.harness import binary_response

from nanoodle import MediaRef, RunError


class TtsTest(MockedTest):
    def test_binary_response_with_header_cost(self):
        mp3 = b"ID3\x03\x00fakemp3"
        self.mock.script("POST", "/api/v1/audio/speech",
                         binary_response(mp3, mime="audio/mpeg", cost=0.0011, balance=4.2))
        wf = self.wf("tts-binary.json", **FAST)
        result = wf.run()
        req = self.mock.requests_to("/api/v1/audio/speech")[0]
        self.assertEqual(req.json, {"model": "kokoro-v1", "input": "hello world",
                                    "voice": "af_bella"})   # speed 1 omitted
        audio = result["Speech"]
        self.assertIsInstance(audio, MediaRef)
        self.assertEqual(audio.mime, "audio/mpeg")
        self.assertEqual(audio.bytes(), mp3)
        self.assertAlmostEqual(result.cost_usd, 0.0011)
        self.assertTrue(result.cost_exact)
        self.assertEqual(result.remaining_balance, 4.2)

    def test_non_default_speed_and_instructions_sent(self):
        self.mock.script("POST", "/api/v1/audio/speech",
                         {"status": 200, "json": {"url": "https://cdn/a.mp3"}})
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "tts",
             "fields": {"model": "m", "prompt": "hi", "voice": "nova",
                        "speed": "1.25", "instructions": "whisper"}}]}, **FAST)
        wf.run()
        req = self.mock.requests_to("/api/v1/audio/speech")[0]
        self.assertEqual(req.json, {"model": "m", "input": "hi", "voice": "nova",
                                    "speed": 1.25, "instructions": "whisper"})

    def test_generic_content_type_pinned_from_requested_format(self):
        self.mock.script("POST", "/api/v1/audio/speech",
                         binary_response(b"RIFFxxxxWAVEdata", mime="application/octet-stream"))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "tts",
             "fields": {"model": "m", "prompt": "hi",
                        "extraJson": "{\"response_format\": \"wav\"}"}}]}, **FAST)
        result = wf.run()
        req = self.mock.requests_to("/api/v1/audio/speech")[0]
        self.assertEqual(req.json["response_format"], "wav")
        self.assertEqual(result["Speech"].mime, "audio/wav")

    def test_json_url_variants(self):
        for j, url in (({"url": "https://cdn/1.mp3"}, "https://cdn/1.mp3"),
                       ({"audioUrl": "https://cdn/2.mp3"}, "https://cdn/2.mp3"),
                       ({"data": {"url": "https://cdn/3.mp3"}}, "https://cdn/3.mp3"),
                       ({"data": {"audioUrl": "https://cdn/4.mp3"}}, "https://cdn/4.mp3")):
            self.mock.reset()
            self.mock.script("POST", "/api/v1/audio/speech", {"status": 200, "json": j})
            wf = self.wf("tts-binary.json", **FAST)
            self.assertEqual(wf.run()["Speech"].url, url)

    def test_json_without_url_or_run_id_errors(self):
        self.mock.script("POST", "/api/v1/audio/speech", {"status": 200, "json": {"ok": True}})
        wf = self.wf("tts-binary.json", **FAST)
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("no audio url in response", str(ctx.exception))

    def test_invalid_extra_json_is_local_error(self):
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "tts",
             "fields": {"model": "m", "prompt": "x", "extraJson": "{nope"}}]}, **FAST)
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("advanced params: invalid JSON", str(ctx.exception))
        self.assertEqual(self.mock.requests, [])


class MusicPollTest(MockedTest):
    def test_run_id_poll_branch_and_query_string(self):
        self.mock.script("POST", "/api/v1/audio/speech",
                         {"status": 200, "json": {"runId": "job-9", "cost": 0.05,
                                                  "paymentSource": "balance",
                                                  "isApiRequest": True}})
        self.mock.script("GET", "/api/tts/status", [
            {"status": 200, "json": {"status": "pending", "queuePosition": 2}},
            {"status": 200, "json": {"status": "completed", "audioUrl": "https://cdn/song.mp3"}},
        ])
        polls = []
        wf = self.wf("music-poll.json", **FAST)
        result = wf.run(on_progress=lambda e: polls.append(e) if e["type"] == "poll" else None)
        submit = self.mock.requests_to("/api/v1/audio/speech")[0]
        self.assertEqual(submit.json, {"model": "suno-v5", "input": "lofi beats",
                                       "instrumental": True, "duration": 30})
        poll = self.mock.requests_to("/api/tts/status")[0]
        params = dict(p.split("=", 1) for p in poll.query.split("&"))
        self.assertEqual(params, {"runId": "job-9", "model": "suno-v5", "cost": "0.05",
                                  "paymentSource": "balance", "isApiRequest": "true"})
        self.assertEqual(poll.headers.get("authorization"), "Bearer test-key")
        self.assertEqual(result["Music"].url, "https://cdn/song.mp3")
        self.assertAlmostEqual(result.cost_usd, 0.05)   # charged at submit
        self.assertGreaterEqual(len(polls), 1)

    def test_poll_completed_url_variant(self):
        self.mock.script("POST", "/api/v1/audio/speech", {"status": 200, "json": {"id": "j2"}})
        self.mock.script("GET", "/api/tts/status",
                         {"status": 200, "json": {"status": "succeeded", "url": "https://cdn/u.mp3"}})
        wf = self.wf("music-poll.json", **FAST)
        self.assertEqual(wf.run()["Music"].url, "https://cdn/u.mp3")

    def test_poll_failure_statuses_raise(self):
        for status, err in (("error", "kaput"), ("failed", None),
                            ("content_policy_violation", "nope")):
            self.mock.reset()
            self.mock.script("POST", "/api/v1/audio/speech", {"status": 200, "json": {"id": "j"}})
            body = {"status": status}
            if err:
                body["error"] = err
            self.mock.script("GET", "/api/tts/status", {"status": 200, "json": body})
            wf = self.wf("music-poll.json", **FAST)
            with self.assertRaises(RunError) as ctx:
                wf.run()
            self.assertIn("audio failed: " + (err or status), str(ctx.exception))

    def test_poll_timeout(self):
        self.mock.script("POST", "/api/v1/audio/speech", {"status": 200, "json": {"runId": "j"}})
        self.mock.script("GET", "/api/tts/status", {"status": 200, "json": {"status": "pending"}})
        wf = self.wf("music-poll.json", poll_intervals={"audio": 0.01},
                     timeouts={"audio": 0.05})
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("audio timed out", str(ctx.exception))

    def test_poll_garbage_skipped_until_completed(self):
        self.mock.script("POST", "/api/v1/audio/speech", {"status": 200, "json": {"runId": "j"}})
        self.mock.script("GET", "/api/tts/status", [
            {"status": 200, "body": b"not json"},
            {"status": 200, "json": {"status": "completed", "audioUrl": "https://cdn/g.mp3"}},
        ])
        wf = self.wf("music-poll.json", **FAST)
        self.assertEqual(wf.run()["Music"].url, "https://cdn/g.mp3")

    def test_transient_poll_transport_failure_is_skipped(self):
        # regression: a network blip on a status GET must NOT abort the paid
        # in-flight job — SPEC-engine: poll failures silently continue
        from nanoodle import NanoodleError
        from nanoodle.transport import default_http
        state = {"fails": 0}

        def flaky(method, url, headers=None, body=None, timeout=None):
            if method == "GET" and "/api/tts/status" in url and state["fails"] == 0:
                state["fails"] += 1
                raise NanoodleError("connection reset")
            return default_http(method, url, headers=headers, body=body, timeout=timeout)

        self.mock.script("POST", "/api/v1/audio/speech", {"status": 200, "json": {"runId": "j"}})
        self.mock.script("GET", "/api/tts/status",
                         {"status": 200, "json": {"status": "completed",
                                                  "audioUrl": "https://cdn/s.mp3"}})
        wf = self.wf("music-poll.json", http=flaky, **FAST)
        self.assertEqual(wf.run()["Music"].url, "https://cdn/s.mp3")
        self.assertEqual(state["fails"], 1)

    def test_poll_completed_without_url_errors(self):
        self.mock.script("POST", "/api/v1/audio/speech", {"status": 200, "json": {"runId": "j"}})
        self.mock.script("GET", "/api/tts/status", {"status": 200, "json": {"status": "completed"}})
        wf = self.wf("music-poll.json", **FAST)
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("completed but no audio url", str(ctx.exception))

    def test_extra_json_merges_and_song_count_purged(self):
        self.mock.script("POST", "/api/v1/audio/speech",
                         {"status": 200, "json": {"url": "https://cdn/x.mp3"}})
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "music",
             "fields": {"model": "m", "prompt": "p",
                        "extraJson": "{\"style\": \"jazz\", \"number_of_songs\": 4,"
                                     " \"numsongs\": 2}"}}]}, **FAST)
        wf.run()
        req = self.mock.requests_to("/api/v1/audio/speech")[0]
        self.assertEqual(req.json.get("style"), "jazz")
        self.assertNotIn("number_of_songs", req.json)   # one-track contract
        self.assertNotIn("numsongs", req.json)

    def test_music_params_omitted_when_empty(self):
        self.mock.script("POST", "/api/v1/audio/speech",
                         {"status": 200, "json": {"url": "https://cdn/x.mp3"}})
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "music",
             "fields": {"model": "m", "prompt": "p", "lyrics": "",
                        "negative_prompt": "", "instrumental": "false",
                        "seed": ""}}]}, **FAST)
        wf.run()
        req = self.mock.requests_to("/api/v1/audio/speech")[0]
        self.assertEqual(req.json, {"model": "m", "input": "p"})

    def test_music_lyrics_negative_prompt_seed_sent(self):
        self.mock.script("POST", "/api/v1/audio/speech",
                         {"status": 200, "json": {"url": "https://cdn/x.mp3"}})
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "music",
             "fields": {"model": "m", "prompt": "p", "lyrics": "la la",
                        "negative_prompt": "no drums", "seed": "11"}}]}, **FAST)
        wf.run()
        req = self.mock.requests_to("/api/v1/audio/speech")[0]
        self.assertEqual(req.json, {"model": "m", "input": "p", "lyrics": "la la",
                                    "negative_prompt": "no drums", "seed": 11})


class RemixTest(MockedTest):
    def test_https_source_rides_as_is(self):
        self.mock.script("POST", "/api/v1/audio/speech",
                         {"status": 200, "json": {"url": "https://cdn/r.mp3"}})
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "aupload", "fields": {"audio": "https://host/src.mp3"}},
            {"id": "n2", "type": "remix",
             "fields": {"model": "m", "prompt": "make it disco", "duration": "20"}},
        ], "links": [{"id": "l1", "from": {"node": "n1", "port": "audio"},
                      "to": {"node": "n2", "port": "audio"}}]}, **FAST)
        wf.run()
        req = self.mock.requests_to("/api/v1/audio/speech")[0]
        self.assertEqual(req.json, {"model": "m", "input": "make it disco",
                                    "duration": 20, "audio": "https://host/src.mp3"})

    def test_data_url_source_inlined(self):
        self.mock.script("POST", "/api/v1/audio/speech",
                         {"status": 200, "json": {"url": "https://cdn/r.mp3"}})
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "aupload", "fields": {"audio": "data:audio/mpeg;base64,U1JD"}},
            {"id": "n2", "type": "remix", "fields": {"model": "m", "prompt": "cover it"}},
        ], "links": [{"id": "l1", "from": {"node": "n1", "port": "audio"},
                      "to": {"node": "n2", "port": "audio"}}]}, **FAST)
        wf.run()
        req = self.mock.requests_to("/api/v1/audio/speech")[0]
        self.assertEqual(req.json["audio"], "data:audio/mpeg;base64,U1JD")

    def test_missing_audio_source_errors(self):
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "remix", "fields": {"model": "m", "prompt": "p"}}]}, **FAST)
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("no audio", str(ctx.exception))
        self.assertEqual(self.mock.requests, [])

    def test_oversized_local_source_is_clear_error(self):
        big = "data:audio/mpeg;base64," + "A" * 4_700_000
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "aupload", "fields": {"audio": big}},
            {"id": "n2", "type": "remix", "fields": {"model": "m", "prompt": "p"}},
        ], "links": [{"id": "l1", "from": {"node": "n1", "port": "audio"},
                      "to": {"node": "n2", "port": "audio"}}]}, **FAST)
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("too large", str(ctx.exception))
        self.assertEqual(self.mock.requests, [])


if __name__ == "__main__":
    unittest.main()
