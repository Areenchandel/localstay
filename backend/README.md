# LocalStay backend (serverless, free-tier friendly)

Stack: API Gateway (HTTP API) -> Lambda (Python, ARM) -> DynamoDB (single table, on-demand). Login: Cognito. All infra is Terraform.

## Deploy
```bash
cd infra
terraform init
terraform apply -var budget_email=you@example.com      # creates everything, prints api_url, user_pool_id, client_id
cd ..
pip install boto3
python scripts/seed.py LocalStay ap-south-1             # loads sample listings
curl "$(terraform -chdir=infra output -raw api_url)/listings?city=goa"
```
Run `terraform destroy` when you are done experimenting.

## Endpoints
| Route | Login | What it does |
|---|---|---|
| GET /listings?city= | no | list stays (with tonight's price) |
| GET /listings/{id} | no | listing, reviews, guest verification, booked nights, next 7 prices |
| POST /listings | yes | host adds a stay |
| POST /bookings | yes | holds the nights for 10 minutes (all-or-nothing transaction) |
| POST /bookings/{lid}/{bid}/confirm | yes | demo payment confirm (blocked unless DEMO_PAYMENTS=true) |
| GET /bookings/me | yes | my bookings |
| POST /reviews | yes | review + verify hospital/food/network (only after check-out) |

Send the Cognito **ID token** in the `Authorization` header (the API authorizer checks the token's audience).

## Design notes (interview answers)
- Double-booking: one lock item per night; `TransactWriteItems` with a condition fails if any night is taken.
- Holds: `hold_until` makes a lock count as free once it expires, so TTL delay (up to 48h) does not matter.
- Prices are computed on the server; the client cannot send a price.
- Confirm is open only in demo mode; in production a Razorpay webhook (signature verified) confirms bookings.
