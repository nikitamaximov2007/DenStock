"""A throwaway certificate authority for HTTPS tests of the MAX client.

Production MAX chains to the Russian Trusted Root CA, which standard trust
stores lack. These tests reproduce that situation locally: a private root CA
signs a certificate for 127.0.0.1, the default trust store refuses it, and
only a client given that CA file connects. Nothing here is a real key.
"""
from __future__ import annotations

import datetime
import ipaddress
import ssl
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


@dataclass
class LocalCa:
    ca_file: Path
    ca_sha256: str
    server_context: ssl.SSLContext


def _name(common_name: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "DenisStock test only"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )


def make_local_ca(directory: Path, *, label: str = "Fake Trusted Root CA") -> LocalCa:
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name(label))
        .issuer_name(_name(label))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_cert_sign=True, crl_sign=True,
                content_commitment=False, key_encipherment=False, data_encipherment=False,
                key_agreement=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("platform-api2.max.test"))
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    directory.mkdir(parents=True, exist_ok=True)
    ca_file = directory / "ca.pem"
    ca_file.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    chain = directory / "server-chain.pem"
    chain.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    key = directory / "server-key.pem"
    key.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(chain, key)
    return LocalCa(
        ca_file=ca_file,
        ca_sha256=ca_cert.fingerprint(hashes.SHA256()).hex(),
        server_context=context,
    )
