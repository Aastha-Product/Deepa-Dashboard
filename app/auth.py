"""Sign-in with a Supabase Auth email link, and roles from app_users.

Flow: /login posts the email -> Supabase emails a link -> it redirects to /auth/callback with the
access token in the URL fragment -> a tiny script posts it to /auth/session -> the server checks the
token with Supabase, looks the email up in app_users and sets a signed, http-only session cookie.
Only people listed in app_users get in; Supabase users are not created from the login page.
"""
import hashlib
import hmac
import os
from dataclasses import dataclass

import httpx
from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

COOKIE = "dp_session"
MAX_AGE = 12 * 3600

ROLE_LABELS = {"raghav": "Senior Demand Planner", "procurement": "Procurement", "deepa": "Head of Operations",
               "it": "IT", "viewer": "Viewer"}
CAN_GATE1 = {"raghav"}
CAN_GATE2 = {"procurement"}
CAN_RUN = {"raghav", "it"}
CAN_LOG_OUTCOME = {"raghav", "procurement"}
CAN_SIGN = {"deepa"}


@dataclass
class User:
    email: str
    name: str
    role: str

    @property
    def role_label(self):
        return ROLE_LABELS.get(self.role, self.role)


class NotSignedIn(Exception):
    pass


def _session_key() -> str:
    """SESSION_SECRET if set; otherwise a key derived from the Supabase JWT secret that the
    Supabase-Vercel integration already stores in Vercel (never the JWT secret itself)."""
    secret = os.environ.get("SESSION_SECRET", "")
    if len(secret) >= 32:
        return secret
    base = os.environ.get("SUPABASE_JWT_SECRET", "")
    if len(base) >= 32:
        return hmac.new(base.encode(), b"freshroots-dashboard-session-v1", hashlib.sha256).hexdigest()
    raise RuntimeError("Set SESSION_SECRET (32+ characters) or connect the Supabase-Vercel integration")


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_session_key(), salt="dp-session")


def _supabase():
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    anon = os.environ.get("SUPABASE_ANON_KEY", "")
    if not url or not anon:
        raise RuntimeError("SUPABASE_URL and SUPABASE_ANON_KEY must be set for sign-in")
    return url, anon


def send_login_link(email: str, redirect_to: str, client: httpx.Client = None) -> None:
    url, anon = _supabase()
    c = client or httpx.Client(timeout=20)
    r = c.post(f"{url}/auth/v1/otp", params={"redirect_to": redirect_to}, headers={"apikey": anon},
               json={"email": email, "create_user": False})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Supabase couldn't send the link (HTTP {r.status_code})")


def email_from_access_token(token: str, client: httpx.Client = None) -> str:
    url, anon = _supabase()
    c = client or httpx.Client(timeout=20)
    r = c.get(f"{url}/auth/v1/user", headers={"apikey": anon, "Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="Sign-in link is invalid or has expired")
    return (r.json().get("email") or "").lower()


def lookup_user(conn, email: str) -> User | None:
    row = conn.execute("SELECT email, name, role FROM app_users WHERE email = %s", (email.lower(),)).fetchone()
    return User(*row) if row else None


def make_cookie(user: User) -> str:
    return _serializer().dumps({"email": user.email, "name": user.name, "role": user.role})


def current_user(request: Request) -> User:
    # Local development only: DP_DEV_USER=email@x,Name,role skips sign-in when not running on Vercel.
    dev = os.environ.get("DP_DEV_USER")
    if dev and not os.environ.get("VERCEL"):
        email, name, role = (p.strip() for p in dev.split(","))
        return User(email, name, role)
    raw = request.cookies.get(COOKIE)
    if not raw:
        raise NotSignedIn()
    try:
        data = _serializer().loads(raw, max_age=MAX_AGE)
    except (BadSignature, SignatureExpired):
        raise NotSignedIn() from None
    return User(data["email"], data["name"], data["role"])


def require(user: User, allowed: set) -> None:
    if user.role not in allowed:
        raise HTTPException(status_code=403, detail="Your role can't do this")
