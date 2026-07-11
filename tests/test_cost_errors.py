"""Cost extraction priority (SPEC-engine costFromJson) incl. zero-cost-kept and
header fallback, balance sources, HTTP error mapping (401/402/403/500), and the
no-key-leak guarantee."""

import unittest

from tests._util import MockedTest
from tests.harness import chat_response

from nanoodle import RunError


class CostPriorityTest(MockedTest):
    def _one_llm(self, **opts):
        return self.wf_dict({"nodes": [{"id": "n1", "type": "llm",
                                        "fields": {"model": "m", "prompt": "p"}}]}, **opts)

    def _chat_with(self, extra_json=None, headers=None):
        j = {"choices": [{"message": {"content": "ok"}}]}
        j.update(extra_json or {})
        self.mock.script("POST", "/api/v1/chat/completions",
                         {"status": 200, "json": j, "headers": headers or {}})

    def test_top_level_positive_cost_wins(self):
        self._chat_with({"cost": 0.5, "x_nanogpt_pricing": {"costUsd": 0.1}})
        self.assertAlmostEqual(self._one_llm().run().cost_usd, 0.5)

    def test_pricing_cost_usd_beats_cost_and_amount(self):
        self._chat_with({"x_nanogpt_pricing": {"costUsd": 0.001, "cost": 0.002,
                                               "amount": 0.003}})
        self.assertAlmostEqual(self._one_llm().run().cost_usd, 0.001)

    def test_pricing_cost_beats_amount(self):
        self._chat_with({"x_nanogpt_pricing": {"cost": 0.002, "amount": 0.003}})
        self.assertAlmostEqual(self._one_llm().run().cost_usd, 0.002)

    def test_pricing_amount_fallback(self):
        self._chat_with({"x_nanogpt_pricing": {"amount": 0.004}})
        self.assertAlmostEqual(self._one_llm().run().cost_usd, 0.004)

    def test_zero_top_level_cost_defers_to_pricing(self):
        self._chat_with({"cost": 0, "x_nanogpt_pricing": {"costUsd": 0.006}})
        self.assertAlmostEqual(self._one_llm().run().cost_usd, 0.006)

    def test_metadata_cost_fallback(self):
        self._chat_with({"metadata": {"cost": 0.008}})
        result = self._one_llm().run()
        self.assertAlmostEqual(result.cost_usd, 0.008)
        self.assertTrue(result.cost_exact)

    def test_zero_cost_is_known_free(self):
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("ok", cost_usd=0))
        result = self._one_llm().run()
        self.assertEqual(result.cost_usd, 0.0)
        self.assertTrue(result.cost_exact)   # present-but-zero = known-free

    def test_top_level_zero_kept_when_nothing_else(self):
        self._chat_with({"cost": 0})
        result = self._one_llm().run()
        self.assertEqual(result.cost_usd, 0.0)
        self.assertTrue(result.cost_exact)

    def test_header_x_cost_fallback_when_json_silent(self):
        self._chat_with(headers={"x-cost": "0.007"})
        result = self._one_llm().run()
        self.assertAlmostEqual(result.cost_usd, 0.007)
        self.assertTrue(result.cost_exact)

    def test_header_x_nano_cost_fallback(self):
        self._chat_with(headers={"x-nano-cost": "0.009"})
        self.assertAlmostEqual(self._one_llm().run().cost_usd, 0.009)

    def test_missing_cost_marks_inexact(self):
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("ok"))
        result = self._one_llm().run()
        self.assertEqual(result.cost_usd, 0.0)
        self.assertFalse(result.cost_exact)

    def test_per_node_cost_attribution(self):
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("ok", cost_usd=0.003))
        result = self._one_llm().run()
        self.assertAlmostEqual(result.nodes["n1"].cost_usd, 0.003)


class BalanceTest(MockedTest):
    def _one_llm(self):
        return self.wf_dict({"nodes": [{"id": "n1", "type": "llm",
                                        "fields": {"model": "m", "prompt": "p"}}]})

    def test_header_beats_json_balance(self):
        j = {"choices": [{"message": {"content": "ok"}}],
             "x_nanogpt_pricing": {"amount": 0.004, "remainingBalance": 9.0}}
        self.mock.script("POST", "/api/v1/chat/completions",
                         {"status": 200, "json": j,
                          "headers": {"x-remaining-balance": "8.5"}})
        result = self._one_llm().run()
        self.assertAlmostEqual(result.cost_usd, 0.004)
        self.assertEqual(result.remaining_balance, 8.5)   # header wins

    def test_top_level_remaining_balance(self):
        j = {"choices": [{"message": {"content": "ok"}}], "remainingBalance": 3.25}
        self.mock.script("POST", "/api/v1/chat/completions", {"status": 200, "json": j})
        self.assertEqual(self._one_llm().run().remaining_balance, 3.25)

    def test_pricing_remaining_balance_fallback(self):
        j = {"choices": [{"message": {"content": "ok"}}],
             "x_nanogpt_pricing": {"remainingBalance": 2.5}}
        self.mock.script("POST", "/api/v1/chat/completions", {"status": 200, "json": j})
        self.assertEqual(self._one_llm().run().remaining_balance, 2.5)


class HttpErrorMappingTest(MockedTest):
    def _one_llm(self):
        return self.wf_dict({"nodes": [{"id": "n1", "type": "llm",
                                        "fields": {"model": "m", "prompt": "p"}}]},
                            api_key="secret-key-123")

    def test_401_maps_to_auth_error_and_never_leaks_key(self):
        self.mock.script("POST", "/api/v1/chat/completions",
                         {"status": 401, "body": b'{"error":"bad key"}'})
        with self.assertRaises(RunError) as ctx:
            self._one_llm().run()
        msg = str(ctx.exception)
        self.assertIn("API key rejected", msg)
        self.assertNotIn("secret-key-123", msg)
        self.assertNotIn("secret-key-123", repr(ctx.exception.result))
        self.assertNotIn("secret-key-123", repr(ctx.exception.result.nodes["n1"]))

    def test_403_maps_to_auth_error(self):
        self.mock.script("POST", "/api/v1/chat/completions",
                         {"status": 403, "body": b"forbidden"})
        with self.assertRaises(RunError) as ctx:
            self._one_llm().run()
        self.assertIn("API key rejected", str(ctx.exception))

    def test_401_with_funds_looking_body_is_still_auth(self):
        # ground truth (play.html isLowFundsError): 401/403 are auth's territory
        self.mock.script("POST", "/api/v1/chat/completions",
                         {"status": 401, "body": b"insufficient permissions"})
        with self.assertRaises(RunError) as ctx:
            self._one_llm().run()
        self.assertIn("API key rejected", str(ctx.exception))
        self.assertNotIn("out of balance", str(ctx.exception))

    def test_402_maps_to_out_of_funds(self):
        self.mock.script("POST", "/api/v1/chat/completions",
                         {"status": 402, "body": b"payment required"})
        with self.assertRaises(RunError) as ctx:
            self._one_llm().run()
        self.assertIn("out of balance", str(ctx.exception))

    def test_funds_body_on_500_maps_to_out_of_funds(self):
        self.mock.script("POST", "/api/v1/chat/completions",
                         {"status": 500, "body": b"insufficient funds for model"})
        with self.assertRaises(RunError) as ctx:
            self._one_llm().run()
        self.assertIn("out of balance", str(ctx.exception))

    def test_500_maps_to_status_prefixed_error_truncated_to_160(self):
        self.mock.script("POST", "/api/v1/chat/completions",
                         {"status": 500, "body": b"X" * 500})
        with self.assertRaises(RunError) as ctx:
            self._one_llm().run()
        self.assertIn("500: " + "X" * 160, str(ctx.exception))
        self.assertNotIn("X" * 161, str(ctx.exception))

    def test_transport_error_message_strips_query_and_key(self):
        from nanoodle import NanoodleError
        from nanoodle.transport import default_http
        with self.assertRaises(NanoodleError) as ctx:
            # port 1 is never listening; connection refused locally, no traffic leaves
            default_http("GET", "http://127.0.0.1:1/path?token=secret-key-123", timeout=0.5)
        msg = str(ctx.exception)
        self.assertIn("could not reach", msg)
        self.assertNotIn("secret-key-123", msg)


class PartialResultTest(MockedTest):
    def test_run_error_carries_partial_results(self):
        self.mock.script("POST", "/api/v1/chat/completions",
                         chat_response("prompt text", cost_usd=0.001))
        self.mock.script("POST", "/v1/images/generations", {"status": 500, "body": b"exploded"})
        wf = self.wf("starter-graph.json")
        with self.assertRaises(RunError) as ctx:
            wf.run({"Text": "x"})
        result = ctx.exception.result
        self.assertEqual(result.nodes["n2"].status, "done")     # llm succeeded
        self.assertEqual(result.nodes["n2"].out["text"], "prompt text")
        self.assertEqual(result.nodes["n3"].status, "error")
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0]["node_id"], "n3")
        self.assertEqual(result.errors[0]["name"], "Image")
        self.assertAlmostEqual(result.cost_usd, 0.001)          # partial cost kept

    def test_upstream_failure_names_the_upstream(self):
        self.mock.script("POST", "/api/v1/chat/completions", {"status": 500, "body": b"dead"})
        wf = self.wf("starter-graph.json")
        with self.assertRaises(RunError) as ctx:
            wf.run({"Text": "x"})
        result = ctx.exception.result
        self.assertEqual(result.nodes["n3"].status, "error")
        self.assertEqual(result.nodes["n3"].error, "upstream failed: LLM")
        # the image endpoint was never called
        self.assertEqual(self.mock.requests_to("/v1/images/generations"), [])

    def test_independent_lane_still_completes_when_sibling_fails(self):
        # Lane A's model call dies; Lane B still runs and keeps its output
        self.mock.script("POST", "/api/v1/chat/completions", [
            {"status": 500, "body": b"boom"},
            chat_response("fine"),
        ])
        wf = self.wf("parallel-lanes.json")
        with self.assertRaises(RunError) as ctx:
            wf.run()
        result = ctx.exception.result
        statuses = sorted(result.nodes[n].status for n in ("n2", "n3"))
        self.assertEqual(statuses, ["done", "error"])
        done = "n2" if result.nodes["n2"].status == "done" else "n3"
        self.assertEqual(result.nodes[done].out["text"], "fine")
        self.assertEqual(len(result.errors), 1)
        # the RunError message names only the FAILED sink
        self.assertIn("output node", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
