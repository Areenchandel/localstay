"""Razorpay flow with a fake Razorpay: order creation, browser verification, signed webhook, refunds."""
import json, hmac, hashlib, base64
import pytest
from test_api import h, call, day, make_listing, book   # same fake DynamoDB + helpers as the other tests

KEY, SECRET, HOOK = "rzp_test_abc", "key_secret_1", "hook_secret_1"


class FakeRzp:
    def __init__(self):
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        return {"id": f"order_{len(self.calls)}"} if path == "/orders" else {}


@pytest.fixture
def rz(h, monkeypatch):
    h._RZP["conf"] = {"key_id": KEY, "key_secret": SECRET, "webhook_secret": HOOK}
    fake = FakeRzp()
    monkeypatch.setattr(h, "rzp", fake)
    return fake


def sign(secret, msg):
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def held(h, guest="g1", nights=2):
    lid = make_listing(h, base=1000)
    code, body = book(h, lid, day(10), day(10 + nights), sub=guest)
    assert code == 201
    return lid, body["booking_id"], body["total"]


def start(h, lid, bid, guest="g1"):
    code, body = call(h, "POST /bookings/{lid}/{bid}/pay-order", sub=guest, path={"lid": lid, "bid": bid})
    assert code == 200, body
    return body


def status(h, lid, bid):
    return h.get_booking(lid, bid)["status"]


def webhook(h, lid, bid, amount, pid="pay_1", event="payment.captured", secret=HOOK, order=None, b64=False):
    order = order or h.get_booking(lid, bid)["order_id"]
    raw = json.dumps({"event": event, "payload": {"payment": {"entity": {"id": pid, "order_id": order, "amount": amount, "status": "captured"}}}})
    body = base64.b64encode(raw.encode()).decode() if b64 else raw
    ev = {"routeKey": "POST /razorpay/webhook", "body": body, "isBase64Encoded": b64, "headers": {"X-Razorpay-Signature": sign(secret, raw)},
          "requestContext": {}}
    r = h.handler(ev, None)
    return r["statusCode"], json.loads(r["body"])


# ---------- config ----------
def test_config_is_demo_until_keys_exist(h):
    assert call(h, "GET /config")[1] == {"payments": "demo", "key_id": None}


def test_config_shows_only_the_public_key(h, rz):
    assert call(h, "GET /config")[1] == {"payments": "razorpay", "key_id": KEY}


# ---------- creating the order ----------
def test_order_amount_is_computed_on_the_server(h, rz):
    lid, bid, total = held(h)
    o = start(h, lid, bid)
    assert o["amount"] == (total + round(total * 0.10)) * 100 and o["currency"] == "INR" and o["key_id"] == KEY
    method, path, body = rz.calls[0]
    assert (method, path) == ("POST", "/orders") and body["receipt"] == bid and body["notes"] == {"listing_id": lid, "booking_id": bid}


def test_order_is_created_once(h, rz):
    lid, bid, _ = held(h)
    assert start(h, lid, bid) == start(h, lid, bid)
    assert [c[1] for c in rz.calls].count("/orders") == 1


def test_order_rules(h, rz):
    lid, bid, _ = held(h)
    assert call(h, "POST /bookings/{lid}/{bid}/pay-order", sub="intruder", path={"lid": lid, "bid": bid})[0] == 404
    assert call(h, "POST /bookings/{lid}/{bid}/pay-order", path={"lid": lid, "bid": bid})[0] == 401
    h.T.update_item(Key={"PK": f"LISTING#{lid}", "SK": f"BOOKING#{bid}"}, UpdateExpression="SET hold_until = :p", ExpressionAttributeValues={":p": 1})
    assert call(h, "POST /bookings/{lid}/{bid}/pay-order", sub="g1", path={"lid": lid, "bid": bid})[0] == 409   # hold expired


def test_order_not_available_without_keys(h):
    lid, bid, _ = held(h)
    assert call(h, "POST /bookings/{lid}/{bid}/pay-order", sub="g1", path={"lid": lid, "bid": bid})[0] == 501


def test_demo_confirm_is_closed_once_razorpay_is_on(h, rz):
    lid, bid, _ = held(h)
    assert call(h, "POST /bookings/{lid}/{bid}/confirm", sub="g1", path={"lid": lid, "bid": bid})[0] == 403


# ---------- browser verification ----------
def verify(h, lid, bid, pid, sig, guest="g1"):
    return call(h, "POST /bookings/{lid}/{bid}/verify", {"razorpay_payment_id": pid, "razorpay_signature": sig, "razorpay_order_id": "order_fake"},
                sub=guest, path={"lid": lid, "bid": bid})


def test_verify_with_good_signature_confirms(h, rz):
    lid, bid, _ = held(h)
    oid = start(h, lid, bid)["order_id"]
    assert verify(h, lid, bid, "pay_9", sign(SECRET, f"{oid}|pay_9"))[0] == 200
    b = h.get_booking(lid, bid)
    assert b["status"] == "CONFIRMED" and b["payment_id"] == "pay_9" and "hold_until" not in b


def test_verify_rejects_bad_signature_and_wrong_user(h, rz):
    lid, bid, _ = held(h)
    oid = start(h, lid, bid)["order_id"]
    assert verify(h, lid, bid, "pay_9", "00" * 32)[0] == 400
    assert verify(h, lid, bid, "pay_9", sign(SECRET, f"{oid}|pay_8"))[0] == 400     # signature for another payment id
    assert verify(h, lid, bid, "pay_9", sign(SECRET, f"{oid}|pay_9"), guest="intruder")[0] == 404
    assert status(h, lid, bid) == "HOLD"


def test_verify_needs_an_order_first(h, rz):
    lid, bid, _ = held(h)
    assert verify(h, lid, bid, "pay_1", sign(SECRET, "None|pay_1"))[0] == 400


# ---------- webhook ----------
def test_webhook_confirms_even_if_browser_never_returns(h, rz):
    lid, bid, _ = held(h)
    amount = start(h, lid, bid)["amount"]
    assert webhook(h, lid, bid, amount)[0] == 200
    assert status(h, lid, bid) == "CONFIRMED"
    assert webhook(h, lid, bid, amount)[0] == 200 and status(h, lid, bid) == "CONFIRMED"     # Razorpay retries are harmless


def test_webhook_works_with_base64_body_and_order_paid_event(h, rz):
    lid, bid, _ = held(h)
    amount = start(h, lid, bid)["amount"]
    assert webhook(h, lid, bid, amount, event="order.paid", b64=True)[0] == 200
    assert status(h, lid, bid) == "CONFIRMED"


def test_webhook_rejects_wrong_signature(h, rz):
    lid, bid, _ = held(h)
    amount = start(h, lid, bid)["amount"]
    assert webhook(h, lid, bid, amount, secret="not-the-secret")[0] == 400
    assert status(h, lid, bid) == "HOLD"


def test_webhook_ignores_wrong_amount_unknown_order_and_other_events(h, rz):
    lid, bid, _ = held(h)
    amount = start(h, lid, bid)["amount"]
    assert webhook(h, lid, bid, amount - 100)[1] == {"ignored": True}
    assert webhook(h, lid, bid, amount, order="order_unknown")[1] == {"ignored": True}
    assert webhook(h, lid, bid, amount, event="payment.failed")[1] == {"ignored": True}
    assert status(h, lid, bid) == "HOLD"


def test_webhook_is_public_but_still_needs_valid_signature(h):
    r = h.handler({"routeKey": "POST /razorpay/webhook", "body": "{}", "headers": {}, "requestContext": {}}, None)
    assert r["statusCode"] == 501      # not 401: the route is public, and without keys it is simply off


# ---------- money arrived but dates are gone ----------
def lose_the_nights(h, lid, bid):
    for n in h.get_booking(lid, bid)["nights"]:
        h.T.delete_item(Key={"PK": f"LISTING#{lid}", "SK": f"NIGHT#{n}"})


def test_webhook_refunds_when_dates_are_gone(h, rz):
    lid, bid, _ = held(h)
    amount = start(h, lid, bid)["amount"]
    lose_the_nights(h, lid, bid)
    assert webhook(h, lid, bid, amount, pid="pay_7")[1]["refunded"] is True
    assert ("POST", "/payments/pay_7/refund", {}) in rz.calls
    assert status(h, lid, bid) == "REFUNDED"
    n = len(rz.calls)
    assert webhook(h, lid, bid, amount, pid="pay_7")[0] == 200
    assert len(rz.calls) == n                       # a retried webhook does not refund twice


def test_verify_refunds_when_dates_are_gone(h, rz):
    lid, bid, _ = held(h)
    oid = start(h, lid, bid)["order_id"]
    lose_the_nights(h, lid, bid)
    assert verify(h, lid, bid, "pay_5", sign(SECRET, f"{oid}|pay_5"))[0] == 409
    assert ("POST", "/payments/pay_5/refund", {}) in rz.calls and status(h, lid, bid) == "REFUNDED"


def test_failed_refund_is_recorded_for_manual_action(h, rz, monkeypatch):
    lid, bid, _ = held(h)
    amount = start(h, lid, bid)["amount"]
    lose_the_nights(h, lid, bid)

    def broken(method, path, body=None):
        raise h.Err(502, "down")
    monkeypatch.setattr(h, "rzp", broken)
    webhook(h, lid, bid, amount)
    assert status(h, lid, bid) == "REFUND_FAILED"
