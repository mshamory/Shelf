"""Optional sign-in: a password (SHELF_PASSWORD), a header set by a login proxy in front of
Shelf (SHELF_AUTH_HEADER), or both. With neither set, Shelf is open, as before.

- In a browser, the password signs you in through /login, which sets a signed cookie.
- Apps that can't show a login page (e-reader apps reading the OPDS catalog, the browser
  extension) send the password with HTTP Basic auth, any user name. Basic auth is accepted
  only for the catalog, its downloads and the extension's API: a browser that remembers
  Basic credentials sends them with every request, even one another site starts, so
  accepting them for the app's forms would open those to cross-site requests.
- A login proxy (Authelia, Authentik, oauth2-proxy...) signs you in itself and passes your
  name in a header. Shelf believes that header only from SHELF_TRUSTED_PROXIES, the
  addresses the proxy connects from; anyone else could set it too.

SHELF_TRUSTED_PROXIES is also whose X-Forwarded-For/-Proto Shelf believes, so the app
knows your address and whether you used https."""

from __future__ import annotations

import base64
import binascii
import functools
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import threading
import time
from http.cookies import SimpleCookie
from urllib.parse import quote

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from .config import settings

log = logging.getLogger("shelf.auth")

COOKIE = "shelf_login"
COOKIE_AGE = 400 * 24 * 3600  # browsers keep cookies at most 400 days
# Open to anyone: what the login page and an installed app's icon need before you sign in.
OPEN_PATHS = re.compile(r"^/(healthz|login|static/.*|manifest\.webmanifest|sw\.js)$")
# Where Basic auth is accepted: the OPDS catalog, what it links to, the extension's API.
BASIC_PATHS = re.compile(r"^/(opds(/.*)?|novel/\d+/export\.epub|covers/[^/]+|api/.*)$")
# ...and of those, where a missing password gets a Basic challenge (the extension's API
# answers 401 without one, so browsers don't pop up a password dialog over it).
CHALLENGE_PATHS = re.compile(r"^/(opds(/.*)?|novel/\d+/export\.epub|covers/[^/]+)$")

MAX_FAILURES = 10  # wrong passwords from one address...
FAILURE_WINDOW = 15 * 60  # ...within this many seconds, then it must wait


def enabled() -> bool:
    return bool(password() or settings.auth_header)


def password() -> str:
    return settings.password.get_secret_value() if settings.password else ""


def check_password(given: str) -> bool:
    expected = password()
    return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())


# -- the login cookie ---------------------------------------------------------------------


@functools.lru_cache(maxsize=4)
def _key(secret_path: str, pw: str) -> bytes:
    """Signs login cookies. It includes the password, so changing the password signs
    everyone out; deleting the secret file does too."""
    from pathlib import Path

    path = Path(secret_path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "xb") as f:
                path.chmod(0o600)
                f.write(secrets.token_bytes(32))
        except FileExistsError:
            pass  # another worker just wrote one: use that
    secret = path.read_bytes()
    return hashlib.sha256(secret + b"\0" + pw.encode()).digest()


def _sign(value: str) -> str:
    key = _key(str(settings.data_dir / "secret_key"), password())
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def login_cookie() -> str:
    issued = str(int(time.time()))
    return f"{issued}.{_sign(issued)}"


def _valid_cookie(value: str) -> bool:
    issued, _, sig = value.partition(".")
    if not issued.isdigit() or time.time() - int(issued) > COOKIE_AGE:
        return False
    return hmac.compare_digest(sig, _sign(issued))


# -- wrong passwords ----------------------------------------------------------------------

_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()


def too_many_failures(addr: str) -> bool:
    now = time.monotonic()
    with _failures_lock:
        recent = [t for t in _failures.get(addr, []) if now - t < FAILURE_WINDOW]
        if recent:
            _failures[addr] = recent
        else:
            _failures.pop(addr, None)
        return len(recent) >= MAX_FAILURES


def note_failure(addr: str) -> None:
    with _failures_lock:
        _failures.setdefault(addr, []).append(time.monotonic())
    log.warning("Wrong Shelf password from %s", addr)


# -- trusted proxies ----------------------------------------------------------------------


@functools.lru_cache(maxsize=4)
def _trusted(setting: str):
    """(trust everyone, addresses, networks, other literals) from SHELF_TRUSTED_PROXIES."""
    items = [s.strip() for s in setting.split(",") if s.strip()]
    hosts, networks, literals = set(), [], set()
    for item in items:
        try:
            if "/" in item:
                networks.append(ipaddress.ip_network(item, strict=False))
            else:
                hosts.add(ipaddress.ip_address(item))
        except ValueError:
            literals.add(item)  # a unix socket, or the test client
    return "*" in items, hosts, networks, literals


def is_trusted(peer: str | None) -> bool:
    every, hosts, networks, literals = _trusted(settings.trusted_proxies)
    if every:
        return True
    if not peer:
        return False
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer in literals
    return ip in hosts or any(ip in n for n in networks)


@functools.lru_cache(maxsize=4)
def _forwarded(app, setting: str) -> ProxyHeadersMiddleware:
    return ProxyHeadersMiddleware(app, trusted_hosts=setting)


# -- the middleware -----------------------------------------------------------------------


class AuthMiddleware:
    """Lets a request through when it's signed in (or sign-in is off); otherwise sends the
    browser to /login, or answers 401. Then applies X-Forwarded-* from trusted proxies."""

    def __init__(self, app):
        self.app = app
        self._warned: set[str] = set()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        peer = scope["client"][0] if scope.get("client") else None
        forwarded = _forwarded(self.app, settings.trusted_proxies)
        state = scope.setdefault("state", {})
        state["auth"] = None
        if not enabled() or scope["method"] == "OPTIONS" or OPEN_PATHS.match(scope["path"]):
            return await forwarded(scope, receive, send)

        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope["headers"]}
        how = self._signed_in(scope, headers, peer)
        if how is True:
            return await send_response(send, 429, {"detail": "Too many wrong passwords. "
                                                   "Try again in a few minutes."})
        if how:
            state["auth"] = how
            return await forwarded(scope, receive, send)
        await self._refuse(scope, send, headers)

    def _signed_in(self, scope, headers: dict[str, str], peer: str | None):
        """("header", name), ("cookie", None) or ("basic", None) if signed in; True if this
        address has given too many wrong passwords; None if not signed in."""
        if settings.auth_header:
            name = headers.get(settings.auth_header.lower(), "").strip()
            if name and is_trusted(peer):
                return ("header", name)
            if name and peer not in self._warned:
                self._warned.add(peer)
                log.warning("Ignoring %s from %s: not in SHELF_TRUSTED_PROXIES",
                            settings.auth_header, peer)
        if not password():
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get("cookie", ""))
        except Exception:  # noqa: BLE001 (a malformed cookie header: not signed in)
            cookie = SimpleCookie()
        if COOKIE in cookie and _valid_cookie(cookie[COOKIE].value):
            return ("cookie", None)
        scheme, _, credentials = headers.get("authorization", "").partition(" ")
        if scheme.lower() == "basic" and BASIC_PATHS.match(scope["path"]):
            addr = peer or "?"
            if too_many_failures(addr):
                return True
            try:
                given = base64.b64decode(credentials, validate=True).decode()
            except (binascii.Error, UnicodeDecodeError):
                given = ""
            if check_password(given.partition(":")[2]):
                return ("basic", None)
            note_failure(addr)
        return None

    async def _refuse(self, scope, send, headers: dict[str, str]) -> None:
        path = scope["path"]
        if password() and scope["method"] == "GET" and not BASIC_PATHS.match(path) \
                and "text/html" in headers.get("accept", ""):
            target = path + ("?" + scope["query_string"].decode("latin-1")
                             if scope["query_string"] else "")
            root = scope.get("root_path", "")
            location = f"{root}/login" + ("" if target == "/" else "?next=" + quote(target))
            return await send_response(send, 303, None, [(b"location", location.encode())])
        extra = []
        if password() and CHALLENGE_PATHS.match(path):
            extra.append((b"www-authenticate", b'Basic realm="Shelf", charset="UTF-8"'))
        detail = ("Shelf needs its password." if password()
                  else "Sign in through your login proxy.")
        await send_response(send, 401, {"detail": detail}, extra)


async def send_response(send, status: int, body: dict | None, headers=()) -> None:
    data = json.dumps(body).encode() if body is not None else b""
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"application/json"), (b"content-length", str(len(data)).encode()),
        (b"cache-control", b"no-store"), *headers]})
    await send({"type": "http.response.body", "body": data})
