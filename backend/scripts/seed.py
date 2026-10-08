"""Load sample listings into DynamoDB.  Usage: python scripts/seed.py [table] [region]"""
import json, sys
from decimal import Decimal
import boto3

table = boto3.resource("dynamodb", region_name=sys.argv[2] if len(sys.argv) > 2 else "ap-south-1").Table(sys.argv[1] if len(sys.argv) > 1 else "LocalStay")
# SAMPLE data: hospital_km, food_delivery, networks, atm_km, altitude_m, road_note, season multipliers (Jan..Dec). Verify with real research.
CITY = {
 "Goa": (4, True, ["Jio", "Airtel", "Vi", "BSNL"], 1, 10, "Open all year", [1.6,1.5,1.2,.9,.7,.7,.7,.8,1,1.3,1.6,1.8]),
 "Manali": (3, True, ["Jio", "Airtel", "BSNL"], 2, 2050, "Rohtang side closed Nov to May", [1.3,1.2,1,1.1,1.1,1.1,.8,.8,.9,1,1.4,1.5]),
 "Rishikesh": (5, True, ["Jio", "Airtel", "Vi"], 1, 350, "Open all year", [1,1.1,1.3,1.3,1.1,.8,.8,1.2,1.3,1.2,1,1]),
 "Jaipur": (3, True, ["Jio", "Airtel", "Vi", "BSNL"], .5, 430, "Open all year", [1.3,1.3,1.1,.8,.7,.7,.8,.9,1,1.3,1.5,1.5]),
 "Pondicherry": (3, True, ["Jio", "Airtel", "Vi", "BSNL"], .8, 3, "Open all year", [1.3,1.3,1.1,.9,.8,.8,.8,.9,1,1,1.2,1.5]),
 "Coorg": (12, False, ["Airtel", "BSNL"], 6, 1150, "Landslide risk in monsoon", [1.2,1.2,1.2,1.3,1.1,.8,.8,.9,1.1,1.3,1.2,1.5]),
 "Munnar": (9, True, ["Jio", "Airtel", "BSNL"], 3, 1600, "Landslide risk in monsoon", [1.2,1.2,1.2,1.3,1.1,.8,.8,.9,1.1,1.3,1.2,1.5]),
 "Varanasi": (2, True, ["Jio", "Airtel", "Vi", "BSNL"], .3, 80, "Narrow lanes, no car access", [1.1,1.2,1,.8,.7,.7,.8,.9,1.2,1.4,1.4,1.2]),
 "Himachal": (45, False, ["Airtel", "BSNL"], 20, 3800, "Passes close in winter", [.7,.7,.8,1,1.4,1.6,1.7,1.6,1.3,.8,.7,.7]),
}
LISTINGS = [("Goa","Azure Bliss Villa","Priya D.",8500,"beachfront"),("Goa","Panjim Studio","Carlos F.",3200,"budget"),
 ("Manali","Himalayan Hearth Cottage","Ram Singh",4200,"mountain",{"hospital_km":18,"food_delivery":False}),("Manali","Old Manali Treehouse","Sunita S.",3800,"nature"),
 ("Rishikesh","Ganga View Home","Annapurna D.",3500,"heritage"),("Rishikesh","Forest Retreat","Swati J.",5200,"nature"),
 ("Jaipur","Haveli Royal Chambers","Farooq M.",7500,"heritage"),("Jaipur","Sanganer Artist House","Gopal K.",2600,"budget"),
 ("Pondicherry","French Quarter Maison","Anita K.",6500,"heritage"),("Pondicherry","Auroville Eco Stay","Lakshmi S.",3800,"nature"),
 ("Coorg","Coffee Blossom Estate","Suresh P.",7800,"nature"),("Coorg","Misty Hills Cottage","Kavitha N.",5200,"mountain"),
 ("Munnar","Tea Garden Bungalow","George T.",6400,"nature"),("Munnar","Kerala Family Homestay","Lissy M.",3800,"budget"),
 ("Varanasi","Ghats Heritage Mansion","Ramesh S.",5200,"heritage"),("Varanasi","Silk Weaver's Home","Irfan A.",2800,"heritage"),
 ("Himachal","Spiti Mud House","Dorje N.",3200,"mountain"),("Himachal","Kasol Pine Nest","Ratan T.",2800,"nature",{"hospital_km":30,"altitude_m":1640,"food_delivery":False})]

with table.batch_writer() as bw:
    for i, row in enumerate(LISTINGS, 1):
        city, title, host, base, typ = row[:5]
        h, f, n, a, al, rd, season = CITY[city]
        lid = f"seed{i:02d}"
        item = {"PK": f"LISTING#{lid}", "SK": "META", "id": lid, "title": title, "city": city, "host": host, "host_id": "seed",
                "type": typ, "base": base, "hospital_km": h, "food_delivery": f, "networks": n, "atm_km": a,
                "altitude_m": al, "road_note": rd, "season": season, "GSI1PK": f"CITY#{city.lower()}", "GSI1SK": f"LISTING#{lid}",
                "GSI2PK": "USER#seed", "GSI2SK": f"LISTING#{lid}"}
        item.update(row[5] if len(row) > 5 else {})
        bw.put_item(Item=json.loads(json.dumps(item), parse_float=Decimal))
print(f"Seeded {len(LISTINGS)} listings")
