"""The JSON auth surface the React storefront talks to.

Two things here are contracts with a client that cannot be seen from the server,
which is why they get tests rather than trust:

  * **401, never 303.** `fetch` follows a redirect transparently and without
    opting in, so an endpoint that answers an anonymous caller with 303 hands the
    client the login page's HTML under a 200. The failure then surfaces as a JSON
    parse error somewhere unrelated - or not at all, if the caller only checks
    `res.ok`. Nothing in `recs.py` stops a future edit from swapping `api_user`
    back to `current_user`, and nothing about that edit would look wrong.

  * **One set of rules behind two front doors.** The form handlers and the
    `/api/auth/*` handlers share `_registration_error` / `_authenticate`. If they
    ever grew separate copies, the looser one would be the one that mattered - so
    the parity tests below drive *both* paths with the same input and compare.

Runs without lifespan on purpose: `TestClient(app)` outside a `with` block never
starts the event consumer or the scheduler, and none of these endpoints need
them.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.models import User
from app.routers.auth import BAD_CREDENTIALS, MIN_PASSWORD_LENGTH
from app.security import SESSION_COOKIE, hash_password

EMAIL = "shopper@bazario.test"
PASSWORD = "correct-horse-battery"

# Hashed once for the whole module. bcrypt at 12 rounds costs ~0.25s a call, and
# a fixture that re-hashed per test would put more time into key stretching than
# into everything else in the suite combined.
PASSWORD_HASH = hash_password(PASSWORD)


@pytest.fixture()
def db():
    # StaticPool + check_same_thread, unlike the other suites' plain
    # `create_engine("sqlite://")`. TestClient runs the app on a worker thread,
    # and the default pool for an in-memory SQLite hands out a *connection per
    # thread* - so the request would land on a second, empty database and every
    # query would fail with "no such table". One shared connection instead.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def client(db, monkeypatch):
    """A client whose requests hit `db`, with the session cookie readable.

    `set_session_cookie` sets `secure=not settings.debug`, and a Secure cookie is
    never sent back over `http://testserver` - so with DEBUG=false in the
    environment every signed-in assertion below would fail for a reason that has
    nothing to do with the code under test. Pinned rather than inherited.
    """
    monkeypatch.setattr(settings, "debug", True)
    app.dependency_overrides[get_db] = lambda: db
    try:
        # Redirects off: several tests here are *about* the status code, and
        # following one would hide it behind the page it landed on. Not a `with`
        # block, so no lifespan - see the module docstring.
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def registered(db) -> User:
    user = User(email=EMAIL, password_hash=PASSWORD_HASH, name="Shopper", role="user")
    db.add(user)
    db.commit()
    return user


# --------------------------------------------------------------------------
# 401, not 303 - the whole reason these endpoints changed
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/recommendations"),
        ("GET", "/api/me/signal"),
        ("POST", "/api/recommendations/refresh"),
    ],
)
def test_a_json_endpoint_answers_an_anonymous_caller_with_401(client, method, path):
    response = client.request(method, path)

    assert response.status_code == 401
    assert "Location" not in response.headers, "a redirect a fetch client cannot use"
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "login required"}


def test_following_redirects_still_never_yields_html(db, monkeypatch):
    """The state the bug actually produced. `fetch` follows redirects by default,
    so the client's view of a 303 is a 200 carrying the login page - which is why
    asserting on the un-followed status alone would not have caught it."""
    monkeypatch.setattr(settings, "debug", True)
    app.dependency_overrides[get_db] = lambda: db
    try:
        following = TestClient(app)  # follow_redirects defaults to True
        response = following.get("/api/recommendations")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 401
    assert "text/html" not in response.headers["content-type"]


def test_a_server_rendered_page_still_redirects(client):
    """`api_user` was added alongside `current_user` rather than replacing it.

    `/inbox` is the example rather than `/dashboard` because the Step 3 conversion
    turned the customer pages into JSON - `/inbox` is now the customer-facing page
    that is still deliberately Jinja, since it serves the real HTML the mailer
    produced. A browser navigation there still wants the redirect, so this is the
    half of the auth change that must not have moved.
    """
    response = client.get("/inbox")

    assert response.status_code == 303
    assert response.headers["Location"] == "/login"


def test_a_valid_session_gets_through(client, registered):
    """The 401 has to be about the missing session and nothing else - a
    dependency that rejected everyone would pass every test above."""
    client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})

    response = client.get("/api/me/signal")

    assert response.status_code == 200
    assert response.json() == {"events": []}


# --------------------------------------------------------------------------
# GET /api/auth/me
# --------------------------------------------------------------------------
def test_me_reports_anonymous_as_a_success(client):
    """Anonymous is a normal state, not an error: the header asks this on every
    cold load, and a 401 would put a red line in the console on each one."""
    response = client.get("/api/auth/me")

    assert response.status_code == 200
    assert response.json() == {"user": None}


def test_me_reports_the_signed_in_account(client, registered):
    client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})

    body = client.get("/api/auth/me").json()

    assert body["user"]["email"] == EMAIL
    assert body["user"]["name"] == "Shopper"
    assert body["user"]["is_admin"] is False


def test_the_user_payload_cannot_widen_by_accident(client, registered):
    """`PublicUser` is built field by field rather than from the ORM object, so
    adding a column to `User` cannot leak it. This pins the field list, because
    the column that would matter is `password_hash`."""
    response = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})

    assert set(response.json()["user"]) == {"id", "email", "name", "role", "is_admin"}
    assert PASSWORD_HASH not in response.text
    assert PASSWORD not in response.text


# --------------------------------------------------------------------------
# POST /api/auth/login
# --------------------------------------------------------------------------
def test_login_issues_the_same_session_cookie_the_form_does(client, registered):
    response = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})

    assert response.status_code == 200
    assert SESSION_COOKIE in response.cookies
    # httponly matters here specifically because the client is JavaScript: the
    # React app must never be able to read the session out of document.cookie.
    assert "httponly" in response.headers["set-cookie"].lower()


def test_login_normalises_the_email(client, registered):
    response = client.post(
        "/api/auth/login", json={"email": f"  {EMAIL.upper()}  ", "password": PASSWORD}
    )

    assert response.status_code == 200


def test_a_wrong_password_is_401_and_sets_no_cookie(client, registered):
    response = client.post("/api/auth/login", json={"email": EMAIL, "password": "wrong-password"})

    assert response.status_code == 401
    assert SESSION_COOKIE not in response.cookies
    assert client.get("/api/auth/me").json() == {"user": None}


def test_login_does_not_reveal_which_emails_are_registered(client, registered):
    """One message for both failure modes. Two would turn the login form into a
    lookup service for who has an account here."""
    wrong_password = client.post("/api/auth/login", json={"email": EMAIL, "password": "nope1234"})
    no_such_account = client.post(
        "/api/auth/login", json={"email": "stranger@bazario.test", "password": "nope1234"}
    )

    assert wrong_password.status_code == no_such_account.status_code == 401
    assert wrong_password.json() == no_such_account.json() == {"detail": BAD_CREDENTIALS}


# --------------------------------------------------------------------------
# POST /api/auth/register
# --------------------------------------------------------------------------
def test_register_creates_the_account_and_signs_it_in(client, db):
    response = client.post(
        "/api/auth/register",
        json={"email": "new@bazario.test", "password": "a-long-enough-one", "name": "New Shopper"},
    )

    assert response.status_code == 201
    assert response.json()["user"]["name"] == "New Shopper"
    assert db.query(User).filter(User.email == "new@bazario.test").count() == 1
    # Signed in by the same response, so the client never has to post twice.
    assert client.get("/api/auth/me").json()["user"]["email"] == "new@bazario.test"


def test_a_missing_name_falls_back_to_the_email_local_part(client):
    response = client.post(
        "/api/auth/register", json={"email": "quiet@bazario.test", "password": "a-long-enough-one"}
    )

    assert response.json()["user"]["name"] == "quiet"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"email": "not-an-email", "password": "a-long-enough-one"}, "valid email"),
        ({"email": "short@bazario.test", "password": "tiny"}, str(MIN_PASSWORD_LENGTH)),
    ],
)
def test_register_rejects_bad_input_with_400_and_a_showable_message(client, payload, expected):
    """400, not 422: these are the app's own rules rather than a malformed body,
    and the client renders `detail` to the user as-is."""
    response = client.post("/api/auth/register", json=payload)

    assert response.status_code == 400
    assert expected in response.json()["detail"]


def test_register_rejects_an_email_that_is_already_taken(client, registered):
    response = client.post(
        "/api/auth/register", json={"email": EMAIL.upper(), "password": "a-long-enough-one"}
    )

    assert response.status_code == 400
    assert "already registered" in response.json()["detail"]


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "not-an-email", "password": "a-long-enough-one"},
        {"email": "short@bazario.test", "password": "tiny"},
        {"email": EMAIL, "password": "a-long-enough-one"},  # already taken
    ],
)
def test_both_front_doors_enforce_the_same_registration_rules(client, registered, payload):
    """Drives the JSON and form paths with identical input and compares, rather
    than restating the messages here. A test that hard-coded the strings would
    still pass if the two paths validated differently."""
    json_detail = client.post("/api/auth/register", json=payload).json()["detail"]
    form = client.post("/register", data=payload)

    assert form.status_code == 400
    assert json_detail in form.text, "the form path disagrees with the JSON path"


def test_the_check_order_is_the_same_on_both_paths(client, registered):
    """A registration can fail several rules at once. Which message comes back
    first is what the user sees, so it is part of the shared behaviour - not an
    incidental detail of whichever handler they went through."""
    payload = {"email": "not-an-email", "password": "tiny"}

    json_detail = client.post("/api/auth/register", json=payload).json()["detail"]

    assert "valid email" in json_detail
    assert json_detail in client.post("/register", data=payload).text


# --------------------------------------------------------------------------
# Where the *form* path lands
# --------------------------------------------------------------------------
def test_a_form_login_lands_on_a_route_that_exists(client, registered):
    """The form handlers used to redirect to `/dashboard`, which Step 3 turned
    into JSON and then removed - so signing in through this form 303'd onto a 404,
    and nothing in the suite noticed. This asserts the target resolves, rather
    than restating a path: a hard-coded string would rot the same way.
    """
    response = client.post("/login", data={"email": EMAIL, "password": PASSWORD})

    assert response.status_code == 303
    target = response.headers["Location"]
    # A shopper has no server-rendered page to land on any more, so the honest
    # destination is the storefront on its own origin.
    assert target == settings.storefront_url
    assert not target.startswith("/"), "an absolute origin, not a path on the API"


def test_an_admin_form_login_lands_in_the_panel(client, db):
    """The one account that *does* have server-rendered pages to come back to."""
    db.add(User(email="ada@bazario.test", password_hash=PASSWORD_HASH, role="admin"))
    db.commit()

    response = client.post("/login", data={"email": "ada@bazario.test", "password": PASSWORD})

    assert response.headers["Location"] == "/admin"
    assert client.get("/admin").status_code == 200, "the target has to actually serve"


@pytest.mark.parametrize(
    ("next_value", "expected"),
    [
        ("/admin/sync", "/admin/sync"),  # an in-site path is honoured
        ("//evil.example/phish", "/admin"),  # protocol-relative: discarded
        ("https://evil.example", "/admin"),  # absolute: discarded
        ("", "/admin"),  # absent: the role decides
    ],
)
def test_next_is_honoured_only_when_it_stays_on_this_site(client, db, next_value, expected):
    """An open redirect off a login page is a ready-made phishing vector, and
    `//host` is the spelling that gets missed - it is not absolute by the usual
    string test but a browser treats it as one."""
    db.add(User(email="ada@bazario.test", password_hash=PASSWORD_HASH, role="admin"))
    db.commit()

    response = client.post(
        "/login",
        data={"email": "ada@bazario.test", "password": PASSWORD, "next": next_value},
    )

    assert response.headers["Location"] == expected


# --------------------------------------------------------------------------
# POST /api/auth/logout
# --------------------------------------------------------------------------
def test_logout_clears_the_session(client, registered):
    client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})

    response = client.post("/api/auth/logout")

    assert response.status_code == 200
    assert response.json() == {"user": None}
    assert client.get("/api/auth/me").json() == {"user": None}
    assert client.get("/api/me/signal").status_code == 401


def test_logging_out_without_a_session_is_a_success(client):
    """A client whose cookie already expired is trying to reach exactly the state
    this endpoint produces. Answering 401 would make it retry or show an error
    for having succeeded."""
    response = client.post("/api/auth/logout")

    assert response.status_code == 200
    assert response.json() == {"user": None}
