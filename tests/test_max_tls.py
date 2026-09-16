"""The MAX client's own TLS trust: verified always, scoped to MAX, never proxied.

Found in the Stage C0 read-only production check: platform-api2.max.ru is
reachable directly, but its certificate chains to the Russian Trusted Root CA,
so the default trust store refuses it and the Stage B client could not have
connected. Reproduced here with a private CA and a fake MAX over HTTPS.
"""

import ssl

import pytest

from apps.customer_requests.max_api import (
    MaxBotApi,
    MaxNetworkError,
    ca_fingerprints,
    tls_context,
)

from .max_fake import FAKE_MAX_TOKEN, FakeMaxServer
from .max_tls import make_local_ca


@pytest.fixture
def local_ca(tmp_path):
    return make_local_ca(tmp_path / "ca")


@pytest.fixture
def https_max(local_ca):
    fake = FakeMaxServer()
    fake.start(tls_context=local_ca.server_context)
    yield fake
    fake.stop()


def test_the_default_trust_store_refuses_a_ca_it_does_not_know(https_max):
    client = MaxBotApi(FAKE_MAX_TOKEN, base_url=https_max.base_url, timeout=3)
    with pytest.raises(MaxNetworkError) as caught:
        client.get_me()
    assert caught.value.ambiguous is False
    assert "SSLCertVerificationError" in str(caught.value)
    assert https_max.requests == []  # nothing was sent before the handshake failed


def test_the_max_ca_file_lets_only_the_max_client_connect(https_max, local_ca):
    client = MaxBotApi(
        FAKE_MAX_TOKEN, base_url=https_max.base_url, timeout=3, ca_file=str(local_ca.ca_file)
    )
    assert client.get_me()["is_bot"] is True
    assert https_max.base_url.startswith("https://")
    # The process-wide default trust is unchanged: a plain client still refuses.
    with pytest.raises(MaxNetworkError):
        MaxBotApi(FAKE_MAX_TOKEN, base_url=https_max.base_url, timeout=3).get_me()


def test_the_pinned_fingerprint_is_enforced(https_max, local_ca, tmp_path):
    pinned = MaxBotApi(
        FAKE_MAX_TOKEN,
        base_url=https_max.base_url,
        timeout=3,
        ca_file=str(local_ca.ca_file),
        ca_sha256=":".join(
            local_ca.ca_sha256[i:i + 2] for i in range(0, 64, 2)
        ).upper(),
    )
    assert pinned.get_me()["username"]
    with pytest.raises(ValueError, match="fingerprint"):
        MaxBotApi(
            FAKE_MAX_TOKEN, base_url=https_max.base_url,
            ca_file=str(local_ca.ca_file), ca_sha256="0" * 64,
        )
    assert ca_fingerprints(str(local_ca.ca_file)) == [local_ca.ca_sha256]


def test_a_different_ca_is_not_trusted(https_max, tmp_path):
    other = make_local_ca(tmp_path / "other", label="Some Other Root")
    client = MaxBotApi(
        FAKE_MAX_TOKEN, base_url=https_max.base_url, timeout=3, ca_file=str(other.ca_file)
    )
    with pytest.raises(MaxNetworkError, match="SSLCertVerificationError"):
        client.get_me()


BROKEN_PEM = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"


@pytest.mark.parametrize("content", [None, "", "not a certificate", BROKEN_PEM])
def test_an_unusable_ca_file_fails_closed_before_any_request(tmp_path, content):
    path = tmp_path / "ca.pem"
    if content is not None:
        path.write_text(content)
    with pytest.raises(ValueError, match="MAX CA"):
        MaxBotApi(FAKE_MAX_TOKEN, base_url="https://127.0.0.1:9", ca_file=str(path))


def test_a_fingerprint_without_a_file_is_refused():
    with pytest.raises(ValueError, match="without a MAX CA file"):
        tls_context("", "ab" * 32)


def test_verification_and_hostname_checks_stay_on(local_ca):
    context = tls_context(str(local_ca.ca_file))
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_environment_proxies_are_never_used(https_max, local_ca, monkeypatch):
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")  # nothing listens there
    client = MaxBotApi(
        FAKE_MAX_TOKEN, base_url=https_max.base_url, timeout=3, ca_file=str(local_ca.ca_file)
    )
    assert client.get_me()["user_id"]
