"""
Auth module. exchange_auth_code_for_token() now calls Fyers' validate-
authcode endpoint DIRECTLY via requests, matching the official docs'
curl example exactly — rather than going through fyers_apiv3's
SessionModel.generate_token(), which has a known fragility: it calls
response.json() without checking the HTTP status first, and crashes
with an opaque "Expecting value: line 1 column 1 (char 0)" whenever
Fyers returns a non-200 response with an empty body (confirmed via
multiple independent community reports of this exact SDK bug).

Calling the endpoint directly means we see the REAL status code and
response body when something goes wrong, instead of a swallowed crash.
"""
import hashlib
import requests
from fyers_apiv3 import fyersModel
from config.settings import settings

VALIDATE_AUTHCODE_URL = "https://api-t1.fyers.in/api/v3/validate-authcode"


def generate_auth_url() -> str:
    session = fyersModel.SessionModel(
        client_id=settings.fyers_client_id,
        redirect_uri=settings.fyers_redirect_uri,
        response_type="code",
        state="sample_state",
    )
    return session.generate_authcode()


def exchange_auth_code_for_token(auth_code: str) -> str:
    app_id_hash = hashlib.sha256(
        f"{settings.fyers_client_id}:{settings.fyers_secret_key}".encode()
    ).hexdigest()

    payload = {
        "grant_type": "authorization_code",
        "appIdHash": app_id_hash,
        "code": auth_code,
    }

    try:
        response = requests.post(VALIDATE_AUTHCODE_URL, json=payload, timeout=15)
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Network error reaching Fyers auth endpoint: {e}")

    if not response.text or not response.text.strip():
        raise RuntimeError(
            f"Fyers returned an EMPTY response body (HTTP {response.status_code}). "
            f"This usually means the auth_code was already used/expired, or Fyers "
            f"had a transient issue. Try logging in again for a fresh code."
        )

    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(
            f"Fyers returned a non-JSON response (HTTP {response.status_code}): "
            f"{response.text[:300]}"
        )

    if data.get("s") != "ok" or "access_token" not in data:
        raise RuntimeError(f"Token exchange failed (HTTP {response.status_code}): {data}")

    return data["access_token"]


def get_fyers_model(access_token: str):
    return fyersModel.FyersModel(
        client_id=settings.fyers_client_id,
        token=access_token,
        is_async=False,
        log_path="",
    )
