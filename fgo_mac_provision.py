#!/usr/bin/env python3
"""First General Order - standalone clean Mac PKI provisioning (no app package needed)."""
import base64
import datetime as dt
import json
import os
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

IDENTITIES = ("core", "ai", "map", "distance", "media", "connect", "supervisor", "client", "installer")


def data_root() -> Path:
    override = os.environ.get("FGO_DATA_ROOT")
    if override:
        return Path(override)
    return Path.home() / ".local" / "share" / "first-general-order-machine"


def _name(cn: str) -> x509.Name:
    return x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "First General Order"),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "Mac Services Domain"),
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
    ])


def _write(path: Path, value: bytes, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(value)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600 if private else 0o644)
    except OSError:
        pass


def _portable_key(pem: bytes) -> bytes:
    return b"FGO:PLAIN:DEVELOPMENT:v2\0" + pem


def _pem_private(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def init(dest: Path, force: bool) -> Path:
    root = Path(dest)
    marker = root / "initialized-v2.json"
    if marker.exists() and root.joinpath("trust", "root-ca.pem").exists() and not force:
        return root
    now = dt.datetime.now(dt.timezone.utc)

    root_key = ec.generate_private_key(ec.SECP384R1())
    root_cert = (
        x509.CertificateBuilder().subject_name(_name("FGO Mac Offline Root CA"))
        .issuer_name(_name("FGO Mac Offline Root CA")).public_key(root_key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .add_extension(x509.KeyUsage(True, False, False, False, False, True, True, False, False), critical=True)
        .sign(root_key, hashes.SHA384())
    )
    issuing_key = ec.generate_private_key(ec.SECP384R1())
    issuing_cert = (
        x509.CertificateBuilder().subject_name(_name("FGO Mac Machine Issuing CA"))
        .issuer_name(root_cert.subject).public_key(issuing_key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1825))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(True, False, False, False, False, True, True, False, False), critical=True)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()), critical=False)
        .sign(root_key, hashes.SHA384())
    )

    _write(root / "trust" / "root-ca.pem", root_cert.public_bytes(serialization.Encoding.PEM))
    _write(root / "trust" / "issuing-ca.pem", issuing_cert.public_bytes(serialization.Encoding.PEM))
    _write(root / "authority" / "issuing-ca.key.dpapi", _portable_key(_pem_private(issuing_key)), private=True)

    for identity in IDENTITIES:
        key = ec.generate_private_key(ec.SECP256R1())
        cert = (
            x509.CertificateBuilder().subject_name(_name("FGO Mac " + identity))
            .issuer_name(issuing_cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=397))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(False, False, True, False, False, False, False, False, False), critical=True)
            .add_extension(x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectAlternativeName(
                [x509.UniformResourceIdentifier("urn:fgo:v2:" + identity)]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuing_key.public_key()), critical=False)
            .sign(issuing_key, hashes.SHA384())
        )
        loc = root / "identities" / identity
        _write(loc / "certificate.pem", cert.public_bytes(serialization.Encoding.PEM))
        _write(loc / "private.key.dpapi", _portable_key(_pem_private(key)), private=True)

    _write(root / "revoked-serials.json", b'{"serials":[]}')
    _write(marker, json.dumps({
        "version": 2,
        "created_at": now.isoformat(),
        "root_fingerprint_sha256": root_cert.fingerprint(hashes.SHA256()).hex(),
        "issuing_fingerprint_sha256": issuing_cert.fingerprint(hashes.SHA256()).hex(),
    }, sort_keys=True, indent=2).encode("utf-8"))
    return root


def enroll_client(dest: Path, out: Path) -> Path:
    root = Path(dest)
    bundle = {
        "root_ca": _b64(root / "trust" / "root-ca.pem"),
        "issuing_ca": _b64(root / "trust" / "issuing-ca.pem"),
        "initialized_v2": (root / "initialized-v2.json").read_text(encoding="utf-8"),
        "client_certificate": _b64(root / "identities" / "client" / "certificate.pem"),
        "client_private_key": _b64(root / "identities" / "client" / "private.key.dpapi"),
    }
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(bundle, sort_keys=True, indent=2), encoding="utf-8")
    return out


def _b64(path: Path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(__doc__)
        return 2
    cmd = args[0]
    root = data_root() / "pki"
    if cmd == "init":
        dest = init(root, "--force" in args[1:])
        print("initialized Mac PKI:", dest)
        return 0
    if cmd == "enroll-client" and len(args) >= 2:
        out = enroll_client(root, args[1])
        print("client enrollment bundle:", out)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
