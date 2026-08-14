"""Netflix code fetcher supporting Outlook (Microsoft Graph) and Gmail (IMAP) providers."""
import os
import re
import imaplib
import email as email_lib
from email.header import decode_header

from crypto_utils import decrypt_token
from msgraph import acquire_from_refresh, graph_get, REAUTH_ERRORS

# Ordered category definitions. Reused across both providers.
CATEGORIES = {
    "login_code": {
        "label": "Login Code",
        "description": "Netflix temporary access / household travel code",
        "keywords": ["temporary access code", "household travel", "travel", "get code", "access code"],
        "extract": "code",
    },
    "verification_code": {
        "label": "Verification Code",
        "description": "Account verification / confirm email code",
        "keywords": ["verification code", "verify your", "confirm your", "your code", "verify email"],
        "extract": "code",
    },
    "household": {
        "label": "Household",
        "description": "Netflix Household update / verification link",
        "keywords": ["household", "update your household", "confirm update", "this was me"],
        "extract": "link",
    },
    "password_reset": {
        "label": "Password Reset",
        "description": "Reset your Netflix password link",
        "keywords": ["reset your password", "password", "forgot"],
        "extract": "link",
    },
    "tv_login": {
        "label": "TV Login",
        "description": "Device / TV sign-in code",
        "keywords": ["sign-in code", "sign in code", "new device", "signing in", "tv", "device"],
        "extract": "code",
    },
}


def categories_list():
    return [
        {"key": k, "label": v["label"], "description": v["description"]}
        for k, v in CATEGORIES.items()
    ]


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_netflix_email(subject: str, body_text: str, body_html: str) -> dict:
    text = body_text or _html_to_text(body_html)
    code = None
    m = re.search(r"(?<![\d-])(\d{4,8})(?![\d-])", text)
    if m:
        code = m.group(1)
    links = re.findall(r"https?://[^\s\"'<>]*netflix\.com[^\s\"'<>]*", (body_html or "") + " " + text)
    link = None
    priority = ["travel", "update", "verify", "password", "confirm", "getcode", "get-code", "account/"]
    for l in links:
        if any(p in l.lower() for p in priority):
            link = l
            break
    if not link and links:
        link = links[0]
    return {"code": code, "link": link, "snippet": text[:400]}


def _match_messages(messages, cfg):
    kws = [k.lower() for k in cfg["keywords"]]
    matched = []
    for msg in messages:
        frm = ((msg.get("from") or {}).get("emailAddress") or {}).get("address", "") or ""
        subject = msg.get("subject", "") or ""
        preview = msg.get("bodyPreview", "") or ""
        if "netflix" not in frm.lower():
            continue
        haystack = (subject + " " + preview).lower()
        if any(k in haystack for k in kws):
            matched.append(msg)
    matched.sort(key=lambda m: m.get("receivedDateTime", ""), reverse=True)
    return matched


async def _fetch_via_graph(mailbox: dict, cfg: dict) -> dict:
    try:
        refresh = decrypt_token(mailbox["ms_refresh_token_enc"])
    except Exception:
        return {"status": "needs_reconnect", "reason": "decrypt_failed"}

    result = acquire_from_refresh(refresh)
    if "access_token" not in result:
        err = result.get("error", "")
        if err in REAUTH_ERRORS:
            return {"status": "needs_reconnect", "reason": err or "token_expired"}
        return {"status": "error", "reason": result.get("error_description", "token_error")}

    access_token = result["access_token"]
    new_refresh = result.get("refresh_token")

    params = {
        "$search": '"netflix"',
        "$top": 25,
        "$select": "id,subject,from,receivedDateTime,bodyPreview,webLink,body",
    }
    resp = await graph_get(access_token, "/me/messages", params)
    if resp is None:
        return {"status": "error", "reason": "no_response", "new_refresh": new_refresh}
    if resp.status_code == 401:
        return {"status": "needs_reconnect", "reason": "unauthorized"}
    if resp.status_code == 429:
        return {"status": "throttled", "reason": "rate_limited", "new_refresh": new_refresh}
    if resp.status_code >= 400:
        return {"status": "error", "reason": f"graph_{resp.status_code}", "new_refresh": new_refresh}

    messages = resp.json().get("value", [])
    matched = _match_messages(messages, cfg)
    if not matched:
        return {"status": "empty", "new_refresh": new_refresh}

    msg = matched[0]
    body = msg.get("body") or {}
    parsed = parse_netflix_email(msg.get("subject", ""), None, body.get("content", ""))
    frm = ((msg.get("from") or {}).get("emailAddress") or {}).get("address", "")
    return {
        "status": "found",
        "new_refresh": new_refresh,
        "message": {
            "code": parsed["code"],
            "link": parsed["link"],
            "snippet": parsed["snippet"],
            "subject": msg.get("subject", ""),
            "from": frm,
            "received": msg.get("receivedDateTime", ""),
            "web_link": msg.get("webLink", ""),
        },
    }


def _decode(s):
    if not s:
        return ""
    parts = decode_header(s)
    out = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            out += text.decode(enc or "utf-8", errors="ignore")
        else:
            out += text
    return out


def _fetch_via_gmail_imap(email_norm: str, cfg: dict) -> dict:
    user = os.environ.get("GMAIL_IMAP_USER")
    pw = os.environ.get("GMAIL_IMAP_PASSWORD")
    if not (user and pw):
        return {"status": "not_configured", "reason": "gmail_imap_not_configured"}
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(user, pw)
        M.select("INBOX")
        typ, data = M.search(None, '(FROM "netflix" TO "%s")' % email_norm)
        ids = data[0].split()
        if not ids:
            M.logout()
            return {"status": "empty"}
        kws = [k.lower() for k in cfg["keywords"]]
        for mid in reversed(ids[-25:]):
            typ, msg_data = M.fetch(mid, "(RFC822)")
            raw = msg_data[0][1]
            m = email_lib.message_from_bytes(raw)
            subject = _decode(m.get("Subject"))
            body_text, body_html = "", ""
            if m.is_multipart():
                for part in m.walk():
                    ctype = part.get_content_type()
                    if ctype == "text/plain" and not body_text:
                        body_text = part.get_payload(decode=True).decode(errors="ignore")
                    elif ctype == "text/html" and not body_html:
                        body_html = part.get_payload(decode=True).decode(errors="ignore")
            else:
                body_text = m.get_payload(decode=True).decode(errors="ignore")
            haystack = (subject + " " + body_text + " " + body_html).lower()
            if any(k in haystack for k in kws):
                parsed = parse_netflix_email(subject, body_text, body_html)
                M.logout()
                return {
                    "status": "found",
                    "message": {
                        "code": parsed["code"],
                        "link": parsed["link"],
                        "snippet": parsed["snippet"],
                        "subject": subject,
                        "from": _decode(m.get("From")),
                        "received": m.get("Date", ""),
                        "web_link": "",
                    },
                }
        M.logout()
        return {"status": "empty"}
    except imaplib.IMAP4.error as e:
        return {"status": "error", "reason": f"imap_auth: {e}"}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


async def fetch_netflix_code(mailbox: dict, email_norm: str, category_key: str, provider: str) -> dict:
    cfg = CATEGORIES[category_key]
    if provider == "outlook_graph":
        return await _fetch_via_graph(mailbox, cfg)
    return _fetch_via_gmail_imap(email_norm, cfg)
