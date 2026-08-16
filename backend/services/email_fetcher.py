"""Netflix code fetcher supporting Outlook (Microsoft Graph) and Gmail (IMAP) providers."""
import os
import re
import html as html_lib
import imaplib
import email as email_lib
from email.header import decode_header

from crypto_utils import decrypt_token
from msgraph import acquire_from_refresh, graph_get, REAUTH_ERRORS

# Ordered category definitions. Reused across both providers.
# Keyword lists are multilingual (EN/PT/ES/FR/DE/IT) since Netflix localizes
# subject lines based on the recipient's account region/language. Keywords are
# grouped per language (rather than one flat list) so we can also detect which
# locale an email was sent in and surface a country flag for it.
CATEGORIES = {
    "login_code": {
        "label": "Login Code",
        "description": "Netflix temporary access / household travel code",
        "keywords": [
            "temporary access code", "household travel", "travel", "get code", "access code",
            "código de acesso", "codigo de acesso", "acesso temporário", "acesso temporario",
            "código de acceso", "codigo de acceso", "acceso temporal",
            "code d'accès", "code d'acces", "accès temporaire",
            "zugangscode", "vorübergehender zugangscode",
            "codice di accesso", "accesso temporaneo",
        ],
        "extract": "code",
    },
    "verification_code": {
        "label": "Verification Code",
        "description": "Account verification / confirm email code",
        "keywords": [
            "verification code", "verify your", "confirm your", "your code", "verify email",
            "código de verificação", "codigo de verificacao", "verifique sua",
            "código de verificación", "codigo de verificacion", "verifica tu",
            "code de vérification", "code de verification", "vérifiez votre",
            "bestätigungscode", "bestatigungscode", "bestätige deine",
            "codice di verifica", "verifica il tuo",
        ],
        "extract": "code",
    },
    "household": {
        "label": "Household",
        "description": "Netflix Household update / verification link",
        "keywords": [
            "household", "update your household", "confirm update", "this was me",
            "residência", "residencia", "atualizar sua residência", "atualizar sua residencia",
            "hogar", "actualizar tu hogar",
            "foyer", "mettre à jour votre foyer", "mettre a jour votre foyer",
            "haushalt", "haushalt aktualisieren",
            "nucleo familiare", "aggiorna il tuo nucleo",
        ],
        "extract": "link",
    },
    "password_reset": {
        "label": "Password Reset",
        "description": "Reset your Netflix password link",
        "keywords": [
            "reset your password", "password", "forgot", "password reset request",
            "redefinir sua senha", "senha", "esqueceu",
            "restablecer tu contraseña", "restablecer tu contrasena", "contraseña", "contrasena",
            "réinitialiser votre mot de passe", "reinitialiser votre mot de passe", "mot de passe",
            "passwort zurücksetzen", "passwort zurucksetzen", "passwort",
            "reimposta la password", "password",
        ],
        "extract": "link",
    },
    "tv_login": {
        "label": "TV Login",
        "description": "Device / TV sign-in code",
        "keywords": [
            "sign-in code", "sign in code", "new device", "signing in", "tv", "device",
            "código de entrada", "codigo de entrada", "novo dispositivo",
            "código de inicio de sesión", "codigo de inicio de sesion", "nuevo dispositivo",
            "code de connexion", "nouvel appareil",
            "anmeldecode", "neues gerät", "neues gerat",
            "codice di accesso tv", "nuovo dispositivo",
        ],
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
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# Link-URL keyword priority, per category — the category being searched must
# rank its own kind of link first, otherwise an unrelated footer link (e.g. a
# generic "manage/update account" link) can outrank the actual actionable link,
# since Netflix emails often contain several netflix.com links in one message.
_LINK_PRIORITY = {
    "password_reset": ["password", "reset", "senha", "contrase", "mot-de-passe", "mot_de_passe", "passwort"],
    "household": ["household", "hogar", "foyer", "haushalt", "residen", "nucleo", "confirm", "update"],
}
_LINK_PRIORITY_FALLBACK = ["password", "household", "verify", "confirm", "travel", "getcode", "get-code", "update", "account/"]

_FLAG_EMOJI_RE = re.compile("[\U0001F1E6-\U0001F1FF]{2}")
_HTML_LANG_RE = re.compile(r'<html[^>]*\blang=["\']([a-zA-Z]{2})(?:[-_]([a-zA-Z]{2}))?["\']', re.IGNORECASE)

# Fallback: infer a country from the language when no explicit region subtag
# (e.g. "pt" -> Brazil is by far Netflix's largest pt-speaking market) is present.
_LANG_TO_COUNTRY = {
    "pt": "BR", "es": "ES", "en": "US", "fr": "FR", "de": "DE", "it": "IT",
}


def _country_flag_from_code(cc: str):
    if not cc or len(cc) != 2 or not cc.isalpha():
        return None
    cc = cc.upper()
    return "".join(chr(0x1F1E6 + (ord(c) - ord("A"))) for c in cc)


def _detect_country(raw_html: str, text: str):
    """Best-effort region detection for the footer of a Netflix email, used to
    show a country flag alongside password-reset / household results. Looks for
    a literal flag emoji Netflix already embeds, then falls back to the html
    lang attribute Netflix sets per-region."""
    for haystack in (raw_html, text):
        if not haystack:
            continue
        m = _FLAG_EMOJI_RE.search(haystack)
        if m:
            return {"flag": m.group(0), "code": None}
    if raw_html:
        m = _HTML_LANG_RE.search(raw_html)
        if m:
            lang = m.group(1).lower()
            region = (m.group(2) or _LANG_TO_COUNTRY.get(lang) or "").upper()
            if region:
                return {"flag": _country_flag_from_code(region), "code": region}
    return None


def parse_netflix_email(subject: str, body_text: str, body_html: str, extract_type: str = "code", category_key: str = None) -> dict:
    text = body_text or _html_to_text(body_html)
    code = None
    link = None
    # Only pull a code for code-type categories (login/verification/tv). Link-only
    # categories (password_reset, household) must never surface a spurious digit
    # match (support phone numbers, footer reference numbers, etc.) as if it were
    # an OTP — that's misleading, so we simply don't look for one.
    if extract_type == "code":
        m = re.search(r"(?<![\d-])(\d{4,8})(?![\d-])", text)
        if m:
            code = m.group(1)
    if extract_type == "link":
        # Decode HTML entities (Graph/IMAP hand back raw markup where a literal
        # "&" inside an href is escaped as "&amp;"); without this, every query
        # param after the first gets glued together with a literal "amp;" and
        # the link comes out malformed.
        decoded_html = html_lib.unescape(body_html or "")
        links = re.findall(r"https?://[^\s\"'<>]*netflix\.com[^\s\"'<>]*", decoded_html + " " + text)
        priority = _LINK_PRIORITY.get(category_key, _LINK_PRIORITY_FALLBACK)
        best = None
        best_rank = len(priority)
        for l in links:
            low = l.lower()
            for rank, p in enumerate(priority):
                if p in low and rank < best_rank:
                    best = l
                    best_rank = rank
                    break
        link = best or (links[0] if links else None)
    country = _detect_country(body_html, text)
    return {"code": code, "link": link, "snippet": text[:400], "country": country}


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


async def _fetch_via_graph(mailbox: dict, cfg: dict, category_key: str) -> dict:
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
    parsed = parse_netflix_email(msg.get("subject", ""), None, body.get("content", ""), cfg["extract"], category_key)
    frm = ((msg.get("from") or {}).get("emailAddress") or {}).get("address", "")
    return {
        "status": "found",
        "new_refresh": new_refresh,
        "message": {
            "code": parsed["code"],
            "link": parsed["link"],
            "snippet": parsed["snippet"],
            "country": parsed["country"],
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


def _fetch_via_gmail_imap(email_norm: str, cfg: dict, category_key: str) -> dict:
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
                parsed = parse_netflix_email(subject, body_text, body_html, cfg["extract"], category_key)
                M.logout()
                return {
                    "status": "found",
                    "message": {
                        "code": parsed["code"],
                        "link": parsed["link"],
                        "snippet": parsed["snippet"],
                        "country": parsed["country"],
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
        return await _fetch_via_graph(mailbox, cfg, category_key)
    return _fetch_via_gmail_imap(email_norm, cfg, category_key)
