"""Media fields hold data:/http(s) URLs — prose placeholders, bare file paths and
non-strings are blanked at load with a how-to warning, so the input surfaces as
genuinely required instead of posting garbage to the API (parity with nanoodle-js)."""

import unittest

from nanoodle import NanoodleError, Workflow
from nanoodle.graph import materialize

PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="


class MediaPlaceholderTest(unittest.TestCase):
    def test_prose_placeholders_blanked_with_warning(self):
        g = materialize({
            "nodes": [
                {"id": "n1", "type": "upload",
                 "fields": {"image": "[image content will be provided separately]"}},
                {"id": "n2", "type": "aupload", "fields": {"audio": "the user's clip goes here"}},
                {"id": "n3", "type": "vupload", "fields": {"video": "./clips/intro.mp4"}},
                {"id": "n4", "type": "inpaint",
                 "fields": {"image": "photo of the user", "mask": {"path": "mask.png"}}},
            ],
            "links": [],
        })
        self.assertEqual(g.node("n1").fields["image"], "")
        self.assertEqual(g.node("n2").fields["audio"], "")
        self.assertEqual(g.node("n3").fields["video"], "")
        self.assertEqual(g.node("n4").fields["image"], "")
        self.assertEqual(g.node("n4").fields["mask"], "")
        self.assertEqual(len(g.warnings), 5)
        self.assertIn('fields.image held "[image content will be provided separately]"', g.warnings[0])
        self.assertIn("treated as empty", g.warnings[0])
        self.assertIn('--input "<key>=@file"', g.warnings[0])
        self.assertIn("a non-string value (dict)", g.warnings[4])

    def test_real_media_urls_untouched(self):
        g = materialize({
            "nodes": [
                {"id": "n1", "type": "upload", "fields": {"image": PNG}},
                {"id": "n2", "type": "aupload", "fields": {"audio": "https://cdn.example.com/c.mp3"}},
                {"id": "n3", "type": "vupload", "fields": {"video": "http://example.com/c.mp4"}},
                {"id": "n4", "type": "upload", "fields": {}},
                {"id": "n5", "type": "upload", "fields": {"image": ""}},
            ],
            "links": [],
        })
        self.assertEqual(g.node("n1").fields["image"], PNG)
        self.assertEqual(g.node("n2").fields["audio"], "https://cdn.example.com/c.mp3")
        self.assertEqual(g.node("n3").fields["video"], "http://example.com/c.mp4")
        self.assertEqual(g.warnings, [])

    def test_placeholder_upload_is_required_with_no_default(self):
        wf = Workflow.from_dict({
            "nodes": [{"id": "n1", "type": "upload",
                       "fields": {"image": "[will be supplied via --input]"}}],
            "links": [],
        }, api_key="k")
        self.assertEqual(len(wf.warnings), 1)
        img = next(i for i in wf.inputs if i.field == "image")
        self.assertFalse(img.optional)
        self.assertFalse(img.default)  # no fake "filled" default
        with self.assertRaises(NanoodleError) as ctx:
            wf.run({})
        self.assertIn("missing required input", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
