"""Container version of the LocalStay API.

The SAME business code (api/handler.py) that runs in Lambda runs here behind FastAPI.
This file only does what API Gateway does for Lambda: match the route, verify the Cognito token,
and build the event. It also serves the frontend so one container is the whole site.
"""
import os, re
import jwt
from jwt import PyJWKClient
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

import handler  # shared with the Lambda deployment

REGION = os.environ.get("AWS_DEFAULT_REGION", "ap-south-1")
POOL = os.environ.get("USER_POOL_ID", "")
CLIENT = os.environ.get("CLIENT_ID", "")
ISS = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL}"
_jwks = PyJWKClient(ISS + "/.well-known/jwks.json") if POOL else None
HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = next((p for p in (os.path.join(HERE, "index.html"), os.path.join(HERE, "..", "..", "frontend", "index.html")) if os.path.exists(p)), None)

app = FastAPI(title="LocalStay API (container)", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _compile():
    out = []
    for key in handler.ROUTES:
        method, path = key.split(" ", 1)
        out.append((method, re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", path) + "$"), key))
    return out


ROUTES = _compile()


def claims_from(request):
    """Verify the Cognito ID token (signature, issuer, audience). Bad or missing token = no claims, so protected routes answer 401."""
    auth = request.headers.get("authorization", "")
    if not auth or not _jwks:
        return {}
    token = auth.split(" ", 1)[-1].strip()
    try:
        key = _jwks.get_signing_key_from_jwt(token).key
        c = jwt.decode(token, key, algorithms=["RS256"], issuer=ISS, audience=CLIENT)
        return c if c.get("token_use") == "id" else {}
    except Exception:
        return {}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not INDEX:
        return HTMLResponse("frontend not found", status_code=404)
    html = open(INDEX, encoding="utf-8").read()
    origin = str(request.base_url).rstrip("/")
    html = re.sub(r'apiBase:"[^"]*"', f'apiBase:"{origin}"', html, count=1)  # the site talks to this same server
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.api_route("/{full_path:path}", methods=["GET", "POST"])
async def gateway(full_path: str, request: Request):
    path = "/" + full_path
    for method, rx, key in ROUTES:
        m = rx.match(path)
        if method == request.method and m:
            body = (await request.body()).decode() or None
            event = {"routeKey": key, "pathParameters": m.groupdict() or None,
                     "queryStringParameters": dict(request.query_params) or None, "body": body,
                     "requestContext": {"authorizer": {"jwt": {"claims": claims_from(request)}}}}
            r = await run_in_threadpool(handler.handler, event, None)
            return Response(content=r["body"], status_code=r["statusCode"], media_type="application/json")
    return JSONResponse({"error": "Not found."}, status_code=404)
