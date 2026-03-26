"""
bunq_client.py
--------------
Handles all communication with the bunq API:
  - One-time setup: Installation + Device registration
  - Per-run:        Session creation and authenticated GET requests
  - Persistent:     Tokens are stored in config to survive restarts
"""

import json
import uuid
import logging
import urllib.parse
import requests
from pathlib import Path
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.backends import default_backend

logger = logging.getLogger(__name__)

BASE_URL = "https://api.bunq.com/v1"


# ---------------------------------------------------------------------------
# RSA Key helpers
# ---------------------------------------------------------------------------

def generate_rsa_keypair() -> tuple[str, str]:
    """Generate a new 2048-bit RSA key pair.

    Returns:
        (private_key_pem, public_key_pem) as PEM strings
    """
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode()

    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()

    return private_pem, public_pem


def sign_payload(payload: str, private_key_pem: str) -> str:
    """Sign a request payload with the private key (required for POST calls).

    Args:
        payload:         JSON string to sign
        private_key_pem: PEM-encoded private key

    Returns:
        Base64-encoded signature string
    """
    import base64
    private_key = serialization.load_pem_private_key(
        private_key_pem.encode(),
        password=None,
        backend=default_backend()
    )
    signature = private_key.sign(
        payload.encode(),
        padding.PKCS1v15(),
        hashes.SHA256()
    )
    return base64.b64encode(signature).decode()


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _default_headers(auth_token: str = None) -> dict:
    """Build the standard bunq request headers.

    Args:
        auth_token: Installation token (setup calls) or Session token (data calls)
    """
    headers = {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "User-Agent": "bunq-firefly-sync/1.0",
        "X-Bunq-Language": "en_US",
        "X-Bunq-Region": "en_US",
        "X-Bunq-Geolocation": "0 0 0 0 000",
        "X-Bunq-Client-Request-Id": str(uuid.uuid4()),
    }
    if auth_token:
        headers["X-Bunq-Client-Authentication"] = auth_token
    return headers


def _post(endpoint: str, payload: dict, auth_token: str = None,
          private_key_pem: str = None) -> dict:
    """Perform a signed POST request to the bunq API.

    Args:
        endpoint:        API path, e.g. '/installation'
        payload:         Request body as dict
        auth_token:      Token for the X-Bunq-Client-Authentication header
        private_key_pem: Private key for signing the payload (required by bunq)
    """
    url = f"{BASE_URL}{endpoint}"
    body = json.dumps(payload, separators=(",", ":"))
    headers = _default_headers(auth_token)

    # All POST requests must include a payload signature
    if private_key_pem:
        headers["X-Bunq-Client-Signature"] = sign_payload(body, private_key_pem)

    logger.debug(f"POST {url}")
    response = requests.post(url, headers=headers, data=body, timeout=30)
    response.raise_for_status()
    return response.json()


def _get(endpoint: str, session_token: str, params: dict = None) -> dict:
    """Perform an authenticated GET request to the bunq API.

    Args:
        endpoint:      API path, e.g. '/user/123/monetary-account'
        session_token: Active session token from the last session-server call
        params:        Optional query parameters (used for pagination)
    """
    url = f"{BASE_URL}{endpoint}"
    logger.debug(f"GET {url} params={params}")
    response = requests.get(
        url,
        headers=_default_headers(session_token),
        params=params,
        timeout=30
    )
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# One-time setup (Installation + Device registration)
# ---------------------------------------------------------------------------

def setup_installation(config: dict) -> dict:
    """Register the installation with bunq (runs only once ever).

    This call:
      1. Generates a new RSA key pair
      2. POSTs the public key to bunq
      3. Stores the returned installation_token and server_public_key in config

    Args:
        config: Loaded config dict (will be mutated and returned)

    Returns:
        Updated config dict with installation_token, server_public_key,
        and private_key populated.
    """
    logger.info("Running one-time installation setup...")
    private_pem, public_pem = generate_rsa_keypair()

    response = _post("/installation", {"client_public_key": public_pem})

    # Extract the installation token and server public key from the response
    items = response["Response"]
    installation_token = next(
        i["Token"]["token"] for i in items if "Token" in i
    )
    server_public_key = next(
        i["ServerPublicKey"]["server_public_key"] for i in items
        if "ServerPublicKey" in i
    )

    # Persist all three values — they are needed for every future session
    config["bunq"]["installation_token"] = installation_token
    config["bunq"]["server_public_key"] = server_public_key
    config["bunq"]["private_key"] = private_pem

    logger.info("Installation successful.")
    return config


def setup_device(config: dict) -> dict:
    """Register this server as a bunq device (runs only once ever).

    Must be called after setup_installation(). Uses the installation token
    and API key to register the device. Stores the resulting device_token.

    Args:
        config: Config dict containing installation_token and api_key

    Returns:
        Updated config dict with device_token populated.
    """
    logger.info("Registering device with bunq...")

    installation_token = config["bunq"]["installation_token"]
    private_key = config["bunq"].get("private_key")
    api_key = config["bunq"]["api_key"]

    payload = {
        "description": config["bunq"]["device_description"],
        "secret": api_key,
        # Empty list = use wildcard (works only with "Allow All IPs" key in bunq app)
        "permitted_ips": ["*"]
    }

    response = _post(
        "/device-server",
        payload,
        auth_token=installation_token,
        private_key_pem=private_key
    )

    # The device-server response only returns an ID, not a separate token.
    # The installation token is reused until a session is created.
    device_id = response["Response"][0]["Id"]["id"]
    config["bunq"]["device_token"] = str(device_id)

    logger.info(f"Device registered (ID: {device_id}).")
    return config


# ---------------------------------------------------------------------------
# Per-run: Session management
# ---------------------------------------------------------------------------

def create_session(config: dict) -> tuple[str, int]:
    """Start a new API session (called at the beginning of every sync run).

    Uses the stored API key and installation token to obtain a fresh
    session token and the user ID.

    Args:
        config: Config dict with installation_token, api_key, private_key

    Returns:
        (session_token, user_id) tuple
    """
    logger.info("Creating new bunq session...")

    installation_token = config["bunq"]["installation_token"]
    private_key = config["bunq"].get("private_key")
    api_key = config["bunq"]["api_key"]

    response = _post(
        "/session-server",
        {"secret": api_key},
        auth_token=installation_token,
        private_key_pem=private_key
    )

    items = response["Response"]

    # Session token is used for all subsequent data requests
    session_token = next(i["Token"]["token"] for i in items if "Token" in i)

    # Extract the correct user ID (UserPerson or UserCompany)
    user_id = None
    for item in items:
        for key in ("UserPerson", "UserCompany", "UserApiKey"):
            if key in item:
                user_id = item[key]["id"]
                break

    if not user_id:
        raise RuntimeError("Could not determine user ID from session response.")

    logger.info(f"Session created for user_id={user_id}.")
    return session_token, user_id


# ---------------------------------------------------------------------------
# Data retrieval
# ---------------------------------------------------------------------------

def get_monetary_accounts(session_token: str, user_id: int) -> list[dict]:
    """Fetch all active monetary accounts for the authenticated user.

    Uses pagination to ensure all accounts are returned — bunq defaults
    to a small page size and may not return all accounts in one response.

    Args:
        session_token: Active session token
        user_id:       bunq user ID

    Returns:
        List of account dicts with keys: id, description, iban, balance, currency
    """
    accounts = []
    params   = {"count": 100}

    while True:
        response = _get(f"/user/{user_id}/monetary-account", session_token, params=params)
        items    = response.get("Response", [])

        if not items:
            break

        for item in items:
            # bunq returns a polymorphic wrapper — the key indicates account type
            for account_type in ("MonetaryAccountBank", "MonetaryAccountSavings",
                                 "MonetaryAccountJoint"):
                if account_type not in item:
                    continue
                acc = item[account_type]

                # Only include active accounts
                if acc.get("status") != "ACTIVE":
                    continue

                # Find the IBAN alias if available
                iban = next(
                    (a["value"] for a in acc.get("alias", []) if a["type"] == "IBAN"),
                    None
                )

                accounts.append({
                    "id":          acc["id"],
                    "description": acc.get("description", ""),
                    "iban":        iban,
                    "balance":     acc.get("balance", {}).get("value"),
                    "currency":    acc.get("balance", {}).get("currency"),
                    "type":        account_type,
                })

        # Follow pagination if more pages exist
        pagination = response.get("Pagination", {})
        older_url  = pagination.get("older_url")
        if not older_url:
            break

        parsed = urllib.parse.urlparse(older_url)
        qs     = urllib.parse.parse_qs(parsed.query)
        if "older_id" in qs:
            params = {"count": 100, "older_id": qs["older_id"][0]}
        else:
            break

    logger.info(f"Found {len(accounts)} active account(s).")
    return accounts


def get_payments(session_token: str, user_id: int, account_id: int,
                 newer_than_id: int = None, count: int = 200,
                 since_date: str = None) -> list[dict]:
    """Fetch payments for a monetary account, with optional cursor-based pagination.

    bunq returns payments in reverse chronological order (newest first).
    Use newer_than_id for incremental sync, or since_date to limit history.

    Args:
        session_token:  Active session token
        user_id:        bunq user ID
        account_id:     Monetary account ID to query
        newer_than_id:  Only return payments with ID > this value (incremental sync)
        count:          Max results per page (bunq max: 200)
        since_date:     Only return payments on or after this date (format: YYYY-MM-DD).
                        Pagination stops as soon as an older payment is encountered.

    Returns:
        List of raw payment dicts from the bunq API
    """
    from datetime import datetime

    endpoint = f"/user/{user_id}/monetary-account/{account_id}/payment"
    params = {"count": count}

    if newer_than_id:
        params["newer_id"] = newer_than_id

    # Parse the cutoff date once for fast comparison in the loop
    cutoff = None
    if since_date:
        cutoff = datetime.strptime(since_date, "%Y-%m-%d").date()

    all_payments = []

    while True:
        response = _get(endpoint, session_token, params=params)
        batch = response.get("Response", [])

        if not batch:
            break

        stop_pagination = False
        for p in batch:
            if "Payment" not in p:
                continue
            payment = p["Payment"]

            # Apply date filter — bunq returns newest first, so once we hit
            # a payment older than the cutoff we can stop fetching more pages
            if cutoff:
                created_str = payment.get("created", "")[:10]  # "YYYY-MM-DD"
                try:
                    payment_date = datetime.strptime(created_str, "%Y-%m-%d").date()
                except ValueError:
                    payment_date = None

                if payment_date and payment_date < cutoff:
                    stop_pagination = True  # All further pages will be older — stop
                    break                   # Skip this payment and all older ones

            all_payments.append(payment)

        if stop_pagination:
            break

        # Check if there are more (older) pages
        pagination = response.get("Pagination", {})
        older_url  = pagination.get("older_url")

        if not older_url:
            break

        parsed = urllib.parse.urlparse(older_url)
        qs = urllib.parse.parse_qs(parsed.query)
        if "older_id" in qs:
            params = {"count": count, "older_id": qs["older_id"][0]}
            # For incremental sync: also carry newer_id so bunq keeps the
            # upper bound and we don't re-fetch already-known payments
            if newer_than_id:
                params["newer_id"] = newer_than_id
        else:
            break

    logger.info(
        f"Fetched {len(all_payments)} payment(s) from account {account_id}"
        + (f" since {since_date}" if since_date else "")
        + "."
    )
    return all_payments
