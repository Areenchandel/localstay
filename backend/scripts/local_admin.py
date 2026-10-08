"""Verify Verified-Local applications.  You check the person's address proof yourself (email / video call),
then approve here. No ID document is ever stored: only the yes/no below.

  python scripts\\local_admin.py list                 show pending applications
  python scripts\\local_admin.py approve a@b.com      mark VERIFIED
  python scripts\\local_admin.py reject a@b.com       mark REJECTED
"""
import sys
import boto3
from boto3.dynamodb.conditions import Attr

t = boto3.resource("dynamodb", region_name="ap-south-1").Table("LocalStay")


def apps():
    out, kw = [], {"FilterExpression": Attr("SK").eq("LOCALAPP")}
    while True:
        r = t.scan(**kw)
        out += r["Items"]
        if "LastEvaluatedKey" not in r:
            return out
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
if cmd == "list":
    rows = [a for a in apps() if a["status"] == "PENDING"]
    for a in rows:
        print(f"{a['email']:35} {a['city']:12} {a['years']} yrs  applied {a['applied']}  note: {a.get('note','')}")
    print(f"{len(rows)} pending")
elif cmd in ("approve", "reject") and len(sys.argv) > 2:
    email = sys.argv[2].lower()
    hit = [a for a in apps() if a.get("email", "").lower() == email]
    if not hit:
        sys.exit("No application found for " + email)
    status = "VERIFIED" if cmd == "approve" else "REJECTED"
    t.update_item(Key={"PK": hit[0]["PK"], "SK": "LOCALAPP"}, UpdateExpression="SET #s = :s",
                  ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": status})
    print(email, "->", status)
else:
    sys.exit(__doc__)
