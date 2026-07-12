"""x402 accountless payments (NanoGPT) — no API key, pay per call in Nano (XNO).

Request with ``x-x402: true`` and no key, get HTTP 402 with payment options,
pay in Nano, call the complete URL, receive the original response.

The library NEVER holds funds or keys: the actual send happens inside the
user-supplied ``payment`` callback (their own wallet, signer, or a human
scanning a QR). Wire shape live-verified against nano-gpt.com 2026-07-12
(tests/fixtures/x402/402.json is a real captured response, byte-identical to
nanoodle-js's copy).
"""

import json
import re
import time
import calendar
import urllib.parse

from .errors import NanoodleError

_NANO_ADDR_RE = re.compile(r"^nano_[a-z0-9]+$")
_RESULT_KEYS = ("choices", "data", "output", "runId", "url", "audioUrl", "transcription", "text")


def assert_payment_option(payment):
    """Reject anything that isn't a callback — a seed/private key must never reach this library."""
    if payment is None:
        return
    if not callable(payment):
        raise NanoodleError(
            "payment must be a callback function — nanoodle never accepts wallet seeds or "
            "private keys. Do the send inside your callback with your own wallet/signer "
            "(it receives the invoice dict: payTo, amountRaw, uri, ...).")


def _to_ms(v):
    """ISO string or unix seconds → epoch ms (None when absent/unparsable)."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v * 1000) if v < 1e12 else int(v)
    try:
        # ISO 8601 like 2026-07-12T03:31:43.000Z
        s = str(v).replace("Z", "").split(".")[0]
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%S")) * 1000
    except ValueError:
        return None


def parse_nano_invoice(body, base_url):
    """Pull the Nano payment option out of a 402 response body.

    Looks in the x402-standard ``accepts`` array first, then the NanoGPT
    ``payment.accepted`` list. Returns an invoice dict for the payment
    callback, or None when no Nano option is offered (other rails are never
    silently used)."""
    if not isinstance(body, dict):
        return None
    pay = body.get("payment") if isinstance(body.get("payment"), dict) else None
    pool = []
    if isinstance(body.get("accepts"), list):
        pool.extend(body["accepts"])
    if pay and isinstance(pay.get("accepted"), list):
        pool.extend(pay["accepted"])
    nano = None
    for a in pool:
        if isinstance(a, dict) and a.get("scheme") == "nano" and _NANO_ADDR_RE.match(str(a.get("payTo") or "")):
            nano = a
            break
    if nano is None:
        return None
    payment_id = nano.get("paymentId") or (pay or {}).get("paymentId")

    def _abs(u):
        return urllib.parse.urljoin(base_url + "/", u) if u else None

    amount_raw = str(nano.get("maxAmountRequired") or nano.get("amount") or "")
    usd = nano.get("maxAmountRequiredUSD", nano.get("amountUsd", (pay or {}).get("amountUsd")))
    try:
        usd = float(usd) if usd is not None else None
    except (TypeError, ValueError):
        usd = None
    return {
        "scheme": "nano",
        "paymentId": payment_id,
        "payTo": nano.get("payTo"),
        # integer raw units (1 XNO = 10^30 raw), as a string
        "amountRaw": amount_raw,
        # human string, e.g. "0.00018406 XNO"
        "amount": nano.get("maxAmountRequiredFormatted") or nano.get("amountFormatted"),
        "amountUsd": usd,
        # ready-to-scan/click nano: URI
        "uri": "nano:%s%s" % (nano.get("payTo"), "?amount=" + amount_raw if amount_raw else ""),
        "expiresAt": _to_ms(nano.get("expiresAt", (pay or {}).get("expiresAt"))),
        "statusUrl": _abs(nano.get("statusUrl") or nano.get("callbackUrl")
                          or (pay or {}).get("statusUrl")
                          or (payment_id and "/api/x402/status/" + payment_id)),
        "completeUrl": _abs(nano.get("completeUrl") or (pay or {}).get("completeUrl")
                            or (payment_id and "/api/x402/complete/" + payment_id)),
        "explorerUrl": (nano.get("extra") or {}).get("explorerUrl"),
        "description": nano.get("description"),
        "requestHash": (pay or {}).get("requestHash") or body.get("requestHash"),
    }


def looks_like_result(j):
    """Does a complete-endpoint body already carry the replayed API result?"""
    return isinstance(j, dict) and any(j.get(k) is not None for k in _RESULT_KEYS)


def parse_json(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None
