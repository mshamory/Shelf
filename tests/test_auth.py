"""Optional sign-in: a password, or a header from a login proxy."""

import base64

import pytest
from pydantic import SecretStr

from .test_app import client  # noqa: F401  (fixture)

HTML = {"Accept": "text/html"}


def basic(password: str, user: str = "reader") -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


@pytest.fixture
def with_password(client, monkeypatch):  # noqa: F811
    from shelf import auth
    from shelf.config import settings

    monkeypatch.setattr(settings, "password", SecretStr("open sesame"))
    monkeypatch.setattr(auth, "_failures", {})
    return client


def test_password(with_password):
    from shelf import auth

    c = with_password
    # Not signed in: browsers go to the login page, apps get a 401.
    r = c.get("/novel/3?hidden=1", headers=HTML, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/novel/3%3Fhidden%3D1"
    assert c.get("/", headers=HTML, follow_redirects=False).headers["location"] == "/login"
    r = c.get("/api/novels")
    assert r.status_code == 401 and "www-authenticate" not in r.headers
    assert r.json()["detail"] == "Shelf needs its password."
    r = c.get("/opds")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
    assert c.post("/scan").status_code == 401
    assert c.get("/healthz").status_code == 200
    assert c.get("/static/shelf.css").status_code == 200
    assert "Sign in" in c.get("/login").text

    # Signing in.
    r = c.post("/login", data={"password": "wrong", "next": "/"})
    assert r.status_code == 401 and "not the password" in r.text
    r = c.post("/login", data={"password": "open sesame", "next": "/?sort=title"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?sort=title"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    page = c.get("/", headers=HTML)
    assert page.status_code == 200 and "Sign out" in page.text
    assert c.post("/login", data={"password": "x", "next": "//evil.example"},
                  follow_redirects=False).status_code == 401

    # A changed password signs everyone out.
    good = c.cookies.get(auth.COOKIE)
    assert auth._valid_cookie(good)
    from shelf.config import settings
    settings.password = SecretStr("new one")
    assert c.get("/api/novels").status_code == 401
    settings.password = SecretStr("open sesame")
    assert c.get("/api/novels").status_code == 200

    c.post("/logout")
    assert c.get("/api/novels").status_code == 401


def test_basic_auth(with_password):
    c = with_password
    assert c.get("/opds", headers=basic("open sesame")).status_code == 200
    assert c.get("/api/novels", headers=basic("open sesame", user="")).status_code == 200
    assert c.get("/opds", headers=basic("wrong")).status_code == 401
    # Not for the app's own pages and forms: browsers send remembered Basic credentials
    # with requests other sites start.
    assert c.post("/scan", headers=basic("open sesame")).status_code == 401
    r = c.get("/", headers={**HTML, **basic("open sesame")}, follow_redirects=False)
    assert r.status_code == 303


def test_too_many_wrong_passwords(with_password):
    c = with_password
    for _ in range(10):
        assert c.get("/opds", headers=basic("guess")).status_code == 401
    assert c.get("/opds", headers=basic("open sesame")).status_code == 429
    assert c.post("/login", data={"password": "open sesame"}).status_code == 429


def test_proxy_header(client, monkeypatch):  # noqa: F811
    from shelf.config import settings

    monkeypatch.setattr(settings, "auth_header", "Remote-User")
    # The proxy isn't trusted yet: anyone could send the header.
    assert client.get("/api/novels", headers={"Remote-User": "alice"}).status_code == 401
    monkeypatch.setattr(settings, "trusted_proxies", "10.0.0.0/8, testclient")
    r = client.get("/", headers={**HTML, "Remote-User": "alice"})
    assert r.status_code == 200 and "Signed in as alice" in r.text
    r = client.get("/api/novels")
    assert r.status_code == 401 and r.json()["detail"] == "Sign in through your login proxy."
    # No login page without a password.
    assert client.get("/", headers=HTML, follow_redirects=False).status_code == 401
    # With a password too, either works.
    monkeypatch.setattr(settings, "password", SecretStr("pw"))
    assert client.get("/opds", headers=basic("pw")).status_code == 200
    assert client.get("/opds", headers={"Remote-User": "bob"}).status_code == 200


def test_forwarded_headers(client, monkeypatch):  # noqa: F811
    from shelf.config import settings

    fwd = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9"}
    assert 'href="http://testserver/opds"' in client.get("/opds", headers=fwd).text
    monkeypatch.setattr(settings, "trusted_proxies", "testclient")
    assert 'href="https://testserver/opds"' in client.get("/opds", headers=fwd).text


def test_trusted_addresses(monkeypatch):
    from shelf import auth
    from shelf.config import settings

    monkeypatch.setattr(settings, "trusted_proxies", "127.0.0.1, ::1, 172.16.0.0/12")
    assert auth.is_trusted("127.0.0.1") and auth.is_trusted("::1")
    assert auth.is_trusted("172.18.0.5") and not auth.is_trusted("192.168.1.2")
    assert not auth.is_trusted(None) and not auth.is_trusted("testclient")
    monkeypatch.setattr(settings, "trusted_proxies", "*")
    assert auth.is_trusted("8.8.8.8")
