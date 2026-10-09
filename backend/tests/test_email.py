"""Booking confirmation emails (SES is faked with moto)."""
import boto3
from test_api import h, call, day, make_listing, book  # noqa: F401  (h is a fixture)


def ses_on(h, monkeypatch):
    client = boto3.client("ses", region_name="ap-south-1")
    client.verify_email_identity(EmailAddress="from@x.com")
    monkeypatch.setattr(h, "SENDER", "from@x.com")
    monkeypatch.setattr(h, "SES", client)


def sent():
    from moto.ses import ses_backends
    return ses_backends["123456789012"]["ap-south-1"].sent_messages


def confirmed_booking(h, guest="g1"):
    lid = make_listing(h)
    _, b = book(h, lid, day(10), day(12), sub=guest)
    return lid, b["booking_id"]


def test_mail_goes_to_guest_once_when_confirmed(h, monkeypatch):
    ses_on(h, monkeypatch)
    lid, bid = confirmed_booking(h)
    assert call(h, "POST /bookings/{lid}/{bid}/confirm", sub="g1", path={"lid": lid, "bid": bid})[0] == 200
    assert len(sent()) == 1
    m = sent()[0]
    assert m.destinations["ToAddresses"] == ["g1@x.com"] and bid in m.body
    call(h, "POST /bookings/{lid}/{bid}/confirm", sub="g1", path={"lid": lid, "bid": bid})  # replay
    assert len(sent()) == 1


def test_no_mail_while_booking_is_only_held(h, monkeypatch):
    ses_on(h, monkeypatch)
    confirmed_booking(h)
    assert len(sent()) == 0


def test_mail_failure_does_not_break_the_booking(h, monkeypatch):
    class Boom:
        def send_email(self, **k):
            raise RuntimeError("ses down")
    monkeypatch.setattr(h, "SENDER", "from@x.com")
    monkeypatch.setattr(h, "SES", Boom())
    lid, bid = confirmed_booking(h)
    code, _ = call(h, "POST /bookings/{lid}/{bid}/confirm", sub="g1", path={"lid": lid, "bid": bid})
    assert code == 200 and h.get_booking(lid, bid)["status"] == "CONFIRMED"


def test_email_is_skipped_when_not_configured(h):
    assert h.SES is None
    lid, bid = confirmed_booking(h)
    assert call(h, "POST /bookings/{lid}/{bid}/confirm", sub="g1", path={"lid": lid, "bid": bid})[0] == 200
