"""Prepare the host directory for a security-on test Elasticsearch (BUILD-PLAN 1.2).

Writes, under the directory given as argv[1] (outside the repository):
  tls/ca.pem, tls/ca.key (0600), tls/node.pem, tls/node.key   HTTP TLS material, SAN localhost and 127.0.0.1
  bootstrap.pw (0600)    random bootstrap credential, passed to the container by file
                         (ELASTIC_PASSWORD_FILE), never printed, logged or put on a command line
  data/                  the node's data directory
Prints only the paths and the CA certificate's SHA-256. Needs `cryptography`.
"""
import datetime
import hashlib
import ipaddress
import os
import secrets
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def write_private(path: Path, data: bytes, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(path, mode)


def key_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption())


def main(base: str) -> None:
    root = Path(base)
    tls = root / "tls"
    tls.mkdir(parents=True, exist_ok=True, mode=0o700)
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "lc-test-ca")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
          .not_valid_after(now + datetime.timedelta(days=30))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .sign(ca_key, hashes.SHA256()))
    node_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    node = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
            .issuer_name(ca_name).public_key(node_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                                                        x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .sign(ca_key, hashes.SHA256()))
    write_private(tls / "ca.key", key_pem(ca_key))
    write_private(tls / "ca.pem", ca.public_bytes(serialization.Encoding.PEM), 0o644)
    write_private(tls / "node.key", key_pem(node_key), 0o644)  # read by the container's user; the directory is 0700
    write_private(tls / "node.pem", node.public_bytes(serialization.Encoding.PEM), 0o644)
    write_private(root / "bootstrap.pw", secrets.token_urlsafe(24).encode())
    (root / "data").mkdir(exist_ok=True)
    os.chmod(root / "data", 0o777)
    print("tls dir:", tls)
    print("bootstrap credential file:", root / "bootstrap.pw", "(0600, not printed)")
    print("ca sha256:", hashlib.sha256(ca.public_bytes(serialization.Encoding.DER)).hexdigest())


if __name__ == "__main__":
    main(sys.argv[1])
