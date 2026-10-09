"""LocalStay API: one Lambda behind API Gateway HTTP API, one DynamoDB table (single-table design)."""
import json, os, re, time, uuid, hmac, hashlib, base64, urllib.request, urllib.error, datetime as dt
from decimal import Decimal
import boto3
from boto3.dynamodb.conditions import Key, Attr
from boto3.dynamodb.types import TypeSerializer
from botocore.config import Config
from botocore.exceptions import ClientError

TABLE = os.environ["TABLE"]
DEMO_PAYMENTS = os.environ.get("DEMO_PAYMENTS", "false") == "true"
T = boto3.resource("dynamodb").Table(TABLE)
DB = boto3.client("dynamodb")
S = TypeSerializer()
FESTIVALS = {"2026-10-20", "2026-11-08", "2026-11-09", "2026-12-25", "2026-12-31", "2027-01-26", "2027-03-04"}
HOLD_SECONDS = 600
PHOTO_BUCKET = os.environ.get("PHOTO_BUCKET", "")
PHOTO_BASE = os.environ.get("PHOTO_BASE", "").rstrip("/")  # CloudFront URL that serves the photos
SENDER = os.environ.get("SENDER_EMAIL", "")
SES = boto3.client("ses", region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "ap-south-1") if SENDER else None
S3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "ap-south-1",
                  config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
PHOTO_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
MAX_PHOTOS = 6
MAX_PHOTO_BYTES = 5_000_000
RZP_PREFIX = os.environ.get("RAZORPAY_PARAM_PREFIX", "")  # SSM path holding key_id, key_secret, webhook_secret. Empty = Razorpay off (demo payments)
_RZP = {"conf": {}, "at": 0}


class Err(Exception):
    def __init__(self, code, msg):
        self.code, self.msg = code, msg


def ser(d):
    return {k: S.serialize(v) for k, v in d.items()}


def clean(x):
    """Remove key attributes and convert Decimal to plain numbers for JSON."""
    if isinstance(x, list):
        return [clean(i) for i in x]
    if isinstance(x, dict):
        return {k: clean(v) for k, v in x.items() if k not in ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK", "ttl")}
    if isinstance(x, Decimal):
        return int(x) if x == x.to_integral_value() else float(x)
    return x


def resp(code, body):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"}, "body": json.dumps(clean(body))}


# ---------- pricing (runs on the server so the client cannot change prices) ----------
def price_for(meta, date_str):
    d = dt.date.fromisoformat(date_str)
    season = float(meta.get("season", [1] * 12)[d.month - 1])
    weekend = 1.15 if d.weekday() in (4, 5) else 1.0
    fest = 1.25 if date_str in FESTIVALS else 1.0
    mult = max(0.7, min(2.0, season * weekend * fest))
    return int(round(float(meta["base"]) * mult / 10) * 10)


def nights_between(ci, co):
    try:
        a, b = dt.date.fromisoformat(ci), dt.date.fromisoformat(co)
    except (ValueError, TypeError):
        raise Err(400, "Dates must look like 2026-12-25.")
    n = (b - a).days
    if n < 1 or n > 30:
        raise Err(400, "Stay must be 1 to 30 nights.")
    if a < dt.date.today():
        raise Err(400, "Check-in cannot be in the past.")
    return [(a + dt.timedelta(d)).isoformat() for d in range(n)]


def get_meta(lid):
    m = T.get_item(Key={"PK": f"LISTING#{lid}", "SK": "META"}).get("Item")
    if not m:
        raise Err(404, "Listing not found.")
    return m


# ---------- listings ----------
def photo_urls(item):
    return [f"{PHOTO_BASE}/{k}" for k in item.get("photos", [])]


def list_listings(qs):
    city = qs.get("city")
    if city:
        r = T.query(IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq(f"CITY#{city.lower()}"))
    else:
        r = T.scan(FilterExpression=Attr("SK").eq("META"))  # fine for a small table; query GSI1 per city when it grows
    items = r["Items"]
    for i in items:
        i["tonight_price"] = price_for(i, dt.date.today().isoformat())
        i["photos"] = photo_urls(i)
    return resp(200, {"listings": items})


def verification(reviews):
    out = {}
    for field in ("hospital", "food", "network"):
        yes = sum(1 for r in reviews if r["verify"].get(field) == "y")
        no = sum(1 for r in reviews if r["verify"].get(field) == "n")
        out[field] = "disputed" if no >= 2 else "verified" if yes >= 3 else "host-reported"
    return out


def get_listing(lid):
    meta = get_meta(lid)
    meta["photos"] = photo_urls(meta)
    revs = T.query(KeyConditionExpression=Key("PK").eq(f"LISTING#{lid}") & Key("SK").begins_with("REVIEW#"))["Items"]
    revs = [{k: v for k, v in r.items() if k != "guest"} for r in revs]  # do not expose reviewer ids
    nights = T.query(KeyConditionExpression=Key("PK").eq(f"LISTING#{lid}") & Key("SK").begins_with("NIGHT#"),
                     ProjectionExpression="SK, hold_until")["Items"]
    now = int(time.time())
    booked = [n["SK"].split("#")[1] for n in nights if "hold_until" not in n or int(n["hold_until"]) > now]
    today = dt.date.today()
    days = [(today + dt.timedelta(i)).isoformat() for i in range(7)]
    return resp(200, {"listing": meta, "reviews": revs, "verification": verification(revs),
                      "booked_nights": booked, "next_7_prices": {d: price_for(meta, d) for d in days}})


def create_listing(sub, name, b):
    title, city = str(b.get("title", "")).strip(), str(b.get("city", "")).strip()
    if not title or not city:
        raise Err(400, "Title and city are required.")
    peer = T.query(IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq(f"CITY#{city.lower()}"), Limit=1)["Items"]
    lid = uuid.uuid4().hex[:8]
    num = lambda k, lo, hi, d: max(Decimal(lo), min(Decimal(hi), Decimal(str(b.get(k, d)))))
    item = {"PK": f"LISTING#{lid}", "SK": "META", "id": lid, "title": title[:80], "city": city, "type": b.get("type", "nature"),
            "host": name, "host_id": sub, "host_bio": str(b.get("bio", ""))[:300], "host_languages": str(b.get("languages", ""))[:80],
            "host_since": str(b.get("since", ""))[:4], "base": num("base", 300, 100000, 2000),
            "hospital_km": num("hospital_km", 0, 500, 10), "food_delivery": bool(b.get("food_delivery", True)),
            "networks": [str(x)[:12] for x in b.get("networks", [])][:6], "atm_km": num("atm_km", 0, 500, 2),
            "altitude_m": num("altitude_m", 0, 6000, 100), "road_note": str(b.get("road_note", ""))[:120],
            "season": peer[0]["season"] if peer else [1] * 12,
            "GSI1PK": f"CITY#{city.lower()}", "GSI1SK": f"LISTING#{lid}", "GSI2PK": f"USER#{sub}", "GSI2SK": f"LISTING#{lid}"}
    T.put_item(Item=item)
    return resp(201, {"id": lid})


# ---------- photos: the browser uploads straight to S3 with a short-lived signed form, Lambda never touches the file ----------
def photo_upload_form(sub, lid, b):
    meta = get_meta(lid)
    if meta.get("host_id") != sub:
        raise Err(403, "Only the host can add photos.")
    if not PHOTO_BUCKET:
        raise Err(503, "Photo upload is not configured.")
    ct = b.get("content_type")
    ext = PHOTO_TYPES.get(ct)
    if not ext:
        raise Err(400, "Photos must be JPEG, PNG or WebP.")
    if len(meta.get("photos", [])) >= MAX_PHOTOS:
        raise Err(400, f"A listing can have up to {MAX_PHOTOS} photos.")
    key = f"photos/{lid}/{uuid.uuid4().hex[:12]}.{ext}"
    post = S3.generate_presigned_post(PHOTO_BUCKET, key, Fields={"Content-Type": ct},
                                      Conditions=[{"Content-Type": ct}, ["content-length-range", 1, MAX_PHOTO_BYTES]], ExpiresIn=300)
    return resp(200, {"upload": post, "key": key})


def add_photo(sub, lid, b):
    key = str(b.get("key", ""))
    if not re.match(rf"^photos/{re.escape(lid)}/[0-9a-f]{{12}}\.(jpg|png|webp)$", key):
        raise Err(400, "Invalid photo key.")
    if get_meta(lid).get("host_id") != sub:
        raise Err(403, "Only the host can add photos.")
    try:
        S3.head_object(Bucket=PHOTO_BUCKET, Key=key)  # make sure the file really arrived
    except ClientError:
        raise Err(400, "Upload not found. Please try again.")
    try:
        T.update_item(Key={"PK": f"LISTING#{lid}", "SK": "META"},
                      UpdateExpression="SET photos = list_append(if_not_exists(photos, :e), :k)",
                      ConditionExpression="host_id = :h AND (attribute_not_exists(photos) OR size(photos) < :m)",
                      ExpressionAttributeValues={":e": [], ":k": [key], ":h": sub, ":m": MAX_PHOTOS})
    except T.meta.client.exceptions.ConditionalCheckFailedException:
        raise Err(400, f"A listing can have up to {MAX_PHOTOS} photos.")
    return resp(201, {"url": f"{PHOTO_BASE}/{key}"})


# ---------- bookings ----------
def create_booking(sub, name, b, email=""):
    lid = b.get("listing_id")
    meta = get_meta(lid)
    nights = nights_between(b.get("check_in"), b.get("check_out"))
    now, bid = int(time.time()), uuid.uuid4().hex[:8].upper()
    hold = now + HOLD_SECONDS
    ops = [{"Put": {"TableName": TABLE,
                    "Item": ser({"PK": f"LISTING#{lid}", "SK": f"NIGHT#{n}", "booking_id": bid, "hold_until": hold, "ttl": hold + 86400}),
                    # one lock item per night: succeeds only if the night is free or an old hold has expired
                    "ConditionExpression": "attribute_not_exists(PK) OR hold_until < :now",
                    "ExpressionAttributeValues": ser({":now": now})}} for n in nights]
    total = sum(price_for(meta, n) for n in nights)
    ops.append({"Put": {"TableName": TABLE, "Item": ser({
        "PK": f"LISTING#{lid}", "SK": f"BOOKING#{bid}", "GSI2PK": f"USER#{sub}", "GSI2SK": f"BOOKING#{bid}",
        "id": bid, "listing_id": lid, "title": meta["title"], "guest": sub, "guest_name": name, "guest_email": email, "check_in": b["check_in"],
        "check_out": b["check_out"], "nights": nights, "total": total, "status": "HOLD", "hold_until": hold})}})
    try:
        DB.transact_write_items(TransactItems=ops)  # all nights + booking, or nothing
    except DB.exceptions.TransactionCanceledException:
        raise Err(409, "Some of those nights are already booked or held. Pick other dates.")
    return resp(201, {"booking_id": bid, "status": "HOLD", "total": total, "hold_expires_at": hold})


# ---------- payments: Razorpay (test or live keys), confirmed by a signed webhook ----------
def rzp_conf():
    """Razorpay keys come from SSM Parameter Store (encrypted), never from code or Terraform state. Cached per container."""
    if _RZP["conf"]:
        return _RZP["conf"]
    if not RZP_PREFIX or time.time() - _RZP["at"] < 60:
        return {}
    _RZP["at"] = time.time()
    try:
        r = boto3.client("ssm").get_parameters(Names=[f"{RZP_PREFIX}/{n}" for n in ("key_id", "key_secret", "webhook_secret")], WithDecryption=True)
        conf = {p["Name"].rsplit("/", 1)[1]: p["Value"] for p in r["Parameters"]}
    except Exception as e:
        print("razorpay config error", repr(e))
        return {}
    _RZP["conf"] = conf if len(conf) == 3 else {}
    return _RZP["conf"]


def rzp(method, path, body=None):
    c = rzp_conf()
    auth = base64.b64encode(f"{c['key_id']}:{c['key_secret']}".encode()).decode()
    req = urllib.request.Request("https://api.razorpay.com/v1" + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "Authorization": "Basic " + auth})
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print("razorpay error", e.code, e.read()[:300])
        raise Err(502, "Payment provider error. Please try again.")
    except Exception as e:
        print("razorpay unreachable", repr(e))
        raise Err(502, "Payment provider is unreachable. Please try again.")


def fee_for(total):
    return int(total * 0.10 + 0.5)  # 10% service fee, rounded half up (the page shows the same number)


def hmac_hex(secret, msg):
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def get_booking(lid, bid):
    return T.get_item(Key={"PK": f"LISTING#{lid}", "SK": f"BOOKING#{bid}"}).get("Item")


def config():
    c = rzp_conf()
    return resp(200, {"payments": "razorpay" if c else "demo", "key_id": c.get("key_id")})


def notify_confirmed(b):
    """Best-effort confirmation email to the guest. Never raises: a mail problem must not undo a paid booking."""
    to = b.get("guest_email")
    if not (SES and SENDER and to):
        return
    try:
        title, ci, co, n = b.get("title", "your stay"), b.get("check_in"), b.get("check_out"), len(b.get("nights", []))
        text = (f"Hi {b.get('guest_name', 'there')},\n\nYour booking is confirmed.\n\n{title}\n{ci} to {co} ({n} night(s))\n"
                f"Booking ID: {b.get('id')}\n\nConfirm local details with your host before you travel.\n\nLocalStay India")
        SES.send_email(Source=SENDER, Destination={"ToAddresses": [to]},
                       Message={"Subject": {"Data": f"Booking confirmed: {title}"}, "Body": {"Text": {"Data": text}}})
    except Exception as e:  # noqa: BLE001
        print("email failed", type(e).__name__, str(e)[:200])


def finalize_booking(lid, bid, payment_id="demo"):
    """Turn a held booking into a confirmed one: keep the nights, drop the expiry. Safe to call twice."""
    b = get_booking(lid, bid)
    if b["status"] == "CONFIRMED":
        return
    ops = [{"Update": {"TableName": TABLE, "Key": ser({"PK": f"LISTING#{lid}", "SK": f"NIGHT#{n}"}),
                       "UpdateExpression": "REMOVE hold_until, #t", "ConditionExpression": "booking_id = :b",
                       "ExpressionAttributeNames": {"#t": "ttl"}, "ExpressionAttributeValues": ser({":b": bid})}} for n in b["nights"]]
    ops.append({"Update": {"TableName": TABLE, "Key": ser({"PK": f"LISTING#{lid}", "SK": f"BOOKING#{bid}"}),
                           "UpdateExpression": "SET #s = :c, payment_id = :p REMOVE hold_until", "ConditionExpression": "#s = :h",
                           "ExpressionAttributeNames": {"#s": "status"}, "ExpressionAttributeValues": ser({":c": "CONFIRMED", ":h": "HOLD", ":p": payment_id})}})
    try:
        DB.transact_write_items(TransactItems=ops)
    except DB.exceptions.TransactionCanceledException:
        if get_booking(lid, bid)["status"] == "CONFIRMED":  # someone else (webhook or browser) got there first
            return
        raise Err(409, "Hold expired and the dates were taken. Please book again.")
    notify_confirmed(b)  # only the call that actually confirmed it sends the mail


def refund(lid, bid, pid):
    """Money arrived but the dates are gone: give it back and record what happened."""
    try:
        rzp("POST", f"/payments/{pid}/refund", {})
        status = "REFUNDED"
    except Err:
        print("REFUND FAILED, handle by hand:", lid, bid, pid)
        status = "REFUND_FAILED"
    T.update_item(Key={"PK": f"LISTING#{lid}", "SK": f"BOOKING#{bid}"}, UpdateExpression="SET #s = :s, payment_id = :p REMOVE hold_until",
                  ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": status, ":p": pid})


def settle(lid, bid, pid):
    if get_booking(lid, bid)["status"] in ("REFUNDED", "REFUND_FAILED"):  # a retried webhook must never refund twice
        raise Err(409, "This payment was already refunded.")
    try:
        finalize_booking(lid, bid, pid)
    except Err:
        refund(lid, bid, pid)
        raise


def confirm_booking(sub, lid, bid):
    if not DEMO_PAYMENTS or rzp_conf():
        raise Err(403, "Bookings are confirmed by the payment provider only.")
    b = get_booking(lid, bid)
    if not b or b["guest"] != sub:
        raise Err(404, "Booking not found.")
    finalize_booking(lid, bid)
    return resp(200, {"status": "CONFIRMED"})


def pay_order(sub, lid, bid):
    c = rzp_conf()
    if not c:
        raise Err(501, "Online payments are not enabled.")
    b = get_booking(lid, bid)
    if not b or b["guest"] != sub:
        raise Err(404, "Booking not found.")
    if b["status"] == "CONFIRMED":
        raise Err(409, "This booking is already paid.")
    if b["status"] != "HOLD" or int(b.get("hold_until", 0)) <= int(time.time()):
        raise Err(409, "Hold expired. Please book again.")
    if b.get("order_id"):
        return resp(200, {"key_id": c["key_id"], "order_id": b["order_id"], "amount": int(b["amount_paise"]), "currency": "INR"})
    total = int(b["total"])
    amount = (total + fee_for(total)) * 100  # computed here, so the browser cannot change what is charged
    order = rzp("POST", "/orders", {"amount": amount, "currency": "INR", "receipt": bid, "notes": {"listing_id": lid, "booking_id": bid}})
    oid = order["id"]
    try:
        T.update_item(Key={"PK": f"LISTING#{lid}", "SK": f"BOOKING#{bid}"}, UpdateExpression="SET order_id = :o, amount_paise = :a",
                      ConditionExpression="attribute_not_exists(order_id)", ExpressionAttributeValues={":o": oid, ":a": amount})
    except T.meta.client.exceptions.ConditionalCheckFailedException:  # double click: keep the first order
        return pay_order(sub, lid, bid)
    T.put_item(Item={"PK": f"RZPORDER#{oid}", "SK": "META", "listing_id": lid, "booking_id": bid, "amount_paise": amount})
    return resp(200, {"key_id": c["key_id"], "order_id": oid, "amount": amount, "currency": "INR"})


def verify_payment(sub, lid, bid, b):
    c = rzp_conf()
    if not c:
        raise Err(501, "Online payments are not enabled.")
    bk = get_booking(lid, bid)
    if not bk or bk["guest"] != sub:
        raise Err(404, "Booking not found.")
    oid = bk.get("order_id")
    pid, sig = str(b.get("razorpay_payment_id", "")), str(b.get("razorpay_signature", ""))
    if not oid or not hmac.compare_digest(hmac_hex(c["key_secret"], f"{oid}|{pid}"), sig):  # the order id is OURS, not the one the browser sends
        raise Err(400, "Payment could not be verified.")
    settle(lid, bid, pid)
    return resp(200, {"status": "CONFIRMED"})


def razorpay_webhook(raw, headers):
    """The authoritative confirmation: works even if the guest closed the tab right after paying."""
    c = rzp_conf()
    if not c:
        raise Err(501, "Online payments are not enabled.")
    if not hmac.compare_digest(hmac_hex(c["webhook_secret"], raw), headers.get("x-razorpay-signature", "")):
        raise Err(400, "Bad signature.")
    ev = json.loads(raw)
    if ev.get("event") not in ("payment.captured", "order.paid"):
        return resp(200, {"ignored": True})
    pay = ev["payload"]["payment"]["entity"]
    m = T.get_item(Key={"PK": f"RZPORDER#{pay['order_id']}", "SK": "META"}).get("Item")
    if not m or int(pay["amount"]) != int(m["amount_paise"]):  # unknown order, or paid amount differs from what we asked for
        print("webhook ignored", pay.get("order_id"), pay.get("amount"))
        return resp(200, {"ignored": True})
    try:
        settle(m["listing_id"], m["booking_id"], pay["id"])
    except Err as e:
        return resp(200, {"refunded": True, "reason": e.msg})  # 200 so Razorpay stops retrying; the refund is already done
    return resp(200, {"ok": True})


def my_bookings(sub):
    r = T.query(IndexName="GSI2", KeyConditionExpression=Key("GSI2PK").eq(f"USER#{sub}") & Key("GSI2SK").begins_with("BOOKING#"))
    now = int(time.time())
    return resp(200, {"bookings": [b for b in r["Items"] if b["status"] == "CONFIRMED" or int(b.get("hold_until", 0)) > now]})


# ---------- reviews ----------
def create_review(sub, name, b):
    lid, bid = b.get("listing_id"), b.get("booking_id")
    bk = T.get_item(Key={"PK": f"LISTING#{lid}", "SK": f"BOOKING#{bid}"}).get("Item")
    if not bk or bk["guest"] != sub or bk["status"] != "CONFIRMED":
        raise Err(403, "You can only review your own confirmed bookings.")
    if dt.date.fromisoformat(bk["check_out"]) > dt.date.today():
        raise Err(403, "You can review after check-out.")
    rating, text = int(b.get("rating", 0)), str(b.get("text", "")).strip()
    if not 1 <= rating <= 5 or not 10 <= len(text) <= 1000:
        raise Err(400, "Rating must be 1 to 5 and the review 10 to 1000 characters.")
    given = b.get("verify", {})
    v = {k: (given.get(k) if given.get(k) in ("y", "n", "u") else "u") for k in ("hospital", "food", "network")}
    try:
        T.put_item(Item={"PK": f"LISTING#{lid}", "SK": f"REVIEW#{bid}", "rating": rating, "text": text, "guest": sub, "name": name,
                         "date": dt.date.today().isoformat(), "verify": v}, ConditionExpression="attribute_not_exists(PK)")
    except T.meta.client.exceptions.ConditionalCheckFailedException:
        raise Err(409, "You already reviewed this booking.")
    return resp(201, {"ok": True})


# ---------- verified locals (people who live in a place review it; no ID documents are stored) ----------
def city_key(c):
    return str(c).strip().lower()[:40]


def local_apply(sub, name, email, b):
    city = str(b.get("city", "")).strip()[:40]
    try:
        years = int(b.get("years", 0))
    except (TypeError, ValueError):
        years = 0
    if not city or not 1 <= years <= 80:
        raise Err(400, "Choose your city and the number of years you have lived there (1 to 80).")
    try:
        T.put_item(Item={"PK": f"USER#{sub}", "SK": "LOCALAPP", "status": "PENDING", "city": city, "city_key": city_key(city),
                         "years": years, "note": str(b.get("note", ""))[:300], "email": email, "name": name,
                         "applied": dt.date.today().isoformat()},
                   ConditionExpression="attribute_not_exists(PK) OR #s <> :v",
                   ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":v": "VERIFIED"})
    except T.meta.client.exceptions.ConditionalCheckFailedException:
        raise Err(409, "You are already a Verified Local.")
    return resp(201, {"status": "PENDING"})


def local_me(sub):
    a = T.get_item(Key={"PK": f"USER#{sub}", "SK": "LOCALAPP"}).get("Item")
    if not a:
        return resp(200, {"status": "NONE"})
    return resp(200, {k: a.get(k) for k in ("status", "city", "city_key", "years")})


def create_local_review(sub, name, b):
    a = T.get_item(Key={"PK": f"USER#{sub}", "SK": "LOCALAPP"}).get("Item")
    if not a or a.get("status") != "VERIFIED":
        raise Err(403, "Only Verified Locals can write local reviews.")
    try:
        safety, locals_ = int(b.get("safety", 0)), int(b.get("locals", 0))
    except (TypeError, ValueError):
        raise Err(400, "Ratings must be numbers from 1 to 5.")
    tips = str(b.get("tips", "")).strip()
    if not (1 <= safety <= 5 and 1 <= locals_ <= 5) or not 10 <= len(tips) <= 600:
        raise Err(400, "Give both ratings (1 to 5) and write 10 to 600 characters of local tips.")
    # the review always goes to the city the person was verified for, whatever the client sends
    T.put_item(Item={"PK": f"CITY#{a['city_key']}", "SK": f"LOCALREVIEW#{sub}", "safety": safety, "locals": locals_,
                     "food": str(b.get("food", ""))[:200], "language": str(b.get("language", ""))[:100], "tips": tips,
                     "name": (name or "Local").split()[0][:30], "years": a["years"], "date": dt.date.today().isoformat()})
    return resp(201, {"ok": True})


def list_local_reviews(qs):
    k = city_key(qs.get("city", ""))
    if not k:
        raise Err(400, "city is required.")
    r = T.query(KeyConditionExpression=Key("PK").eq(f"CITY#{k}") & Key("SK").begins_with("LOCALREVIEW#"))
    return resp(200, {"reviews": r["Items"]})


ROUTES = {
    "GET /listings": lambda c: list_listings(c["qs"]),
    "GET /listings/{id}": lambda c: get_listing(c["path"]["id"]),
    "POST /listings": lambda c: create_listing(c["sub"], c["name"], c["body"]),
    "POST /listings/{id}/photo-url": lambda c: photo_upload_form(c["sub"], c["path"]["id"], c["body"]),
    "POST /listings/{id}/photos": lambda c: add_photo(c["sub"], c["path"]["id"], c["body"]),
    "POST /bookings": lambda c: create_booking(c["sub"], c["name"], c["body"], c["email"]),
    "GET /bookings/me": lambda c: my_bookings(c["sub"]),
    "POST /bookings/{lid}/{bid}/confirm": lambda c: confirm_booking(c["sub"], c["path"]["lid"], c["path"]["bid"]),
    "POST /bookings/{lid}/{bid}/pay-order": lambda c: pay_order(c["sub"], c["path"]["lid"], c["path"]["bid"]),
    "POST /bookings/{lid}/{bid}/verify": lambda c: verify_payment(c["sub"], c["path"]["lid"], c["path"]["bid"], c["body"]),
    "GET /config": lambda c: config(),
    "POST /razorpay/webhook": lambda c: razorpay_webhook(c["raw"], c["headers"]),
    "POST /reviews": lambda c: create_review(c["sub"], c["name"], c["body"]),
    "POST /local/apply": lambda c: local_apply(c["sub"], c["name"], c["email"], c["body"]),
    "GET /local/me": lambda c: local_me(c["sub"]),
    "POST /local/reviews": lambda c: create_local_review(c["sub"], c["name"], c["body"]),
    "GET /local/reviews": lambda c: list_local_reviews(c["qs"]),
}


PUBLIC = {"GET /listings", "GET /listings/{id}", "GET /local/reviews", "GET /config", "POST /razorpay/webhook"}


def handler(event, context):
    try:
        claims = (event["requestContext"].get("authorizer") or {}).get("jwt", {}).get("claims", {})
        raw = event.get("body") or ""
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode()
        ctx = {"qs": event.get("queryStringParameters") or {}, "path": event.get("pathParameters") or {},
               "raw": raw, "headers": {k.lower(): v for k, v in (event.get("headers") or {}).items()},
               "body": json.loads(raw or "{}"), "sub": claims.get("sub"),
               "name": claims.get("name") or claims.get("email", "Guest"), "email": claims.get("email", "")}
        if not ctx["sub"] and event["routeKey"] not in PUBLIC:
            raise Err(401, "Please log in.")  # second lock behind the API Gateway authorizer
        return ROUTES[event["routeKey"]](ctx)
    except Err as e:
        return resp(e.code, {"error": e.msg})
    except (KeyError, ValueError, TypeError) as e:
        print("bad request", repr(e))
        return resp(400, {"error": "Bad request."})
    except Exception as e:  # unexpected: log it (CloudWatch), do not leak details
        print("unhandled", repr(e))
        return resp(500, {"error": "Something went wrong."})
