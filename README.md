# LocalStay: all files in one place

```
localstay-project/
  frontend/index.html     the website (open in a browser; works in demo mode with localStorage)
  backend/
    api/handler.py        Lambda: listings, bookings, reviews
    infra/main.tf         Terraform: DynamoDB, Lambda, API Gateway, Cognito, budget alert
    scripts/seed.py       loads 18 sample listings
    README.md             deploy steps
```

## Quick start
1. Open `frontend/index.html` in your browser to try the site (demo mode, no AWS needed).
2. Deploy the backend: follow `backend/README.md`.
3. Next step: connect the frontend to the API (replace localStorage calls with fetch to `api_url`, and the demo login with Cognito).

All listings, distances, ratings and prices are sample data. Verify before real use.
