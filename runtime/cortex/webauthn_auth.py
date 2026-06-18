"""WebAuthn step-up auth for PUBLIC approvals (see [[feedback_public_actions_biometric]]).

Rashad registers his phone once (a platform passkey); then approving anything that goes OUT to the public
(send an email, send a newsletter, publish a blog post) requires a fresh fingerprint / device-PIN. py_webauthn
does the crypto. Credentials + the in-flight challenge + a short-lived one-time step-up token live in settings.

The gate is only ENFORCED once a credential is registered (is_registered()), so enabling it can never lock the
operator out of the existing flows before they've set it up.
"""
from __future__ import annotations

import json
import secrets
import time

import webauthn
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                       ResidentKeyRequirement, UserVerificationRequirement)

from . import config, db

RP_ID = config.get("WEBAUTHN_RP_ID", "coretex.uk")
RP_NAME = "Cortex"
ORIGIN = config.get("WEBAUTHN_ORIGIN", "https://coretex.uk")
_USER_ID = b"cortex-operator"          # single operator (Rashad)
_STEPUP_TTL = 120                       # a verified fingerprint authorises an approve for this many seconds


def _creds() -> list[dict]:
    return db.setting_get("webauthn_credentials") or []


def is_registered() -> bool:
    return bool(_creds())


# ---------- registration (one-time, per device) ----------

def register_options() -> dict:
    opts = webauthn.generate_registration_options(
        rp_id=RP_ID, rp_name=RP_NAME, user_id=_USER_ID, user_name="Rashad",
        authenticator_selection=AuthenticatorSelectionCriteria(
            user_verification=UserVerificationRequirement.REQUIRED,
            resident_key=ResidentKeyRequirement.PREFERRED))
    db.setting_set("webauthn_reg_challenge", bytes_to_base64url(opts.challenge))
    return json.loads(webauthn.options_to_json(opts))


def register_verify(credential: dict) -> dict:
    chal = db.setting_get("webauthn_reg_challenge")
    if not chal:
        return {"ok": False, "error": "no registration challenge in flight"}
    try:
        v = webauthn.verify_registration_response(
            credential=json.dumps(credential), expected_challenge=base64url_to_bytes(chal),
            expected_rp_id=RP_ID, expected_origin=ORIGIN)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    creds = _creds()
    creds.append({"id": bytes_to_base64url(v.credential_id),
                  "public_key": bytes_to_base64url(v.credential_public_key),
                  "sign_count": v.sign_count})
    db.setting_set("webauthn_credentials", creds)
    db.setting_set("webauthn_reg_challenge", None)
    return {"ok": True, "registered": len(creds)}


# ---------- authentication (the fingerprint at each public approval) ----------

def auth_options() -> dict:
    opts = webauthn.generate_authentication_options(
        rp_id=RP_ID, user_verification=UserVerificationRequirement.REQUIRED,
        allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["id"])) for c in _creds()])
    db.setting_set("webauthn_auth_challenge", bytes_to_base64url(opts.challenge))
    return json.loads(webauthn.options_to_json(opts))


def auth_verify(credential: dict) -> dict:
    chal = db.setting_get("webauthn_auth_challenge")
    if not chal:
        return {"ok": False, "error": "no auth challenge in flight"}
    cid = credential.get("id") or credential.get("rawId")
    creds = _creds()
    cred = next((c for c in creds if c["id"] == cid), None)
    if not cred:
        return {"ok": False, "error": "unknown credential"}
    try:
        v = webauthn.verify_authentication_response(
            credential=json.dumps(credential), expected_challenge=base64url_to_bytes(chal),
            expected_rp_id=RP_ID, expected_origin=ORIGIN,
            credential_public_key=base64url_to_bytes(cred["public_key"]),
            credential_current_sign_count=cred.get("sign_count", 0))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    cred["sign_count"] = v.new_sign_count
    db.setting_set("webauthn_credentials", creds)
    db.setting_set("webauthn_auth_challenge", None)
    token = secrets.token_urlsafe(24)
    db.setting_set("webauthn_stepup", {"token": token, "exp": int(time.time()) + _STEPUP_TTL})
    return {"ok": True, "stepup_token": token}


def consume_stepup(token: str | None) -> bool:
    """A one-time, short-lived proof that a fingerprint was just verified. True iff valid (and consumes it)."""
    s = db.setting_get("webauthn_stepup")
    if not (s and token and s.get("token") == token and s.get("exp", 0) >= int(time.time())):
        return False
    db.setting_set("webauthn_stepup", None)
    return True
