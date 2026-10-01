import hashlib

import pytest

from api.internal_auth import is_internal_caller

TOKEN = "s3cret-internal-token"


@pytest.fixture
def production(monkeypatch):
    monkeypatch.setenv("WAYPOINT_ENV", "production")
    monkeypatch.delenv("WAYPOINT_INTERNAL_TOKEN", raising=False)
    monkeypatch.setenv("WAYPOINT_INTERNAL_TOKEN_SHA256", hashlib.sha256(TOKEN.encode()).hexdigest())


def test_the_token_whose_digest_is_configured_is_accepted(production):
    assert is_internal_caller(TOKEN)


@pytest.mark.parametrize("presented", [None, "", "wrong", TOKEN + " "])
def test_anything_else_is_refused(production, presented):
    assert not is_internal_caller(presented)


def test_the_digest_itself_is_not_a_credential(production, monkeypatch):
    # Whoever can read the environment holds the digest; presenting it must fail.
    assert not is_internal_caller(hashlib.sha256(TOKEN.encode()).hexdigest())


def test_a_plaintext_token_in_the_environment_is_not_consulted(monkeypatch):
    monkeypatch.setenv("WAYPOINT_ENV", "production")
    monkeypatch.delenv("WAYPOINT_INTERNAL_TOKEN_SHA256", raising=False)
    monkeypatch.setenv("WAYPOINT_INTERNAL_TOKEN", TOKEN)
    assert not is_internal_caller(TOKEN)


def test_local_mode_is_open(monkeypatch):
    monkeypatch.setenv("WAYPOINT_ENV", "local")
    assert is_internal_caller(None)
