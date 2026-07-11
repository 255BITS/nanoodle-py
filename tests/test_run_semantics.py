"""Run-loop semantics: concurrency, progress events, overall timeout, run
isolation (a run never mutates the loaded workflow), result lookup sugar,
check_balance."""

import time
import unittest

from tests._util import MockedTest
from tests.harness import chat_response, image_response

from nanoodle import RunError

PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
           "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


class ConcurrencyTest(MockedTest):
    def test_parallel_lanes_overlap(self):
        self.mock.script("POST", "/api/v1/chat/completions",
                         dict(chat_response("slow"), delay=0.25))
        wf = self.wf("parallel-lanes.json")
        t0 = time.monotonic()
        result = wf.run()
        elapsed = time.monotonic() - t0
        self.assertEqual(len(self.mock.requests_to("/api/v1/chat/completions")), 2)
        self.assertGreaterEqual(self.mock.max_concurrent, 2)   # both lanes in flight at once
        self.assertLess(elapsed, 0.48)                          # ran concurrently, not serially
        self.assertEqual(result["Lane A"], "slow")
        self.assertEqual(result["Lane B"], "slow")


class ProgressTest(MockedTest):
    def test_event_sequence_per_node(self):
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("txt"))
        self.mock.script("POST", "/v1/images/generations", image_response(b64_list=[PNG_B64]))
        events = []
        wf = self.wf("starter-graph.json")
        wf.run({"Text": "x"}, on_progress=events.append)
        by_node = {}
        for e in events:
            by_node.setdefault(e["node_id"], []).append(e["type"])
        self.assertEqual(by_node["n1"], ["node-start", "node-done"])
        self.assertEqual(by_node["n2"], ["node-start", "node-done"])
        self.assertEqual(by_node["n3"], ["node-start", "node-done"])
        # events carry the display name
        names = {e["node_id"]: e["name"] for e in events}
        self.assertEqual(names["n2"], "LLM")
        done = [e for e in events if e["type"] == "node-done"]
        self.assertTrue(all("ms" in e for e in done))

    def test_node_error_event_emitted(self):
        self.mock.script("POST", "/api/v1/chat/completions", {"status": 500, "body": b"x"})
        events = []
        wf = self.wf("starter-graph.json")
        with self.assertRaises(RunError):
            wf.run({"Text": "x"}, on_progress=events.append)
        errs = [e for e in events if e["type"] == "node-error"]
        self.assertEqual({e["node_id"] for e in errs}, {"n2", "n3"})
        n3 = next(e for e in errs if e["node_id"] == "n3")
        self.assertIn("upstream failed", n3["error"])

    def test_broken_progress_callback_never_kills_the_run(self):
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("txt"))
        self.mock.script("POST", "/v1/images/generations", image_response(b64_list=[PNG_B64]))
        wf = self.wf("starter-graph.json")

        def bomb(evt):
            raise ValueError("callback bug")

        result = wf.run({"Text": "x"}, on_progress=bomb)
        self.assertEqual(result.errors, [])


class OverallTimeoutTest(MockedTest):
    def test_timeout_bounds_in_flight_nodes_and_returns_promptly(self):
        # regression: the deadline must bound the blocking wait too — a slow
        # in-flight node used to run to completion and return SUCCESS long
        # after the timeout, so run(timeout=...) had zero effect
        self.mock.script("POST", "/api/v1/chat/completions",
                         dict(chat_response("slow"), delay=0.5))
        wf = self.wf("starter-graph.json")
        t0 = time.monotonic()
        with self.assertRaises(RunError) as ctx:
            wf.run({"Text": "x"}, timeout=0.1)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.4)   # returned before the 0.5s node finished
        self.assertIn("timed out", str(ctx.exception))
        result = ctx.exception.result
        self.assertEqual(result.nodes["n2"].status, "error")   # in flight at deadline
        self.assertIn("timed out", result.nodes["n2"].error)
        self.assertEqual(result.nodes["n3"].status, "error")   # never scheduled
        self.assertIn("timed out", result.nodes["n3"].error)
        self.assertEqual(self.mock.requests_to("/v1/images/generations"), [])


class RunIsolationTest(MockedTest):
    def test_run_inputs_do_not_mutate_the_workflow(self):
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("txt"))
        self.mock.script("POST", "/v1/images/generations", image_response(b64_list=[PNG_B64]))
        wf = self.wf("starter-graph.json")
        wf.run({"Text": "override one"})
        self.assertEqual(wf.graph.node("n1").fields["text"],
                         "a cozy ramen shop on a rainy night")   # untouched
        self.mock.reset()
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("txt"))
        self.mock.script("POST", "/v1/images/generations", image_response(b64_list=[PNG_B64]))
        wf.run()   # second run uses the pristine default again
        chat = self.mock.requests_to("/api/v1/chat/completions")[0]
        self.assertEqual(chat.json["messages"][1]["content"],
                         "a cozy ramen shop on a rainy night")

    def test_settings_do_not_stick_between_runs(self):
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("txt"))
        self.mock.script("POST", "/v1/images/generations", image_response(b64_list=[PNG_B64]))
        wf = self.wf("starter-graph.json")
        wf.run({"Text": "x"}, settings={"n3.size": "2k"})
        self.assertEqual(self.mock.requests_to("/v1/images/generations")[0].json["size"], "2k")
        self.mock.reset()
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("txt"))
        self.mock.script("POST", "/v1/images/generations", image_response(b64_list=[PNG_B64]))
        wf.run({"Text": "x"})
        self.assertEqual(self.mock.requests_to("/v1/images/generations")[0].json["size"], "1k")


class ResultLookupTest(MockedTest):
    def _run(self):
        self.mock.script("POST", "/api/v1/chat/completions", chat_response("txt"))
        self.mock.script("POST", "/v1/images/generations", image_response(b64_list=[PNG_B64]))
        return self.wf("starter-graph.json").run({"Text": "x"})

    def test_case_insensitive_getitem_get_and_contains(self):
        result = self._run()
        self.assertIs(result["image"], result["Image"])   # case-insensitive
        self.assertIs(result["n3"], result["Image"])      # node-id key
        self.assertIn("Image", result)
        self.assertNotIn("Video", result)
        self.assertEqual(result.get("missing", "dflt"), "dflt")
        with self.assertRaises(KeyError) as ctx:
            result["missing"]
        self.assertIn("Image", str(ctx.exception))         # lists available keys


class CheckBalanceTest(MockedTest):
    def test_check_balance_posts_empty_object(self):
        self.mock.script("POST", "/api/check-balance",
                         {"status": 200, "json": {"usd_balance": "5.25"}})
        wf = self.wf("starter-graph.json")
        self.assertEqual(wf.check_balance(), 5.25)
        req = self.mock.requests_to("/api/check-balance")[0]
        self.assertEqual(req.json, {})
        self.assertEqual(req.headers.get("authorization"), "Bearer test-key")
        self.assertEqual(req.headers.get("x-api-key"), "test-key")


if __name__ == "__main__":
    unittest.main()
