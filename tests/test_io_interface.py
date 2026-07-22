"""The workflow public interface: input/output/setting derivation and every
key-resolution order incl. ambiguity errors (SPEC-io)."""

import unittest

from tests import fixture

from nanoodle import NanoodleError, Workflow
from nanoodle.iodef import resolve_input_key, resolve_setting_key


class InputDerivationTest(unittest.TestCase):
    def test_wired_fields_hidden(self):
        wf = Workflow.load(fixture("starter-graph.json"), api_key="k")
        pairs = [(s.node_id, s.field) for s in wf.inputs]
        self.assertIn(("n1", "text"), pairs)
        self.assertIn(("n2", "system"), pairs)
        self.assertNotIn(("n2", "prompt"), pairs)   # wired -> hidden
        self.assertNotIn(("n3", "prompt"), pairs)   # wired -> hidden

    def test_optional_flag_and_defaults(self):
        wf = Workflow.load(fixture("starter-graph.json"), api_key="k")
        by_field = {(s.node_id, s.field): s for s in wf.inputs}
        text = by_field[("n1", "text")]
        self.assertFalse(text.optional)
        self.assertEqual(text.default, "a cozy ramen shop on a rainy night")
        system = by_field[("n2", "system")]
        self.assertTrue(system.optional)
        self.assertTrue(system.default.startswith("You write image prompts."))

    def test_llm_system_spec_default_when_field_empty(self):
        wf = Workflow.from_dict({"nodes": [
            {"id": "n1", "type": "llm", "fields": {"model": "m", "prompt": "p"}}]}, api_key="k")
        system = next(s for s in wf.inputs if s.field == "system")
        self.assertEqual(system.default, "You are a helpful, concise assistant.")

    def test_inpaint_specials_nothing_wired(self):
        wf = Workflow.from_dict({"nodes": [
            {"id": "n1", "type": "inpaint", "fields": {"model": "m"}}]}, api_key="k")
        specs = {(s.node_id, s.field): s for s in wf.inputs}
        self.assertIn(("n1", "prompt"), specs)
        self.assertEqual(specs[("n1", "prompt")].label, "What to paint in")
        # BOTH image and mask surfaced (play.html's combined upload+brush
        # control writes both fields) — otherwise the mask could never be
        # supplied and the workflow would be unrunnable
        self.assertIn(("n1", "image"), specs)
        self.assertIn(("n1", "mask"), specs)
        self.assertEqual(specs[("n1", "image")].label, "Image — brush the area to repaint")
        self.assertEqual(specs[("n1", "mask")].label, "Mask (white = repaint)")

    def test_inpaint_mask_input_when_image_wired(self):
        wf = Workflow.from_dict({"nodes": [
            {"id": "n1", "type": "upload", "fields": {"image": "data:image/png;base64,AAA="}},
            {"id": "n2", "type": "inpaint", "fields": {"model": "m"}},
        ], "links": [{"id": "l1", "from": {"node": "n1", "port": "image"},
                      "to": {"node": "n2", "port": "image"}}]}, api_key="k")
        specs = {(s.node_id, s.field): s for s in wf.inputs}
        self.assertIn(("n2", "mask"), specs)
        self.assertEqual(specs[("n2", "mask")].label, "Mask (white = repaint)")
        self.assertNotIn(("n2", "image"), specs)

    def test_choice_input_options_and_default(self):
        wf = Workflow.load(fixture("join-choice.json"), api_key="k")
        choice = next(s for s in wf.inputs if s.kind == "choice")
        self.assertEqual(choice.options, ["red", "blue", "green"])
        self.assertEqual(choice.default, "blue")

    def test_custom_name_single_required_input_names_it(self):
        wf = Workflow.load(fixture("field-override.json"), api_key="k")
        keys = {s.key for s in wf.inputs}
        self.assertIn("Persona", keys)   # PR #138 flat-label rule
        self.assertIn("Prompt", keys)    # n2's unwired prompt keeps its generic label

    def test_duplicate_labels_get_numeric_suffixes(self):
        # cross-language parity: duplicate input labels are suffixed ' 2', ' 3'
        # (like output keys, SPEC-io), matching the JS library — NOT renamed to
        # nodeId.field, which made wf.inputs keys differ between languages
        wf = Workflow.load(fixture("duplicate-names.json"), api_key="k")
        keys = sorted(s.key for s in wf.inputs)
        self.assertEqual(keys, ["System prompt", "System prompt 2", "Text", "Text 2"])
        # friendly keys stay addressable and resolve in derivation order
        first = resolve_input_key(wf.inputs, "Text", wf.graph)
        self.assertEqual((first.node_id, first.field), ("n1", "text"))
        second = resolve_input_key(wf.inputs, "text 2", wf.graph)   # case-insensitive
        self.assertEqual((second.node_id, second.field), ("n2", "text"))


class KeyResolutionTest(unittest.TestCase):
    def test_resolution_orders(self):
        wf = Workflow.load(fixture("field-override.json"), api_key="k")
        specs = wf.inputs
        by_name = resolve_input_key(specs, " persona ", wf.graph)   # trimmed, case-insensitive
        self.assertEqual((by_name.node_id, by_name.field), ("n1", "text"))
        by_id_field = resolve_input_key(specs, "n1.text", wf.graph)
        self.assertIs(by_id_field, by_name)
        bare_id = resolve_input_key(specs, "n2", wf.graph)          # single input on the node
        self.assertEqual(bare_id.field, "prompt")
        by_label = resolve_input_key(specs, "PROMPT", wf.graph)
        self.assertIs(by_label, bare_id)
        by_field = resolve_input_key(specs, "text", wf.graph)
        self.assertIs(by_field, by_name)

    def test_unknown_key_lists_available(self):
        wf = Workflow.load(fixture("field-override.json"), api_key="k")
        with self.assertRaises(NanoodleError) as ctx:
            wf.run({"Bogus": "x"})
        msg = str(ctx.exception)
        self.assertIn("Bogus", msg)
        self.assertIn("Persona", msg)
        self.assertIn("Prompt", msg)

    def test_duplicate_custom_names_ambiguous(self):
        wf = Workflow.load(fixture("duplicate-names.json"), api_key="k")
        with self.assertRaises(NanoodleError) as ctx:
            wf.run({"Writer": "x"})   # two nodes named Writer
        self.assertIn("ambiguous", str(ctx.exception))

    def test_custom_name_resolves_to_single_required_input(self):
        # regression: a named llm with prompt AND optional system derived — the
        # advertised key ("Poet", assigned by the PR #138 flat-label rule) MUST
        # resolve to the required prompt, not raise "ambiguous"
        wf = Workflow.from_dict({"nodes": [
            {"id": "n1", "type": "llm", "fields": {"model": "m"}, "name": "Poet"}]},
            api_key="k")
        self.assertIn("Poet", [s.key for s in wf.inputs])   # the advertised key
        spec = resolve_input_key(wf.inputs, "Poet", wf.graph)
        self.assertEqual((spec.node_id, spec.field), ("n1", "prompt"))

    def test_same_custom_name_on_two_nodes_resolves_by_advertised_key(self):
        # two DIFFERENT nodes sharing one name: keys are suffixed ("Poet",
        # "Poet 2") and each ADVERTISED key resolves to its own node — the
        # assigned-key check runs before the custom-name check (JS parity)
        wf = Workflow.from_dict({"nodes": [
            {"id": "n1", "type": "llm", "fields": {"model": "m"}, "name": "Poet"},
            {"id": "n2", "type": "image", "fields": {"model": "m"}, "name": "Poet"}]},
            api_key="k")
        self.assertEqual([s.key for s in wf.inputs if s.field == "prompt"],
                         ["Poet", "Poet 2"])
        first = resolve_input_key(wf.inputs, "Poet", wf.graph)
        self.assertEqual((first.node_id, first.field), ("n1", "prompt"))
        second = resolve_input_key(wf.inputs, "Poet 2", wf.graph)
        self.assertEqual((second.node_id, second.field), ("n2", "prompt"))

    def test_bare_scalar_refused_with_multiple_required(self):
        wf = Workflow.load(fixture("llm-vision.json"), api_key="k")
        with self.assertRaises(NanoodleError) as ctx:
            wf.run("hello")
        self.assertIn("exactly one required input", str(ctx.exception))

    def test_missing_required_input_named(self):
        wf = Workflow.from_dict({"nodes": [
            {"id": "n1", "type": "llm", "fields": {"model": "m"}},
        ]}, api_key="k")
        with self.assertRaises(NanoodleError) as ctx:
            wf.run()
        self.assertIn("missing required input", str(ctx.exception))
        self.assertIn("Prompt", str(ctx.exception))

    def test_choice_value_validated_against_options(self):
        wf = Workflow.load(fixture("join-choice.json"), api_key="k")
        with self.assertRaises(NanoodleError) as ctx:
            wf.run({"Choice": "purple"})
        self.assertIn("options", str(ctx.exception))


class OutputDerivationTest(unittest.TestCase):
    def test_sinks_only(self):
        wf = Workflow.load(fixture("starter-graph.json"), api_key="k")
        self.assertEqual([(o.key, o.node_id, o.type) for o in wf.outputs],
                         [("Image", "n3", "image")])
        self.assertEqual(wf.outputs[0].ports, ["image"])

    def test_duplicate_output_names_suffixed_in_topo_order(self):
        wf = Workflow.load(fixture("duplicate-names.json"), api_key="k")
        self.assertEqual([o.key for o in wf.outputs], ["Writer", "Writer 2"])
        self.assertEqual([o.node_id for o in wf.outputs], ["n3", "n4"])

class SettingsTest(unittest.TestCase):
    def test_settings_derived_with_current_values(self):
        wf = Workflow.load(fixture("starter-graph.json"), api_key="k")
        settings = {s.key: s for s in wf.settings}
        self.assertIn("n2.model", settings)
        self.assertEqual(settings["n2.model"].default, "zai-org/glm-5.2")
        self.assertEqual(settings["n3.size"].default, "1k")
        self.assertNotIn("n3.prompt", settings)   # wired IO, never a setting

    def test_wired_setting_refused(self):
        wf = Workflow.load(fixture("starter-graph.json"), api_key="k")
        with self.assertRaises(NanoodleError) as ctx:
            wf.run({"Text": "x"}, settings={"n3.prompt": "sneaky"})
        self.assertIn("wired", str(ctx.exception))

    def test_unknown_setting_lists_available(self):
        wf = Workflow.load(fixture("starter-graph.json"), api_key="k")
        with self.assertRaises(NanoodleError) as ctx:
            wf.run({"Text": "x"}, settings={"n9.zap": "1"})
        msg = str(ctx.exception)
        self.assertIn("unknown setting", msg)
        self.assertIn("n2.model", msg)

    def test_setting_resolved_by_unique_field_name(self):
        wf = Workflow.load(fixture("starter-graph.json"), api_key="k")
        spec = resolve_setting_key(wf.settings, "size", wf.graph)
        self.assertEqual((spec.node_id, spec.field), ("n3", "size"))

    def test_ambiguous_setting_field_errors(self):
        wf = Workflow.load(fixture("starter-graph.json"), api_key="k")
        with self.assertRaises(NanoodleError) as ctx:
            resolve_setting_key(wf.settings, "model", wf.graph)   # n2.model and n3.model
        self.assertIn("ambiguous", str(ctx.exception))

    def test_setting_dot_form_matches_custom_name_and_title(self):
        # cross-language parity: "CustomName.field" / "Title.field" resolve like
        # "nodeId.field" (SPEC-io: settings resolve the same way as inputs)
        wf = Workflow.from_dict({"nodes": [
            {"id": "n1", "type": "llm", "fields": {"model": "a"}, "name": "Writer"},
            {"id": "n2", "type": "tts", "fields": {"model": "b", "prompt": "p"}}]},
            api_key="k")
        spec = resolve_setting_key(wf.settings, "Writer.model", wf.graph)
        self.assertEqual((spec.node_id, spec.field), ("n1", "model"))
        spec2 = resolve_setting_key(wf.settings, "speech.voice", wf.graph)   # type title
        self.assertEqual((spec2.node_id, spec2.field), ("n2", "voice"))

    def test_image_size_options_match_the_app(self):
        # ground truth play.html SIZES — no invented values, 'auto' included
        wf = Workflow.from_dict({"nodes": [
            {"id": "n1", "type": "image", "fields": {"model": "m", "prompt": "p"}}]},
            api_key="k")
        size = next(s for s in wf.settings if s.field == "size")
        self.assertEqual(size.options, ["1024x1024", "1024x1536", "1536x1024", "auto"])

    def test_custom_civitai_air_setting_surfaces(self):
        wf = Workflow.from_dict({"nodes": [
            {"id": "n1", "type": "image",
             "fields": {"model": "custom-civitai", "prompt": "p",
                        "customCivitaiAir": "urn:air:x"}}]}, api_key="k")
        keys = {s.key for s in wf.settings}
        self.assertIn("n1.customCivitaiAir", keys)


if __name__ == "__main__":
    unittest.main()
