"""Tests for the container version (FastAPI wrapper around the same handler). Run:  python -m pytest tests -v"""
import os, sys, time, importlib, json
import boto3, pytest
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from moto import mock_aws

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "api"))
sys.path.insert(0, os.path.join(HERE, "..", "ec2"))
os.environ.update(TABLE="LocalStay", AWS_DEFAULT_REGION="ap-south-1", AWS_ACCESS_KEY_ID="test", AWS_SECRET_ACCESS_KEY="test",
                  DEMO_PAYMENTS="true", USER_POOL_ID="pool1", CLIENT_ID="client1",
                  PHOTO_BUCKET="photos-test", PHOTO_BASE="https://cdn.example.com")
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJwks:
    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": KEY.public_key()})()


def token(sub="u1", aud="client1", iss=None, use="id", exp=3600):
    iss = iss or "https://cognito-idp.ap-south-1.amazonaws.com/pool1"
    return jwt.encode({"sub": sub, "email": f"{sub}@x.com", "name": "Tester", "aud": aud, "iss": iss, "token_use": use,
                       "exp": int(time.time()) + exp}, KEY, algorithm="RS256")


@pytest.fixture
def client():
    with mock_aws():
        boto3.client("dynamodb").create_table(
            TableName="LocalStay", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK")],
            GlobalSecondaryIndexes=[{"IndexName": n, "KeySchema": [{"AttributeName": n + "PK", "KeyType": "HASH"}, {"AttributeName": n + "SK", "KeyType": "RANGE"}],
                                     "Projection": {"ProjectionType": "ALL"}} for n in ("GSI1", "GSI2")])
        boto3.client("s3", region_name="ap-south-1").create_bucket(Bucket="photos-test", CreateBucketConfiguration={"LocationConstraint": "ap-south-1"})
        import handler, app
        importlib.reload(handler)
        importlib.reload(app)
        app._jwks = FakeJwks()
        yield TestClient(app.app)


def auth(sub="u1", **kw):
    return {"Authorization": "Bearer " + token(sub=sub, **kw)}


def test_health(client):
    assert client.get("/health").json() == {"ok": True}


def test_site_is_served_and_points_to_itself(client):
    r = client.get("/")
    assert r.status_code == 200 and 'apiBase:"http://testserver"' in r.text


def test_public_route_works_without_token(client):
    assert client.get("/listings").status_code == 200


def test_protected_route_rejects_missing_token(client):
    assert client.post("/listings", json={"title": "x", "city": "Goa"}).status_code == 401


def test_valid_token_can_create_and_read_listing(client):
    r = client.post("/listings", json={"title": "Container Villa", "city": "Goa", "base": 2000}, headers=auth())
    assert r.status_code == 201
    lid = r.json()["id"]
    got = client.get(f"/listings/{lid}")
    assert got.status_code == 200 and got.json()["listing"]["title"] == "Container Villa"


@pytest.mark.parametrize("kw", [{"aud": "someone-else"}, {"iss": "https://evil.example.com"}, {"use": "access"}, {"exp": -10}])
def test_bad_tokens_are_rejected(client, kw):
    r = client.post("/listings", json={"title": "x", "city": "Goa"}, headers=auth(**kw))
    assert r.status_code == 401


def test_garbage_token_is_rejected(client):
    assert client.post("/listings", json={"title": "x", "city": "Goa"}, headers={"Authorization": "Bearer abc"}).status_code == 401


def test_path_parameters_and_booking_flow(client):
    lid = client.post("/listings", json={"title": "V", "city": "Goa"}, headers=auth("host")).json()["id"]
    import datetime as dt
    ci, co = [(dt.date.today() + dt.timedelta(days=n)).isoformat() for n in (10, 12)]
    r = client.post("/bookings", json={"listing_id": lid, "check_in": ci, "check_out": co}, headers=auth("guest"))
    assert r.status_code == 201
    bid = r.json()["booking_id"]
    assert client.post(f"/bookings/{lid}/{bid}/confirm", headers=auth("guest")).status_code == 200
    assert client.post("/bookings", json={"listing_id": lid, "check_in": ci, "check_out": co}, headers=auth("g2")).status_code == 409


def test_unknown_path_is_404(client):
    assert client.get("/nothing/here").status_code == 404


def test_cors_header_present(client):
    r = client.get("/listings", headers={"Origin": "https://example.com"})
    assert r.headers.get("access-control-allow-origin") == "*"
