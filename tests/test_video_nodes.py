"""Video family (tvideo / ivideo / vedit / lipsync): submit payloads, the
poll loop (pending -> completed / failed / timeout / garbage), url extraction."""

import unittest

from tests._util import FAST, MockedTest
from tests.harness import video_status

from nanoodle import MediaRef, RunError


class TvideoSubmitAndPollTest(MockedTest):
    def test_submit_payload_poll_loop_and_url(self):
        self.mock.script("POST", "/api/generate-video",
                         {"status": 200, "json": {"runId": "r-77", "cost": 0.25,
                                                  "remainingBalance": 3.75}})
        self.mock.script("GET", "/api/video/status", [
            {"status": 200, "json": {"status": "PENDING"}},
            {"status": 200, "json": {"status": "processing"}},   # case-insensitive
            video_status("COMPLETED", url="https://cdn/v.mp4"),
        ])
        polls = []
        wf = self.wf("video-poll.json", **FAST)
        result = wf.run(on_progress=lambda e: polls.append(e) if e["type"] == "poll" else None)
        submit = self.mock.requests_to("/api/generate-video")[0]
        self.assertEqual(submit.json, {
            "model": "seedance-2.0",
            "prompt": "a drifting paper boat",
            "duration": "5",
            "aspect_ratio": "16:9",   # aspect field -> aspect_ratio wire name
            "resolution": "720p",
            "seed": 7,                # fields.modelOpts merged verbatim
        })
        status_reqs = self.mock.requests_to("/api/video/status")
        self.assertEqual(len(status_reqs), 3)
        self.assertEqual(status_reqs[0].query, "requestId=r-77")
        # polls carry BOTH auth headers like every other call
        self.assertEqual(status_reqs[0].headers.get("authorization"), "Bearer test-key")
        self.assertEqual(status_reqs[0].headers.get("x-api-key"), "test-key")
        video = result["Text→Video"]
        self.assertIsInstance(video, MediaRef)
        self.assertEqual(video.url, "https://cdn/v.mp4")
        self.assertAlmostEqual(result.cost_usd, 0.25)
        self.assertEqual(result.remaining_balance, 3.75)
        self.assertGreaterEqual(len(polls), 2)

    def test_node_dims_win_over_stale_model_opts(self):
        self.mock.script("POST", "/api/generate-video", {"status": 200, "json": {"runId": "r"}})
        self.mock.script("GET", "/api/video/status",
                         video_status("COMPLETED", url="https://cdn/d.mp4", nested=False))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "tvideo",
             "fields": {"model": "m", "prompt": "p", "resolution": "720p",
                        "modelOpts": {"resolution": "480p", "motion_strength": 3}}}]},
            **FAST)
        wf.run()
        body = self.mock.requests_to("/api/generate-video")[0].json
        self.assertEqual(body["resolution"], "720p")        # node field wins
        self.assertEqual(body["motion_strength"], 3)        # other knobs merged verbatim

    def test_empty_dims_omitted(self):
        self.mock.script("POST", "/api/generate-video", {"status": 200, "json": {"runId": "r"}})
        self.mock.script("GET", "/api/video/status",
                         video_status("COMPLETED", url="https://cdn/x.mp4", nested=False))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "tvideo",
             "fields": {"model": "m", "prompt": "p", "resolution": "", "aspect": "",
                        "duration": ""}}]}, **FAST)
        wf.run()
        body = self.mock.requests_to("/api/generate-video")[0].json
        self.assertEqual(body, {"model": "m", "prompt": "p"})

    def test_reference_images_ordered(self):
        self.mock.script("POST", "/api/generate-video", {"status": 200, "json": {"runId": "r"}})
        self.mock.script("GET", "/api/video/status",
                         video_status("COMPLETED", url="https://cdn/r.mp4", nested=False))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "upload", "fields": {"image": "data:image/png;base64,ONE"}},
            {"id": "n2", "type": "upload", "fields": {"image": "data:image/png;base64,TWO"}},
            {"id": "n3", "type": "tvideo", "fields": {"model": "m", "prompt": "p"}},
        ], "links": [
            {"id": "l1", "from": {"node": "n2", "port": "image"}, "to": {"node": "n3", "port": "ref2"}},
            {"id": "l2", "from": {"node": "n1", "port": "image"}, "to": {"node": "n3", "port": "ref1"}},
        ]}, **FAST)
        wf.run()
        submit = self.mock.requests_to("/api/generate-video")[0]
        self.assertEqual(submit.json["reference_images"],
                         ["data:image/png;base64,ONE", "data:image/png;base64,TWO"])


class PollLoopTest(MockedTest):
    def test_failed_status_raises_with_error(self):
        self.mock.script("POST", "/api/generate-video", {"status": 200, "json": {"id": "r-1"}})
        self.mock.script("GET", "/api/video/status",
                         video_status("FAILED", error="nsfw filter"))
        wf = self.wf("video-poll.json", **FAST)
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("video failed: nsfw filter", str(ctx.exception))

    def test_canceled_status_raises_status_name(self):
        self.mock.script("POST", "/api/generate-video", {"status": 200, "json": {"runId": "r"}})
        self.mock.script("GET", "/api/video/status", video_status("CANCELED"))
        wf = self.wf("video-poll.json", **FAST)
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("video failed: CANCELED", str(ctx.exception))

    def test_poll_timeout(self):
        self.mock.script("POST", "/api/generate-video", {"status": 200, "json": {"runId": "r-2"}})
        self.mock.script("GET", "/api/video/status", {"status": 200, "json": {"status": "PENDING"}})
        wf = self.wf("video-poll.json", poll_intervals={"video": 0.01},
                     timeouts={"video": 0.05})
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("timed out", str(ctx.exception))

    def test_poll_garbage_and_500s_are_skipped(self):
        self.mock.script("POST", "/api/generate-video", {"status": 200, "json": {"runId": "r-3"}})
        self.mock.script("GET", "/api/video/status", [
            {"status": 500, "body": b"boom"},
            {"status": 200, "body": b"not json"},
            video_status("SUCCEEDED", url="https://cdn/x.mp4", nested=False),
        ])
        wf = self.wf("video-poll.json", **FAST)
        result = wf.run()
        self.assertEqual(result["Text→Video"].url, "https://cdn/x.mp4")

    def test_submit_without_run_id_errors(self):
        self.mock.script("POST", "/api/generate-video", {"status": 200, "json": {"ok": True}})
        wf = self.wf("video-poll.json", **FAST)
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("no runId", str(ctx.exception))
        self.assertEqual(self.mock.requests_to("/api/video/status"), [])

    def test_completed_without_url_errors(self):
        self.mock.script("POST", "/api/generate-video", {"status": 200, "json": {"runId": "r"}})
        self.mock.script("GET", "/api/video/status", video_status("COMPLETED"))
        wf = self.wf("video-poll.json", **FAST)
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("completed but no video url", str(ctx.exception))

    def test_video_list_url_shape(self):
        self.mock.script("POST", "/api/generate-video", {"status": 200, "json": {"runId": "r"}})
        self.mock.script("GET", "/api/video/status",
                         video_status("COMPLETED", url="https://cdn/l.mp4", video_list=True))
        wf = self.wf("video-poll.json", **FAST)
        self.assertEqual(wf.run()["Text→Video"].url, "https://cdn/l.mp4")


class SourceMediaTest(MockedTest):
    def _complete(self):
        self.mock.script("POST", "/api/generate-video", {"status": 200, "json": {"runId": "r"}})
        self.mock.script("GET", "/api/video/status",
                         video_status("COMPLETED", url="https://cdn/out.mp4", nested=False))

    def test_ivideo_sources_and_endframe(self):
        self._complete()
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "upload", "fields": {"image": "data:image/png;base64,FIRST"}},
            {"id": "n2", "type": "upload", "fields": {"image": "data:image/png;base64,LAST"}},
            {"id": "n3", "type": "ivideo", "fields": {"model": "m", "prompt": "morph"}},
        ], "links": [
            {"id": "l1", "from": {"node": "n1", "port": "image"}, "to": {"node": "n3", "port": "image"}},
            {"id": "l2", "from": {"node": "n2", "port": "image"}, "to": {"node": "n3", "port": "endframe"}},
        ]}, **FAST)
        wf.run()
        submit = self.mock.requests_to("/api/generate-video")[0]
        self.assertEqual(submit.json["imageDataUrl"], "data:image/png;base64,FIRST")
        self.assertEqual(submit.json["last_image"], "data:image/png;base64,LAST")

    def test_ivideo_without_image_errors(self):
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "ivideo", "fields": {"model": "m", "prompt": "p"}}]}, **FAST)
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("no image input", str(ctx.exception))
        self.assertEqual(self.mock.requests, [])

    def test_vedit_data_source(self):
        self._complete()
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "vupload", "fields": {"video": "data:video/mp4;base64,VID"}},
            {"id": "n2", "type": "vedit", "fields": {"model": "m", "prompt": "p"}},
        ], "links": [{"id": "l1", "from": {"node": "n1", "port": "video"},
                      "to": {"node": "n2", "port": "video"}}]}, **FAST)
        wf.run()
        submit = self.mock.requests_to("/api/generate-video")[0]
        self.assertEqual(submit.json["videoDataUrl"], "data:video/mp4;base64,VID")
        self.assertNotIn("videoUrl", submit.json)

    def test_vedit_https_source(self):
        self._complete()
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "vupload", "fields": {"video": "https://host/in.mp4"}},
            {"id": "n2", "type": "vedit", "fields": {"model": "m", "prompt": "p"}},
        ], "links": [{"id": "l1", "from": {"node": "n1", "port": "video"},
                      "to": {"node": "n2", "port": "video"}}]}, **FAST)
        wf.run()
        submit = self.mock.requests_to("/api/generate-video")[0]
        self.assertEqual(submit.json["videoUrl"], "https://host/in.mp4")
        self.assertNotIn("videoDataUrl", submit.json)

    def test_lipsync_data_audio(self):
        self._complete()
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "upload", "fields": {"image": "data:image/png;base64,FACE"}},
            {"id": "n2", "type": "aupload", "fields": {"audio": "data:audio/mpeg;base64,VOX"}},
            {"id": "n3", "type": "lipsync", "fields": {"model": "m", "prompt": ""}},
        ], "links": [
            {"id": "l1", "from": {"node": "n1", "port": "image"}, "to": {"node": "n3", "port": "image"}},
            {"id": "l2", "from": {"node": "n2", "port": "audio"}, "to": {"node": "n3", "port": "audio"}},
        ]}, **FAST)
        wf.run()
        submit = self.mock.requests_to("/api/generate-video")[0]
        self.assertEqual(submit.json["imageDataUrl"], "data:image/png;base64,FACE")
        self.assertEqual(submit.json["audioDataUrl"], "data:audio/mpeg;base64,VOX")
        self.assertNotIn("audioUrl", submit.json)

    def test_lipsync_https_audio(self):
        self._complete()
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "upload", "fields": {"image": "data:image/png;base64,FACE"}},
            {"id": "n2", "type": "aupload", "fields": {"audio": "https://host/vox.mp3"}},
            {"id": "n3", "type": "lipsync", "fields": {"model": "m"}},
        ], "links": [
            {"id": "l1", "from": {"node": "n1", "port": "image"}, "to": {"node": "n3", "port": "image"}},
            {"id": "l2", "from": {"node": "n2", "port": "audio"}, "to": {"node": "n3", "port": "audio"}},
        ]}, **FAST)
        wf.run()
        submit = self.mock.requests_to("/api/generate-video")[0]
        self.assertEqual(submit.json["audioUrl"], "https://host/vox.mp3")
        self.assertNotIn("audioDataUrl", submit.json)

    def test_lipsync_missing_audio_errors(self):
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "upload", "fields": {"image": "data:image/png;base64,FACE"}},
            {"id": "n2", "type": "lipsync", "fields": {"model": "m"}},
        ], "links": [{"id": "l1", "from": {"node": "n1", "port": "image"},
                      "to": {"node": "n2", "port": "image"}}]}, **FAST)
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("no audio input", str(ctx.exception))


class VideoLoraTest(MockedTest):
    """Authored LoRAs ride on tvideo/ivideo/vedit submits — but NOT lipsync
    (play.html passes opts.lora only from those three run()s)."""

    def _complete(self):
        self.mock.script("POST", "/api/generate-video", {"status": 200, "json": {"runId": "r"}})
        self.mock.script("GET", "/api/video/status",
                         video_status("COMPLETED", url="https://cdn/out.mp4", nested=False))

    def test_ltx_lora_rides_on_tvideo_submit(self):
        self._complete()
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "tvideo",
             "fields": {"model": "ltx-2", "prompt": "p",
                        "loraUrl": "https://host/v.safetensors", "loraStrength": "0.8"}}]},
            **FAST)
        wf.run()
        body = self.mock.requests_to("/api/generate-video")[0].json
        self.assertEqual(body["lora_url_1"], "https://host/v.safetensors")
        self.assertEqual(body["lora_scale_1"], 0.8)

    def test_lipsync_never_sends_lora(self):
        self._complete()
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "upload", "fields": {"image": "data:image/png;base64,IMG="}},
            {"id": "n2", "type": "aupload", "fields": {"audio": "data:audio/mpeg;base64,AUD="}},
            {"id": "n3", "type": "lipsync",
             "fields": {"model": "ltx-avatar", "loraUrl": "https://host/v.safetensors"}},
        ], "links": [
            {"id": "l1", "from": {"node": "n1", "port": "image"}, "to": {"node": "n3", "port": "image"}},
            {"id": "l2", "from": {"node": "n2", "port": "audio"}, "to": {"node": "n3", "port": "audio"}},
        ]}, **FAST)
        wf.run()
        body = self.mock.requests_to("/api/generate-video")[0].json
        self.assertFalse([k for k in body if "lora" in k.lower()])


class PollTransportFailureTest(MockedTest):
    def test_transient_poll_transport_failure_is_skipped(self):
        # regression: a network blip on a status GET must NOT abort the paid
        # in-flight job — SPEC-engine: poll failures silently continue
        from nanoodle import NanoodleError
        from nanoodle.transport import default_http
        state = {"fails": 0}

        def flaky(method, url, headers=None, body=None, timeout=None):
            if method == "GET" and "/api/video/status" in url and state["fails"] == 0:
                state["fails"] += 1
                raise NanoodleError("connection reset")
            return default_http(method, url, headers=headers, body=body, timeout=timeout)

        self.mock.script("POST", "/api/generate-video", {"status": 200, "json": {"runId": "r"}})
        self.mock.script("GET", "/api/video/status",
                         video_status("COMPLETED", url="https://cdn/v.mp4"))
        wf = self.wf("video-poll.json", http=flaky, **FAST)
        self.assertEqual(wf.run()["Text→Video"].url, "https://cdn/v.mp4")
        self.assertEqual(state["fails"], 1)   # the blip really happened


if __name__ == "__main__":
    unittest.main()
