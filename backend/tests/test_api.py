"""Tests for the LocalStay API. DynamoDB is faked with moto, so nothing touches real AWS.
Run from the backend folder:  python -m pytest tests -v
"""
import os, sys, json, time, importlib, datetime as dt
from concurrent.futures import ThreadPoolExecutor

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
os.environ.update(TABLE="LocalStay", AWS_DEFAULT_REGION="ap-south-1", AWS_ACCESS_KEY_ID="test",
                  AWS_SECRET_ACCESS_KEY="test", DEMO_PAYMENTS="true")


@pytest.fixture
def h():
    """A fresh handler module bound to a fresh fake DynamoDB table for every test."""
    with mock_aws():
        boto3.client("dynamodb").create_table(
            TableName="LocalStay", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK")],
            GlobalSecondaryIndexes=[{"IndexName": n, "KeySchema": [{"AttributeName": n + "PK", "KeyType": "HASH"}, {"AttributeName": n + "SK", "KeyType": "RANGE"}],
                                     "Projection": {"ProjectionType": "ALL"}} for n in ("GSI1", "GSI2")])
        import handler
        importlib.reload(handler)
        handler.DEMO_PAYMENTS = True
        yield handler


def call(h, route, body=None, sub=None, qs=None, path=None):
    claims = {"sub": sub, "email": f"{sub}@x.com", "name": f"User {sub}"} if sub else {}
    ev = {"routeKey": route, "body": json.dumps(body or {}), "queryStringParameters": qs, "pathParameters": path,
          "requestContext": {"authorizer": {"jwt": {"claims": claims}}}}
    r = h.handler(ev, None)
    return r["statusCode"], json.loads(r["body"])


def day(n):
    return (dt.date.today() + dt.timedelta(days=n)).isoformat()


def make_listing(h, host="host1", city="Goa", base=1000):
    code, body = call(h, "POST /listings", {"title": "Test Villa", "city": city, "base": base, "hospital_km": 4}, sub=host)
    assert code == 201
    return body["id"]


# ---------- pure functions: pricing, dates, verification ----------
META = {"base": 1000, "season": [1] * 12}


@pytest.mark.parametrize("date,expected", [
    ("2026-10-13", 1000),   # normal Tuesday
    ("2026-10-09", 1150),   # Friday: weekend x1.15
    ("2026-10-20", 1250),   # festival x1.25 on a Tuesday
    ("2026-12-25", 1440),   # Friday + festival: 1.15 x 1.25
])
def test_price_rules(h, date, expected):
    assert h.price_for(META, date) == expected


def test_price_is_clamped(h):
    assert h.price_for({"base": 1000, "season": [5] * 12}, "2026-10-13") == 2000   # never above 2x
    assert h.price_for({"base": 1000, "season": [0.1] * 12}, "2026-10-13") == 700  # never below 0.7x


def test_nights_between_valid(h):
    assert h.nights_between(day(1), day(4)) == [day(1), day(2), day(3)]


@pytest.mark.parametrize("ci,co", [("bad", "date"), (None, None), (day(3), day(3)), (day(5), day(2)), (day(-2), day(1)), (day(1), day(40))])
def test_nights_between_rejects_bad_input(h, ci, co):
    with pytest.raises(h.Err) as e:
        h.nights_between(ci, co)
    assert e.value.code == 400


def test_verification_rules(h):
    y = {"verify": {"hospital": "y", "food": "n", "network": "u"}}
    n = {"verify": {"hospital": "n", "food": "n", "network": "u"}}
    assert h.verification([y, y, y])["hospital"] == "verified"
    assert h.verification([y, y])["hospital"] == "host-reported"      # only 2 confirmations
    assert h.verification([n, n, y])["food"] == "disputed"            # 2 disputes
    assert h.verification([])["network"] == "host-reported"


# ---------- auth guard ----------
def test_protected_routes_need_login(h):
    for route in ("POST /bookings", "POST /listings", "POST /local/apply", "GET /bookings/me", "POST /reviews"):
        assert call(h, route, {})[0] == 401, route


def test_public_routes_need_no_login(h):
    assert call(h, "GET /listings")[0] == 200
    assert call(h, "GET /local/reviews", qs={"city": "goa"})[0] == 200


def test_unknown_route_does_not_crash(h):
    assert call(h, "GET /nothing", sub="u1")[0] == 400


# ---------- listings ----------
def test_create_and_read_listing(h):
    lid = make_listing(h)
    code, body = call(h, "GET /listings/{id}", path={"id": lid})
    assert code == 200 and body["listing"]["title"] == "Test Villa"
    assert "PK" not in body["listing"]                                  # key attributes are not leaked
    assert len(body["next_7_prices"]) == 7


def test_list_filters_by_city(h):
    make_listing(h, city="Goa")
    make_listing(h, city="Manali")
    code, body = call(h, "GET /listings", qs={"city": "goa"})
    assert code == 200 and [x["city"] for x in body["listings"]] == ["Goa"]
    assert len(call(h, "GET /listings")[1]["listings"]) == 2


def test_listing_not_found(h):
    assert call(h, "GET /listings/{id}", path={"id": "nope"})[0] == 404


def test_listing_values_are_clamped(h):
    code, body = call(h, "POST /listings", {"title": "x", "city": "Goa", "base": 99999999, "hospital_km": -5}, sub="h1")
    meta = call(h, "GET /listings/{id}", path={"id": body["id"]})[1]["listing"]
    assert meta["base"] == 100000 and meta["hospital_km"] == 0


# ---------- bookings and double-booking protection ----------
def book(h, lid, ci, co, sub="g1"):
    return call(h, "POST /bookings", {"listing_id": lid, "check_in": ci, "check_out": co}, sub=sub)


def test_booking_creates_hold_with_server_price(h):
    lid = make_listing(h, base=1000)
    code, body = book(h, lid, day(10), day(12))
    assert code == 201 and body["status"] == "HOLD"
    meta = h.get_meta(lid)
    assert body["total"] == h.price_for(meta, day(10)) + h.price_for(meta, day(11))


def test_overlapping_booking_is_rejected(h):
    lid = make_listing(h)
    assert book(h, lid, day(10), day(13))[0] == 201
    assert book(h, lid, day(12), day(15), sub="g2")[0] == 409           # night 12 is taken
    assert book(h, lid, day(13), day(15), sub="g2")[0] == 201           # check-out day is free


def test_failed_booking_writes_nothing(h):
    lid = make_listing(h)
    book(h, lid, day(10), day(11))
    book(h, lid, day(9), day(12), sub="g2")                             # fails on night 10
    booked = call(h, "GET /listings/{id}", path={"id": lid})[1]["booked_nights"]
    assert booked == [day(10)]                                          # nights 9 and 11 were not locked


def test_expired_hold_can_be_rebooked(h):
    lid = make_listing(h)
    book(h, lid, day(10), day(11))
    h.T.update_item(Key={"PK": f"LISTING#{lid}", "SK": f"NIGHT#{day(10)}"}, UpdateExpression="SET hold_until = :p",
                    ExpressionAttributeValues={":p": int(time.time()) - 5})
    assert book(h, lid, day(10), day(11), sub="g2")[0] == 201


def test_concurrent_bookings_only_one_wins(h):
    lid = make_listing(h)
    with ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(lambda i: book(h, lid, day(20), day(21), sub=f"g{i}")[0], range(8)))
    assert codes.count(201) == 1 and codes.count(409) == 7


def test_confirm_booking(h):
    lid = make_listing(h)
    bid = book(h, lid, day(10), day(11))[1]["booking_id"]
    assert call(h, "POST /bookings/{lid}/{bid}/confirm", sub="g1", path={"lid": lid, "bid": bid})[0] == 200
    mine = call(h, "GET /bookings/me", sub="g1")[1]["bookings"]
    assert mine[0]["status"] == "CONFIRMED"
    assert call(h, "POST /bookings/{lid}/{bid}/confirm", sub="g1", path={"lid": lid, "bid": bid})[0] == 200  # safe to repeat


def test_cannot_confirm_someone_elses_booking(h):
    lid = make_listing(h)
    bid = book(h, lid, day(10), day(11))[1]["booking_id"]
    assert call(h, "POST /bookings/{lid}/{bid}/confirm", sub="intruder", path={"lid": lid, "bid": bid})[0] == 404


def test_confirm_is_closed_when_demo_payments_off(h):
    lid = make_listing(h)
    bid = book(h, lid, day(10), day(11))[1]["booking_id"]
    h.DEMO_PAYMENTS = False
    assert call(h, "POST /bookings/{lid}/{bid}/confirm", sub="g1", path={"lid": lid, "bid": bid})[0] == 403


def test_my_bookings_only_shows_own(h):
    lid = make_listing(h)
    book(h, lid, day(10), day(11), sub="g1")
    assert len(call(h, "GET /bookings/me", sub="g1")[1]["bookings"]) == 1
    assert call(h, "GET /bookings/me", sub="g2")[1]["bookings"] == []


# ---------- reviews ----------
def test_review_needs_confirmed_past_booking(h):
    lid = make_listing(h)
    bid = book(h, lid, day(10), day(11))[1]["booking_id"]
    review = {"listing_id": lid, "booking_id": bid, "rating": 5, "text": "Lovely stay, very quiet."}
    assert call(h, "POST /reviews", review, sub="g1")[0] == 403         # not confirmed yet
    call(h, "POST /bookings/{lid}/{bid}/confirm", sub="g1", path={"lid": lid, "bid": bid})
    assert call(h, "POST /reviews", review, sub="g1")[0] == 403         # check-out is in the future


def test_review_after_checkout_and_no_duplicates(h):
    lid, bid = make_listing(h), "PAST1234"
    h.T.put_item(Item={"PK": f"LISTING#{lid}", "SK": f"BOOKING#{bid}", "guest": "g1", "status": "CONFIRMED",
                       "check_in": day(-5), "check_out": day(-3)})
    review = {"listing_id": lid, "booking_id": bid, "rating": 4, "text": "Good place, helpful host.",
              "verify": {"hospital": "y", "food": "bogus", "network": "n"}}
    assert call(h, "POST /reviews", review, sub="g1")[0] == 201
    assert call(h, "POST /reviews", review, sub="g1")[0] == 409
    assert call(h, "POST /reviews", review, sub="g2")[0] == 403         # not their booking
    body = call(h, "GET /listings/{id}", path={"id": lid})[1]
    rev = body["reviews"][0]
    assert "guest" not in rev                                           # reviewer id hidden
    assert rev["verify"] == {"hospital": "y", "food": "u", "network": "n"}  # bad value becomes unknown


# ---------- verified locals ----------
def approve(h, sub):
    h.T.update_item(Key={"PK": f"USER#{sub}", "SK": "LOCALAPP"}, UpdateExpression="SET #s = :v",
                    ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":v": "VERIFIED"})


def test_local_flow(h):
    tip = {"safety": 5, "locals": 4, "tips": "Avoid the beach road after 9pm, it is dark."}
    assert call(h, "GET /local/me", sub="l1")[1]["status"] == "NONE"
    assert call(h, "POST /local/reviews", tip, sub="l1")[0] == 403      # not verified
    assert call(h, "POST /local/apply", {"city": "Goa", "years": 12}, sub="l1")[1]["status"] == "PENDING"
    assert call(h, "POST /local/reviews", tip, sub="l1")[0] == 403      # pending is not enough
    approve(h, "l1")
    assert call(h, "POST /local/reviews", dict(tip, city="Manali"), sub="l1")[0] == 201
    reviews = call(h, "GET /local/reviews", qs={"city": "GOA"})[1]["reviews"]   # goes to the verified city, any case
    assert len(reviews) == 1 and reviews[0]["years"] == 12
    assert call(h, "GET /local/reviews", qs={"city": "manali"})[1]["reviews"] == []
    assert call(h, "POST /local/apply", {"city": "Goa", "years": 12}, sub="l1")[0] == 409


@pytest.mark.parametrize("body", [{"city": "", "years": 5}, {"city": "Goa", "years": 0}, {"city": "Goa", "years": "abc"}, {"city": "Goa", "years": 99}])
def test_local_apply_validation(h, body):
    assert call(h, "POST /local/apply", body, sub="l1")[0] == 400


def test_local_reviews_needs_city(h):
    assert call(h, "GET /local/reviews")[0] == 400
