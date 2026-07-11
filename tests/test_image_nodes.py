"""Image-endpoint nodes: image / edit / inpaint — exact payloads, b64 mime
sniffing, multi-reference shapes, and the local media-size guards."""

import base64
import unittest

from tests._util import MockedTest
from tests.harness import image_response

from nanoodle import RunError

BIG_DATA_URL = "data:image/png;base64," + "A" * (4_700_000)   # > 4.4MB inline cap


class ImageNodeTest(MockedTest):
    def test_variations_seed_and_jpeg_sniff(self):
        jpeg_b64 = base64.b64encode(b"\xff\xd8\xffmockjpeg").decode()
        self.mock.script("POST", "/v1/images/generations",
                         image_response(b64_list=[jpeg_b64, jpeg_b64]))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "image",
             "fields": {"model": "m", "prompt": "p", "variations": "2", "seed": "42"}}]})
        result = wf.run()
        req = self.mock.requests_to("/v1/images/generations")[0]
        self.assertEqual(req.json, {"model": "m", "size": "1024x1024", "n": 2,
                                    "response_format": "b64_json", "prompt": "p",
                                    "seed": 42})
        self.assertTrue(result["Image"].url.startswith("data:image/jpeg;base64,"))
        self.assertEqual(len(result.nodes["n1"].out["images"]), 2)

    def test_b64_sniff_table(self):
        # png / gif / webp prefixes + unknown default png (play.html table)
        for b64, mime in (("iVBORxxxx", "image/png"), ("R0lGxxxx", "image/gif"),
                          ("UklGRxxxx", "image/webp"), ("QUJDRA==", "image/png")):
            self.mock.reset()
            self.mock.script("POST", "/v1/images/generations", image_response(b64_list=[b64]))
            wf = self.wf_dict({"nodes": [
                {"id": "n1", "type": "image", "fields": {"model": "m", "prompt": "p"}}]})
            self.assertEqual(wf.run()["Image"].url, "data:%s;base64,%s" % (mime, b64))

    def test_url_entries_pass_through(self):
        self.mock.script("POST", "/v1/images/generations",
                         image_response(urls=["https://cdn.example/a.png"]))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "image", "fields": {"model": "m", "prompt": "p"}}]})
        self.assertEqual(wf.run()["Image"].url, "https://cdn.example/a.png")

    def test_empty_data_is_no_image_error(self):
        self.mock.script("POST", "/v1/images/generations", {"status": 200, "json": {"data": []}})
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "image", "fields": {"model": "m", "prompt": "p"}}]})
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("no image in response", str(ctx.exception))

    def test_missing_model_error_before_network(self):
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "image", "fields": {"prompt": "p"}}]})
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("pick a model first", str(ctx.exception))
        self.assertEqual(self.mock.requests, [])

    def test_custom_civitai_air_rides_in_body(self):
        self.mock.script("POST", "/v1/images/generations", image_response(urls=["https://x/y"]))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "image",
             "fields": {"model": "custom-civitai", "prompt": "p",
                        "customCivitaiAir": "civitai:123@456"}}]})
        wf.run()
        req = self.mock.requests_to("/v1/images/generations")[0]
        self.assertEqual(req.json["customCivitaiAir"], "civitai:123@456")

    def test_custom_civitai_air_normalized_from_url_and_bare_forms(self):
        # play.html normalizeCustomCivitaiAir: civitai.com URL and bare ID@VER
        # forms are normalized to civitai:ID@VER before the request
        for raw, want in (
                ("https://civitai.com/models/123?modelVersionId=456", "civitai:123@456"),
                ("123@456", "civitai:123@456")):
            self.mock.reset()
            self.mock.script("POST", "/v1/images/generations",
                             image_response(urls=["https://x/y"]))
            wf = self.wf_dict({"nodes": [
                {"id": "n1", "type": "image",
                 "fields": {"model": "custom-civitai", "prompt": "p",
                            "customCivitaiAir": raw}}]})
            wf.run()
            req = self.mock.requests_to("/v1/images/generations")[0]
            self.assertEqual(req.json["customCivitaiAir"], want)

    def test_custom_civitai_empty_air_errors_before_the_paid_call(self):
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "image",
             "fields": {"model": "custom-civitai", "prompt": "p"}}]})
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("select an AIR model", str(ctx.exception))
        self.assertEqual(self.mock.requests, [])   # never charged

    def test_custom_civitai_malformed_air_errors_before_the_paid_call(self):
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "image",
             "fields": {"model": "custom-civitai", "prompt": "p",
                        "customCivitaiAir": "urn:air:sdxl:ckpt@1"}}]})
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("AIR must look like", str(ctx.exception))
        self.assertEqual(self.mock.requests, [])

    def test_baked_sel_picks_the_primary_image(self):
        # play.html: image: urls[clamp(parseInt(fields.sel))] — not always urls[0]
        self.mock.script("POST", "/v1/images/generations",
                         image_response(urls=["https://x/0.png", "https://x/1.png"]))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "image",
             "fields": {"model": "m", "prompt": "p", "variations": "2", "sel": "1"}}]})
        self.assertEqual(wf.run()["Image"].url, "https://x/1.png")

    def test_out_of_range_or_junk_sel_clamped(self):
        for sel, want in (("7", "https://x/1.png"), ("-3", "https://x/0.png"),
                          ("junk", "https://x/0.png")):
            self.mock.reset()
            self.mock.script("POST", "/v1/images/generations",
                             image_response(urls=["https://x/0.png", "https://x/1.png"]))
            wf = self.wf_dict({"nodes": [
                {"id": "n1", "type": "image",
                 "fields": {"model": "m", "prompt": "p", "variations": "2", "sel": sel}}]})
            self.assertEqual(wf.run()["Image"].url, want)

    def test_non_numeric_seed_omitted(self):
        self.mock.script("POST", "/v1/images/generations", image_response(urls=["https://x/y"]))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "image",
             "fields": {"model": "m", "prompt": "p", "seed": "random"}}]})
        wf.run()
        self.assertNotIn("seed", self.mock.requests_to("/v1/images/generations")[0].json)


class EditNodeTest(MockedTest):
    def test_multi_image_payload_is_array(self):
        self.mock.script("POST", "/v1/images/generations",
                         image_response(urls=["https://cdn.example/out.png"], cost=0.02))
        wf = self.wf("edit-multi.json")
        result = wf.run()
        req = self.mock.requests_to("/v1/images/generations")[0]
        self.assertEqual(req.json, {
            "model": "nano-banana-2",
            "size": "1k",
            "n": 1,
            "response_format": "b64_json",
            "prompt": "blend them",
            "imageDataUrl": ["data:image/png;base64,AAA=", "data:image/png;base64,BBB="],
        })
        self.assertEqual(result["Edit"].url, "https://cdn.example/out.png")

    def test_single_image_is_string_not_array(self):
        self.mock.script("POST", "/v1/images/generations", image_response(urls=["https://x/y.png"]))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "upload", "fields": {"image": "data:image/png;base64,AAA="}},
            {"id": "n2", "type": "edit", "fields": {"model": "m", "prompt": "p"}},
        ], "links": [{"id": "l1", "from": {"node": "n1", "port": "image"},
                      "to": {"node": "n2", "port": "image"}}]})
        wf.run()
        req = self.mock.requests_to("/v1/images/generations")[0]
        self.assertEqual(req.json["imageDataUrl"], "data:image/png;base64,AAA=")

    def test_no_image_input_errors(self):
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "edit", "fields": {"model": "m", "prompt": "p"}}]})
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("no image input", str(ctx.exception))

    def test_empty_prompt_refused_for_normal_model(self):
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "upload", "fields": {"image": "data:image/png;base64,AAA="}},
            {"id": "n2", "type": "edit", "fields": {"model": "nano-banana-2", "prompt": ""}},
        ], "links": [{"id": "l1", "from": {"node": "n1", "port": "image"},
                      "to": {"node": "n2", "port": "image"}}]})
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("no edit instruction", str(ctx.exception))
        self.assertEqual(self.mock.requests, [])

    def test_upscaler_runs_with_empty_prompt_omitted_from_body(self):
        self.mock.script("POST", "/v1/images/generations", image_response(urls=["https://x/u.png"]))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "upload", "fields": {"image": "data:image/png;base64,AAA="}},
            {"id": "n2", "type": "edit", "fields": {"model": "clarity-upscaler", "prompt": ""}},
        ], "links": [{"id": "l1", "from": {"node": "n1", "port": "image"},
                      "to": {"node": "n2", "port": "image"}}]})
        wf.run()
        req = self.mock.requests_to("/v1/images/generations")[0]
        self.assertNotIn("prompt", req.json)
        self.assertEqual(req.json["imageDataUrl"], "data:image/png;base64,AAA=")

    def test_combined_reference_size_guard_no_network(self):
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "upload", "fields": {"image": BIG_DATA_URL}},
            {"id": "n2", "type": "edit", "fields": {"model": "m", "prompt": "p"}},
        ], "links": [{"id": "l1", "from": {"node": "n1", "port": "image"},
                      "to": {"node": "n2", "port": "image"}}]})
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("too large", str(ctx.exception))
        self.assertEqual(self.mock.requests, [])


class InpaintNodeTest(MockedTest):
    def test_field_source_and_mask_pass_through(self):
        self.mock.script("POST", "/v1/images/generations", image_response(urls=["https://x/o.png"]))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "inpaint",
             "fields": {"model": "m", "prompt": "a hat",
                        "image": "data:image/png;base64,SRC=",
                        "mask": "data:image/png;base64,MASK="}}]})
        wf.run()
        req = self.mock.requests_to("/v1/images/generations")[0]
        self.assertEqual(req.json["imageDataUrl"], "data:image/png;base64,SRC=")
        self.assertEqual(req.json["maskDataUrl"], "data:image/png;base64,MASK=")
        self.assertEqual(req.json["prompt"], "a hat")

    def test_wired_source_and_mask_win_over_fields(self):
        self.mock.script("POST", "/v1/images/generations", image_response(urls=["https://x/o.png"]))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "upload", "fields": {"image": "data:image/png;base64,WSRC"}},
            {"id": "n2", "type": "upload", "fields": {"image": "data:image/png;base64,WMASK"}},
            {"id": "n3", "type": "inpaint",
             "fields": {"model": "m", "prompt": "p",
                        "image": "data:image/png;base64,STALE",
                        "mask": "data:image/png;base64,STALE"}},
        ], "links": [
            {"id": "l1", "from": {"node": "n1", "port": "image"}, "to": {"node": "n3", "port": "image"}},
            {"id": "l2", "from": {"node": "n2", "port": "image"}, "to": {"node": "n3", "port": "mask"}},
        ]})
        wf.run()
        req = self.mock.requests_to("/v1/images/generations")[0]
        self.assertEqual(req.json["imageDataUrl"], "data:image/png;base64,WSRC")
        self.assertEqual(req.json["maskDataUrl"], "data:image/png;base64,WMASK")

    def test_missing_mask_is_upfront_named_error(self):
        # image wired, mask not: the mask is a required derived input -> named upfront
        from nanoodle import NanoodleError
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "upload", "fields": {"image": "data:image/png;base64,AAA="}},
            {"id": "n2", "type": "inpaint", "fields": {"model": "m", "prompt": "p"}},
        ], "links": [{"id": "l1", "from": {"node": "n1", "port": "image"},
                      "to": {"node": "n2", "port": "image"}}]})
        with self.assertRaises(NanoodleError) as ctx:
            wf.run()
        self.assertIn("missing required input", str(ctx.exception))
        self.assertIn("Mask", str(ctx.exception))
        self.assertEqual(self.mock.requests, [])

    def test_neither_wired_both_image_and_mask_supplied_as_run_inputs(self):
        # regression: with nothing wired BOTH image and mask are derived inputs
        # (the mask used to be underivable, making such workflows unrunnable)
        self.mock.script("POST", "/v1/images/generations", image_response(urls=["https://x/o.png"]))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "inpaint", "fields": {"model": "m", "prompt": "p"}}]})
        wf.run({"n1.image": "data:image/png;base64,SRC=",
                "n1.mask": "data:image/png;base64,MASK="})
        req = self.mock.requests_to("/v1/images/generations")[0]
        self.assertEqual(req.json["imageDataUrl"], "data:image/png;base64,SRC=")
        self.assertEqual(req.json["maskDataUrl"], "data:image/png;base64,MASK=")

    def test_neither_wired_nothing_baked_is_upfront_missing_error(self):
        from nanoodle import NanoodleError
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "inpaint", "fields": {"model": "m", "prompt": "p"}}]})
        with self.assertRaises(NanoodleError) as ctx:
            wf.run()
        self.assertIn("missing required input", str(ctx.exception))
        self.assertIn("Mask", str(ctx.exception))
        self.assertEqual(self.mock.requests, [])   # fails before spending

    def test_mask_supplied_as_run_input(self):
        self.mock.script("POST", "/v1/images/generations", image_response(urls=["https://x/o.png"]))
        wf = self.wf_dict({"nodes": [
            {"id": "n1", "type": "upload", "fields": {"image": "data:image/png;base64,AAA="}},
            {"id": "n2", "type": "inpaint", "fields": {"model": "m", "prompt": "p"}},
        ], "links": [{"id": "l1", "from": {"node": "n1", "port": "image"},
                      "to": {"node": "n2", "port": "image"}}]})
        wf.run({"Mask (white = repaint)": "data:image/png;base64,RUNMASK"})
        req = self.mock.requests_to("/v1/images/generations")[0]
        self.assertEqual(req.json["maskDataUrl"], "data:image/png;base64,RUNMASK")


class LoraParamsTest(MockedTest):
    """Authored LoRAs must ride on image-family requests
    (SPEC-engine image section '+ LoRA params'; play.html imgExtra→loraParams)."""

    def _image_body(self, model, fields_extra):
        self.mock.script("POST", "/v1/images/generations", image_response(urls=["https://x/y"]))
        fields = {"model": model, "prompt": "p"}
        fields.update(fields_extra)
        wf = self.wf_dict({"nodes": [{"id": "n1", "type": "image", "fields": fields}]})
        wf.run()
        return self.mock.requests_to("/v1/images/generations")[0].json

    def test_flux_lora_single_slot_keys(self):
        body = self._image_body("flux-lora", {"loraUrl": "https://host/a.safetensors",
                                              "loraStrength": "0.7"})
        self.assertEqual(body["lora_url"], "https://host/a.safetensors")
        self.assertEqual(body["lora_strength"], 0.7)

    def test_flux2_multi_lora_numbered_keys(self):
        body = self._image_body("flux-2-dev-lora", {"loras": [
            {"url": "https://host/a.safetensors", "strength": "1"},
            {"url": "https://host/b.safetensors", "strength": "0.5"}]})
        self.assertEqual(body["lora_url_1"], "https://host/a.safetensors")
        self.assertEqual(body["lora_scale_1"], 1)
        self.assertEqual(body["lora_url_2"], "https://host/b.safetensors")
        self.assertEqual(body["lora_scale_2"], 0.5)

    def test_pimage_lora_weights_keys_and_blank_strength_defaults_to_1(self):
        body = self._image_body("pruna-ai/p-image/edit-lora",
                                {"loraUrl": "https://host/a.safetensors"})
        self.assertEqual(body["lora_weights"], "https://host/a.safetensors")
        self.assertEqual(body["lora_scale"], 1)

    def test_hf_blob_url_normalized_to_resolve(self):
        body = self._image_body("flux-lora",
                                {"loraUrl": "https://huggingface.co/u/r/blob/main/a.safetensors"})
        self.assertEqual(body["lora_url"],
                         "https://huggingface.co/u/r/resolve/main/a.safetensors")

    def test_non_lora_model_sends_no_lora_keys(self):
        body = self._image_body("nano-banana-2", {"loraUrl": "https://host/a.safetensors"})
        self.assertFalse([k for k in body if "lora" in k.lower()])

    def test_civitai_lora_link_rejected_before_the_paid_call(self):
        wf = self.wf_dict({"nodes": [{"id": "n1", "type": "image",
                                      "fields": {"model": "flux-lora", "prompt": "p",
                                                 "loraUrl": "https://civitai.com/models/9"}}]})
        with self.assertRaises(RunError) as ctx:
            wf.run()
        self.assertIn("CivitAI links", str(ctx.exception))
        self.assertEqual(self.mock.requests, [])


if __name__ == "__main__":
    unittest.main()
