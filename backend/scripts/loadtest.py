"""Load test against the LIVE API: many guests fight for the same dates; exactly one may win.

Needs your AWS CLI login (same one deploy.ps1 uses). Creates temporary Cognito users and one
temporary listing, and deletes all of them at the end.

  python backend/scripts/loadtest.py            (8 guests, 200 reads)
  python backend/scripts/loadtest.py 8 200 2    (guests, reads, parallel readers)
"""
import json, sys, time, uuid, threading, datetime as dt, urllib.request, urllib.error
from collections import Counter
from concurrent.futures import ThreadPoolExecutor


def percentile(values, p):
    if not values:
        return 0.0
    v = sorted(values)
    return v[min(len(v) - 1, int(round((p / 100) * (len(v) - 1))))]


def scenario(post, get, host_post, guests, reads, read_workers=8):
    """post(token_index, path, body) -> (status, json); get(path) -> (status, json). Returns a result dict."""
    code, listing = host_post("/listings", {"title": "LOADTEST DELETE ME", "city": "Goa", "base": 1000, "hospital_km": 4})
    assert code == 201, listing
    lid = listing["id"]
    ci = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    co = (dt.date.today() + dt.timedelta(days=32)).isoformat()

    # 1) double-booking race: every guest books the same 2 nights at the same moment
    gate, out, lat = threading.Barrier(guests), [], []

    def fight(i):
        gate.wait()
        t = time.perf_counter()
        r = post(i, "/bookings", {"listing_id": lid, "check_in": ci, "check_out": co})
        lat.append((time.perf_counter() - t) * 1000)
        out.append(r[0])

    with ThreadPoolExecutor(guests) as ex:
        list(ex.map(fight, range(guests)))
    won, taken = out.count(201), out.count(409)

    # 2) read load on the public listings endpoint
    rl, bad, codes = [], 0, Counter()

    def read(_):
        nonlocal bad
        t = time.perf_counter()
        c, _b = get("/listings")
        rl.append((time.perf_counter() - t) * 1000)
        codes[c] += 1
        bad += c != 200

    started = time.perf_counter()
    with ThreadPoolExecutor(read_workers) as ex:
        list(ex.map(read, range(reads)))
    secs = time.perf_counter() - started
    return {"listing_id": lid, "guests": guests, "won": won, "rejected_409": taken, "booking_status_codes": dict(Counter(out)), "read_status_codes": dict(codes), "other": guests - won - taken,
            "book_p50_ms": percentile(lat, 50), "book_p95_ms": percentile(lat, 95),
            "reads": reads, "read_errors": bad, "read_p50_ms": percentile(rl, 50), "read_p95_ms": percentile(rl, 95),
            "reads_per_sec": reads / secs if secs else 0}


def main():
    import boto3
    import boto3.dynamodb.conditions
    guests = int(sys.argv[1]) if len(sys.argv) > 1 else 8   # this AWS account allows only 10 Lambdas at once
    reads = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 4   # parallel readers
    region, api = "ap-south-1", "https://s41y3in6tl.execute-api.ap-south-1.amazonaws.com"
    cg = boto3.client("cognito-idp", region_name=region)
    pool_id, client_id = "ap-south-1_k27cYRPr4", "6nb6pe1fp1e06rln3f94topabd"
    run, pw = uuid.uuid4().hex[:6], "Lt!9" + uuid.uuid4().hex[:12]
    names = [f"loadtest+{run}{i}@example.com" for i in range(guests + 1)]  # last one is the host
    print(f"Creating {len(names)} temporary users...")
    tokens = []
    for n in names:
        cg.admin_create_user(UserPoolId=pool_id, Username=n, MessageAction="SUPPRESS",
                             UserAttributes=[{"Name": "email", "Value": n}, {"Name": "email_verified", "Value": "true"}, {"Name": "name", "Value": "Load Test"}])
        cg.admin_set_user_password(UserPoolId=pool_id, Username=n, Password=pw, Permanent=True)
        tokens.append(cg.initiate_auth(AuthFlow="USER_PASSWORD_AUTH", ClientId=client_id,
                                       AuthParameters={"USERNAME": n, "PASSWORD": pw})["AuthenticationResult"]["IdToken"])

    def http(method, path, body=None, token=None):
        req = urllib.request.Request(api + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **({"Authorization": token} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    lid = None
    try:
        res = scenario(lambda i, p, b: http("POST", p, b, tokens[i]), lambda p: http("GET", p),
                       lambda p, b: http("POST", p, b, tokens[-1]), guests, reads, workers)
        lid = res["listing_id"]
        print(json.dumps(res, indent=2))
        ok = res["won"] == 1 and res["other"] == 0 and res["read_errors"] == 0
        print("\nRESULT:", "PASS" if ok else "FAIL",
              f"- {res['guests']} guests, 1 booking won, {res['rejected_409']} correctly rejected, zero double-booking" if ok else "- investigate")
    finally:
        print("Cleaning up...")
        for n in names:
            try:
                cg.admin_delete_user(UserPoolId=pool_id, Username=n)
            except Exception as e:  # noqa: BLE001
                print("could not delete", n, e)
        if lid:
            t = boto3.resource("dynamodb", region_name=region).Table("LocalStay")
            items = t.query(KeyConditionExpression=boto3.dynamodb.conditions.Key("PK").eq(f"LISTING#{lid}"))["Items"]
            for it in items:
                t.delete_item(Key={"PK": it["PK"], "SK": it["SK"]})
            print(f"Deleted test listing and {len(items)} items.")


if __name__ == "__main__":
    main()
