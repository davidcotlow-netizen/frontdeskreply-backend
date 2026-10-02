"""
Security helpers: admin auth for dashboard/API routes, Twilio + Retell webhook verification.

Admin routes accept EITHER
  * `X-Admin-Key: <ADMIN_API_KEY>` (server-side scripts), or
  * `Authorization: Bearer <Clerk session JWT>` from the dashboard. The token must be signed by
    this Clerk instance (JWKS fetched with CLERK_SECRET_KEY) and the Clerk user's
    public_metadata.business_id must match the business being accessed.

Kill switches (env, no redeploy needed to relax):
  ADMIN_AUTH_MODE   enforce (default) | log | off
  TWILIO_SIG_MODE   auto (default: log until the first valid signature proves the token/URL setup,
                    then enforce) | enforce | log | off
  RETELL_SIG_MODE   log (default) | enforce | off
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from typing import Optional

import httpx
import jwt
from fastapi import HTTPException, Request

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_JWKS_CACHE: dict = {"keys": None, "at": 0.0}
_USER_BIZ_CACHE: dict[str, tuple[Optional[str], float]] = {}
_CACHE_TTL = 300  # seconds
_TWILIO_STATE = {"verified_once": False}


def _mode(name: str, default: str) -> str:
    return (os.environ.get(name) or default).strip().lower()


# ── Clerk ────────────────────────────────────────────────────────────────────

def _clerk_jwks() -> list:
    now = time.time()
    if _JWKS_CACHE["keys"] and now - _JWKS_CACHE["at"] < _CACHE_TTL:
        return _JWKS_CACHE["keys"]
    secret = get_settings().clerk_secret_key
    if not secret:
        raise HTTPException(status_code=503, detail="Auth not configured")
    r = httpx.get("https://api.clerk.com/v1/jwks",
                  headers={"Authorization": f"Bearer {secret}"}, timeout=10)
    r.raise_for_status()
    keys = r.json().get("keys", [])
    _JWKS_CACHE.update(keys=keys, at=now)
    return keys


def _clerk_user_business_id(user_id: str) -> Optional[str]:
    now = time.time()
    hit = _USER_BIZ_CACHE.get(user_id)
    if hit and now - hit[1] < _CACHE_TTL:
        return hit[0]
    secret = get_settings().clerk_secret_key
    r = httpx.get(f"https://api.clerk.com/v1/users/{user_id}",
                  headers={"Authorization": f"Bearer {secret}"}, timeout=10)
    biz = None
    if r.status_code == 200:
        meta = r.json().get("public_metadata") or {}
        biz = meta.get("business_id")
    _USER_BIZ_CACHE[user_id] = (biz, now)
    return biz


def _verify_clerk_token(token: str) -> str:
    """Return the Clerk user id (sub) for a valid session token, else raise."""
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    keys = _clerk_jwks()
    jwk = next((k for k in keys if k.get("kid") == kid), None)
    if jwk is None:  # key rotated: refresh once
        _JWKS_CACHE["keys"] = None
        jwk = next((k for k in _clerk_jwks() if k.get("kid") == kid), None)
    if jwk is None:
        raise HTTPException(status_code=401, detail="Invalid token")
    try:
        key = jwt.algorithms.RSAAlgorithm.from_jwk(jwk)
        claims = jwt.decode(token, key=key, algorithms=["RS256"],
                            options={"verify_aud": False}, leeway=10)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Invalid token")
    return sub


def _requested_business_id(request: Request) -> Optional[str]:
    return (request.query_params.get("business_id")
            or request.path_params.get("business_id"))


async def require_admin(request: Request) -> None:
    """FastAPI dependency for every non-public route."""
    mode = _mode("ADMIN_AUTH_MODE", "enforce")
    if mode == "off":
        return
    try:
        _check_admin(request)
    except HTTPException as e:
        if mode == "log":
            logger.warning(f"admin_auth_would_reject path={request.url.path} reason={e.detail}")
            return
        raise


def _check_admin(request: Request) -> None:
    admin_key = os.environ.get("ADMIN_API_KEY", "")
    supplied = request.headers.get("X-Admin-Key", "")
    if admin_key and supplied and hmac.compare_digest(admin_key, supplied):
        return
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth.startswith("Bearer fdr_"):
        raise HTTPException(status_code=401, detail="Authentication required")
    user_id = _verify_clerk_token(auth[7:].strip())
    user_biz = _clerk_user_business_id(user_id)
    if not user_biz:
        raise HTTPException(status_code=403, detail="No business linked to this account")
    wanted = _requested_business_id(request)
    if wanted and wanted != user_biz:
        raise HTTPException(status_code=403, detail="Not allowed for this business")
    request.state.business_id = user_biz


# ── Twilio ───────────────────────────────────────────────────────────────────

def _twilio_signature(auth_token: str, url: str, params: dict) -> str:
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    mac = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(mac).decode()


def _candidate_urls(request: Request) -> list[str]:
    """Twilio signs the exact public URL. Behind Railway's proxy the scheme/host we see can differ."""
    path_qs = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    hosts = {request.headers.get("x-forwarded-host") or request.headers.get("host") or "",
             (os.environ.get("PUBLIC_API_HOST") or "api.frontdeskreply.com")}
    urls = []
    for h in filter(None, hosts):
        urls += [f"https://{h}{path_qs}", f"http://{h}{path_qs}"]
    urls.append(str(request.url))
    return list(dict.fromkeys(urls))


async def verify_twilio(request: Request) -> None:
    """Dependency for Twilio webhooks. Reads the form (Starlette caches it for the handler)."""
    mode = _mode("TWILIO_SIG_MODE", "auto")
    if mode == "off":
        return
    token = get_settings().twilio_auth_token
    if not token:
        logger.error("twilio_sig_unchecked: TWILIO_AUTH_TOKEN not set; allowing request")
        return
    sig = request.headers.get("X-Twilio-Signature", "")
    form = await request.form()
    params = {k: v for k, v in form.items()}
    ok = bool(sig) and any(
        hmac.compare_digest(_twilio_signature(token, u, params), sig) for u in _candidate_urls(request)
    )
    if ok:
        _TWILIO_STATE["verified_once"] = True
        return
    enforce = mode == "enforce" or (mode == "auto" and _TWILIO_STATE["verified_once"])
    logger.warning(f"twilio_sig_invalid path={request.url.path} mode={mode} enforcing={enforce}")
    if enforce:
        raise HTTPException(status_code=403, detail="Invalid signature")


# ── Retell ───────────────────────────────────────────────────────────────────

def retell_signature_ok(raw_body: bytes, header: str) -> bool:
    """Retell signs webhooks as `v=<ms timestamp>,d=<hex hmac-sha256(api_key, body + timestamp)>`."""
    key = get_settings().retell_api_key
    if not key or not header:
        return False
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts, digest = parts["v"], parts["d"]
    except Exception:
        return False
    if abs(time.time() * 1000 - int(ts)) > 5 * 60 * 1000:
        return False
    expected = hmac.new(key.encode(), raw_body + ts.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, digest)


async def verify_retell(request: Request) -> None:
    mode = _mode("RETELL_SIG_MODE", "log")
    if mode == "off":
        return
    body = await request.body()
    if retell_signature_ok(body, request.headers.get("x-retell-signature", "")):
        return
    logger.warning(f"retell_sig_invalid path={request.url.path} mode={mode}")
    if mode == "enforce":
        raise HTTPException(status_code=403, detail="Invalid signature")
