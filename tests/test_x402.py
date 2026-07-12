"""x402 accountless payments: invoice parsing pinned to a REAL captured 402
(tests/fixtures/x402/402.json, byte-identical to nanoodle-js's copy) plus the
full settle flow against an injectable transport. Mirrors nanoodle-js
tests/x402.test.mjs behavior for behavior."""

import json
import time
import unittest

from tests import fixture

from nanoodle import NanoodleError, Workflow
from nanoodle.engine import Engine
from nanoodle.transport import HttpResponse
from nanoodle.x402 import assert_payment_option, looks_like_result, parse_nano_invoice

with open(fixture("x402/402.json"), "r", encoding="utf-8") as f:
    FIXTURE_402 = json.load(f)

BASE = "https://nano-gpt.com"
CHAT_OK = {"choices": [{"message": {"content": "paid hello"}}], "cost": 0.0001}


def fresh_402(minutes=15):
    """The fixture's real expiresAt is ~15 min after capture — long dead by test
    time. Settle-flow tests need a live window; parser tests use the raw fixture."""
    j = json.loads(json.dumps(FIXTURE_402))
    secs = int(time.time()) + minutes * 60
    for a in j.get("accepts", []):
        a["expiresAt"] = secs
    for a in (j.get("payment") or {}).get("accepted", []):
        a["expiresAt"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(secs))
    if j.get("payment"):
        j["payment"]["expiresAt"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(secs))
    return j


def json_resp(status, body):
    return HttpResponse(status, {"Content-Type": "application/json"},
                        json.dumps(body).encode("utf-8"))


def make_engine(http, payment, api_key=None):
    return Engine(api_key, BASE, http, poll_intervals={"x402": 0.001}, payment=payment)


class ParseInvoiceTest(unittest.TestCase):
    def test_real_fixture(self):
        inv = parse_nano_invoice(FIXTURE_402, BASE)
        self.assertEqual(inv["scheme"], "nano")
        self.assertRegex(inv["paymentId"], r"^pay_[0-9a-f]+$")
        self.assertRegex(inv["payTo"], r"^nano_[a-z0-9]+$")
        self.assertRegex(inv["amountRaw"], r"^\d+$")  # integer raw units
        self.assertTrue(inv["amount"].endswith("XNO"))
        self.assertGreater(inv["amountUsd"], 0)
        self.assertEqual(inv["uri"], "nano:%s?amount=%s" % (inv["payTo"], inv["amountRaw"]))
        self.assertTrue(inv["statusUrl"].startswith(BASE + "/api/x402/status/pay_"))
        self.assertTrue(inv["completeUrl"].startswith(BASE + "/api/x402/complete/pay_"))
        self.assertGreater(inv["expiresAt"], 1e12)  # epoch ms
        self.assertTrue(inv["explorerUrl"].startswith("https://"))

    def test_parity_with_js_invoice_fields(self):
        # same fixture, same field names as nanoodle-js's parseNanoInvoice — the
        # invoice a payment callback sees must be interchangeable between libs
        inv = parse_nano_invoice(FIXTURE_402, BASE)
        self.assertEqual(
            sorted(inv.keys()),
            sorted(["scheme", "paymentId", "payTo", "amountRaw", "amount", "amountUsd",
                    "uri", "expiresAt", "statusUrl", "completeUrl", "explorerUrl",
                    "description", "requestHash"]))

    def test_no_nano_option_returns_none(self):
        stripped = json.loads(json.dumps(FIXTURE_402))
        stripped["accepts"] = [a for a in stripped["accepts"] if a["scheme"] != "nano"]
        stripped["payment"]["accepted"] = []
        self.assertIsNone(parse_nano_invoice(stripped, BASE))

    def test_looks_like_result(self):
        self.assertTrue(looks_like_result(CHAT_OK))
        self.assertTrue(looks_like_result({"data": [{"b64_json": "x"}]}))
        self.assertFalse(looks_like_result({"status": "completed", "paymentId": "pay_x"}))
        self.assertFalse(looks_like_result(None))


class SettleFlowTest(unittest.TestCase):
    def chat(self, engine):
        resp = engine._post_json("/api/v1/chat/completions",
                                 {"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        return json.loads(resp.text())

    def test_replayed_result_no_repost(self):
        calls = []
        invoices = []

        def http(method, url, headers=None, body=None, timeout=None):
            calls.append((method, url, headers))
            if "/chat/completions" in url:
                return json_resp(402, fresh_402())
            if "/api/x402/complete/" in url:
                n = sum(1 for c in calls if "/complete/" in c[1])
                if n == 1:
                    return json_resp(402, {"error": "Payment not verified", "status": "pending"})
                return json_resp(200, CHAT_OK)
            raise AssertionError("unexpected url " + url)

        eng = make_engine(http, invoices.append)
        self.assertEqual(self.chat(eng), CHAT_OK)
        self.assertEqual(len(invoices), 1, "exactly one payment per request")
        self.assertEqual(invoices[0]["payTo"], parse_nano_invoice(FIXTURE_402, BASE)["payTo"])
        first_headers = calls[0][2]
        self.assertEqual(first_headers.get("x-x402"), "true")
        self.assertNotIn("Authorization", first_headers, "keyless request carries no Authorization")
        self.assertEqual(sum(1 for c in calls if "/chat/completions" in c[1]), 1,
                         "result came from complete, not a re-POST")

    def test_settle_only_reposts_with_payment_id(self):
        calls = []

        def http(method, url, headers=None, body=None, timeout=None):
            calls.append((method, url, headers))
            if "/chat/completions" in url:
                if headers.get("x-x402-payment-id"):
                    return json_resp(200, CHAT_OK)
                return json_resp(402, fresh_402())
            if "/api/x402/complete/" in url:
                return json_resp(200, {"status": "completed", "paymentId": "pay_x"})
            raise AssertionError("unexpected url " + url)

        eng = make_engine(http, lambda inv: None)
        self.assertEqual(self.chat(eng), CHAT_OK)
        reposts = [c for c in calls if "/chat/completions" in c[1] and c[2].get("x-x402-payment-id")]
        self.assertEqual(len(reposts), 1)
        self.assertRegex(reposts[0][2]["x-x402-payment-id"], r"^pay_")

    def test_second_402_after_settle_is_hard_error(self):
        paid = []

        def http(method, url, headers=None, body=None, timeout=None):
            if "/chat/completions" in url:
                return json_resp(402, fresh_402())
            return json_resp(200, {"status": "completed"})

        eng = make_engine(http, lambda inv: paid.append(1))
        with self.assertRaisesRegex(NanoodleError, "still answered 402"):
            self.chat(eng)
        self.assertEqual(len(paid), 1, "never a second payment")

    def test_expired_window(self):
        expired = json.loads(json.dumps(FIXTURE_402))
        for a in expired["accepts"]:
            a["expiresAt"] = int(time.time()) - 60
        expired["payment"]["expiresAt"] = "2020-01-01T00:00:00.000Z"

        def http(method, url, headers=None, body=None, timeout=None):
            if "/chat/completions" in url:
                return json_resp(402, expired)
            return json_resp(402, {"error": "Payment not verified"})

        eng = make_engine(http, lambda inv: None)
        with self.assertRaisesRegex(NanoodleError, "expired.*nano_"):
            self.chat(eng)

    def test_no_nano_option_is_actionable_and_unpaid(self):
        paid = []
        no_nano = {"accepts": [a for a in FIXTURE_402["accepts"] if a["scheme"] != "nano"]}

        def http(method, url, headers=None, body=None, timeout=None):
            return json_resp(402, no_nano)

        eng = make_engine(http, lambda inv: paid.append(1))
        with self.assertRaisesRegex(NanoodleError, "no usable Nano option"):
            self.chat(eng)
        self.assertEqual(paid, [])

    def test_keyed_mode_ignores_payment(self):
        paid = []

        def http(method, url, headers=None, body=None, timeout=None):
            return json_resp(402, fresh_402())

        eng = make_engine(http, lambda inv: paid.append(1), api_key="k")
        with self.assertRaisesRegex(NanoodleError, "out of balance"):
            self.chat(eng)
        self.assertEqual(paid, [])


class GuardTest(unittest.TestCase):
    def test_seed_string_refused(self):
        with self.assertRaisesRegex(NanoodleError, "never accepts wallet seeds or private keys"):
            assert_payment_option("vault grief snake ... twelve words")
        with self.assertRaisesRegex(NanoodleError, "never accepts wallet seeds"):
            Workflow.from_dict({"nodes": [], "links": []}, payment="seed")

    def test_workflow_keyless_with_payment_passes_guard(self):
        # regression twin of the js env-fallback bug: api_key="" must stay keyless
        # even when NANOGPT_API_KEY is set, and payment= satisfies the network guard
        import os
        prev = os.environ.get("NANOGPT_API_KEY")
        os.environ["NANOGPT_API_KEY"] = "sk-should-never-be-used"
        try:
            wf = Workflow.from_dict(
                {"nodes": [{"id": "n1", "type": "text", "x": 0, "y": 0, "fields": {"text": "hi"}}],
                 "links": []},
                api_key="", payment=lambda inv: None)
            self.assertFalse(wf._api_key)
            self.assertIsNotNone(wf._payment)
        finally:
            if prev is None:
                del os.environ["NANOGPT_API_KEY"]
            else:
                os.environ["NANOGPT_API_KEY"] = prev


if __name__ == "__main__":
    unittest.main()
