import os
import uuid
import logging
import secrets
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

from fastapi import FastAPI, APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field

from db import db
from auth_utils import (
    hash_password,
    verify_password,
    create_access_token,
    get_current_admin,
)
import msgraph
from services.email_fetcher import (
    CATEGORIES,
    categories_list,
    fetch_netflix_code,
)
from crypto_utils import encrypt_token

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("himawari24")

app = FastAPI(title="himawari24 - Netflix Email Access")
api = APIRouter(prefix="/api")

PROVIDERS = {"gmail_imap", "outlook_graph"}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def normalize_email(e: str) -> str:
    return (e or "").strip().lower()


# ---------- Models ----------
class LoginReq(BaseModel):
    email: EmailStr
    password: str


class AssignmentCreate(BaseModel):
    email: EmailStr
    provider: str = "outlook_graph"
    label: str = ""


class AssignmentUpdate(BaseModel):
    provider: str


class SearchReq(BaseModel):
    email_norm: str
    category: str


# ---------- Auth ----------
@api.post("/auth/login")
async def login(body: LoginReq):
    email = normalize_email(body.email)
    user = await db.users.find_one({"email": email})
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = create_access_token(user["id"], email)
    return {
        "access_token": token,
        "user": {"id": user["id"], "email": email, "name": user.get("name", "Admin"), "role": user["role"]},
    }


@api.get("/auth/me")
async def me(admin=Depends(get_current_admin)):
    return admin


# ---------- Config status ----------
@api.get("/config/status")
async def config_status(admin=Depends(get_current_admin)):
    return {
        "microsoft_configured": msgraph.is_configured(),
        "gmail_configured": bool(os.environ.get("GMAIL_IMAP_USER") and os.environ.get("GMAIL_IMAP_PASSWORD")),
        "redirect_uri": os.environ.get("MS_REDIRECT_URI", ""),
        "authority": os.environ.get("MS_AUTHORITY", ""),
    }


@api.get("/categories")
async def get_categories(admin=Depends(get_current_admin)):
    return categories_list()


# ---------- Assignments ----------
@api.get("/assignments")
async def list_assignments(admin=Depends(get_current_admin)):
    docs = await db.email_assignments.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)
    mailboxes = await db.mailbox_accounts.find({}, {"_id": 0, "ms_refresh_token_enc": 0}).to_list(500)
    mb_by_email = {m["email_norm"]: m for m in mailboxes}
    for d in docs:
        mb = mb_by_email.get(d["email_norm"])
        d["mailbox_status"] = mb["status"] if mb else None
        d["mailbox_email"] = mb.get("mailbox_email") if mb else None
    return docs


@api.post("/assignments")
async def create_assignment(body: AssignmentCreate, admin=Depends(get_current_admin)):
    if body.provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail="Invalid provider")
    email_norm = normalize_email(body.email)
    existing = await db.email_assignments.find_one({"email_norm": email_norm})
    if existing:
        raise HTTPException(status_code=409, detail="Email already assigned")
    doc = {
        "id": str(uuid.uuid4()),
        "email_norm": email_norm,
        "provider": body.provider,
        "label": body.label or "",
        "created_at": now_iso(),
    }
    await db.email_assignments.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api.patch("/assignments/{assignment_id}")
async def update_assignment(assignment_id: str, body: AssignmentUpdate, admin=Depends(get_current_admin)):
    if body.provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail="Invalid provider")
    res = await db.email_assignments.update_one(
        {"id": assignment_id}, {"$set": {"provider": body.provider}}
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Assignment not found")
    return {"ok": True}


@api.delete("/assignments/{assignment_id}")
async def delete_assignment(assignment_id: str, admin=Depends(get_current_admin)):
    doc = await db.email_assignments.find_one({"id": assignment_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Assignment not found")
    await db.email_assignments.delete_one({"id": assignment_id})
    await db.mailbox_accounts.delete_one({"email_norm": doc["email_norm"]})
    return {"ok": True}


# ---------- Mailboxes ----------
@api.get("/mailboxes")
async def list_mailboxes(admin=Depends(get_current_admin)):
    docs = await db.mailbox_accounts.find({}, {"_id": 0, "ms_refresh_token_enc": 0, "access_token_cache": 0}).to_list(500)
    return docs


@api.get("/mailboxes/connect")
async def mailboxes_connect(email_norm: str, admin=Depends(get_current_admin)):
    if not msgraph.is_configured():
        raise HTTPException(status_code=400, detail="Microsoft OAuth is not configured. Set MS_CLIENT_ID and MS_CLIENT_SECRET.")
    email_norm = normalize_email(email_norm)
    assignment = await db.email_assignments.find_one({"email_norm": email_norm})
    if not assignment:
        raise HTTPException(status_code=404, detail="No assignment for this email")

    state = secrets.token_urlsafe(24)
    flow = msgraph.build_auth_flow(state)
    if "auth_uri" not in flow:
        raise HTTPException(status_code=500, detail=flow.get("error_description", "MSAL error"))
    await db.oauth_states.insert_one({
        "state": state,
        "email_norm": email_norm,
        "flow": flow,
        "created_at": now_iso(),
        "created_dt": datetime.now(timezone.utc),
    })
    return {"auth_url": flow["auth_uri"]}


@api.get("/oauth/microsoft/callback")
async def oauth_callback(request: Request):
    frontend = os.environ.get("FRONTEND_URL", "")
    params = dict(request.query_params)
    state = params.get("state")
    if not state:
        return RedirectResponse(f"{frontend}/?error=missing_state")
    stored = await db.oauth_states.find_one_and_delete({"state": state})
    if not stored:
        return RedirectResponse(f"{frontend}/?error=invalid_state")

    try:
        result = msgraph.redeem_auth_code(stored["flow"], params)
    except Exception as e:
        logger.error("Token redemption failed: %s", e)
        return RedirectResponse(f"{frontend}/?error=token_exchange_failed")

    if "access_token" not in result:
        return RedirectResponse(f"{frontend}/?error=token_exchange_failed")
    refresh = result.get("refresh_token")
    if not refresh:
        return RedirectResponse(f"{frontend}/?error=no_refresh_token")

    # Detect the connected mailbox address
    resp = await msgraph.graph_get(result["access_token"], "/me", {"$select": "id,displayName,mail,userPrincipalName"})
    mailbox_email = ""
    if resp is not None and resp.status_code < 400:
        me_data = resp.json()
        mailbox_email = me_data.get("mail") or me_data.get("userPrincipalName") or ""

    email_norm = stored["email_norm"]
    await db.mailbox_accounts.update_one(
        {"email_norm": email_norm},
        {"$set": {
            "email_norm": email_norm,
            "provider": "outlook_graph",
            "mailbox_email": mailbox_email,
            "ms_refresh_token_enc": encrypt_token(refresh),
            "status": "connected",
            "connected_at": now_iso(),
            "updated_at": now_iso(),
        }},
        upsert=True,
    )
    return RedirectResponse(f"{frontend}/?connected={email_norm}")


@api.post("/mailboxes/{email_norm}/disconnect")
async def disconnect_mailbox(email_norm: str, admin=Depends(get_current_admin)):
    email_norm = normalize_email(email_norm)
    res = await db.mailbox_accounts.delete_one({"email_norm": email_norm})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Mailbox not connected")
    return {"ok": True}


# ---------- Search ----------
@api.post("/search")
async def search(body: SearchReq, admin=Depends(get_current_admin)):
    email_norm = normalize_email(body.email_norm)
    if body.category not in CATEGORIES:
        raise HTTPException(status_code=400, detail="Unknown category")
    assignment = await db.email_assignments.find_one({"email_norm": email_norm})
    if not assignment:
        raise HTTPException(status_code=404, detail="No assignment for this email")

    provider = assignment["provider"]
    mailbox = await db.mailbox_accounts.find_one({"email_norm": email_norm})

    if provider == "outlook_graph":
        if not mailbox or mailbox.get("status") != "connected":
            return {"found": False, "reason": "not_connected", "provider": provider}
        result = await fetch_netflix_code(mailbox, email_norm, body.category, provider)
    else:
        result = await fetch_netflix_code(mailbox or {}, email_norm, body.category, provider)

    status = result.get("status")

    # Persist rotated refresh token if Microsoft returned a new one
    if result.get("new_refresh") and mailbox:
        await db.mailbox_accounts.update_one(
            {"email_norm": email_norm},
            {"$set": {"ms_refresh_token_enc": encrypt_token(result["new_refresh"]), "updated_at": now_iso()}},
        )

    if status == "needs_reconnect":
        if mailbox:
            await db.mailbox_accounts.update_one(
                {"email_norm": email_norm},
                {"$set": {"status": "needs_reconnect", "updated_at": now_iso()}},
            )
        return {"found": False, "reason": "needs_reconnect", "provider": provider}

    if status == "found":
        return {"found": True, "provider": provider, **result["message"]}

    return {"found": False, "reason": status, "provider": provider}


app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    # Idempotent admin seeding
    admin_email = normalize_email(os.environ.get("ADMIN_EMAIL", "admin@himawari24.local"))
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin123")
    existing = await db.users.find_one({"email": admin_email})
    if existing is None:
        await db.users.insert_one({
            "id": str(uuid.uuid4()),
            "email": admin_email,
            "password_hash": hash_password(admin_password),
            "name": "Admin",
            "role": "admin",
            "created_at": now_iso(),
        })
        logger.info("Seeded admin %s", admin_email)
    elif not verify_password(admin_password, existing["password_hash"]):
        await db.users.update_one({"email": admin_email}, {"$set": {"password_hash": hash_password(admin_password)}})

    await db.email_assignments.create_index("email_norm", unique=True)
    await db.mailbox_accounts.create_index("email_norm", unique=True)
    await db.oauth_states.create_index("state", unique=True)
    await db.oauth_states.create_index("created_dt", expireAfterSeconds=600)


@app.on_event("shutdown")
async def shutdown():
    pass
