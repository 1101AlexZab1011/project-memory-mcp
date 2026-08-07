"""Client identity: an Ed25519 keypair that never leaves the machine.

A token proves you hold a secret that some server issued you. A key proves you
are the same actor everywhere, because the proof is possession of something no
server ever saw. That difference is the whole reason for this module: with
several servers federating the same project, a display name is one server's
word, and only a fingerprint means the same thing on all of them.

Two consequences follow from the private key never being transmitted:

- Enrollment sends a *public* key rather than receiving a secret, so nothing
  worth stealing crosses the wire in either direction.
- The server's client table holds nothing confidential, which matters because
  this server ships scheduled snapshots and JSON exports.

Signing covers requests this package makes itself - a local server querying a
remote. An agent's MCP client that speaks HTTP directly sends static headers and
cannot sign, so those clients authenticate with a per-client bearer token
instead. Both are per-client and both are revocable; only one is portable.
"""

from __future__ import annotations

import base64
import hashlib
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Requests older than this are refused, so a captured one cannot be replayed.
# Wide enough to survive ordinary clock skew between machines, narrow enough
# that a recording is useless by the time anyone gets to it.
MAX_CLOCK_SKEW_SECONDS = 300


class IdentityError(Exception):
    """Raised for a missing key, a bad signature, or a malformed credential."""


def _ed25519():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as error:  # pragma: no cover - depends on the environment
        raise IdentityError(
            "Ed25519 support needs the 'cryptography' package: pip install cryptography"
        ) from error
    return ed25519, serialization


def generate_private_key(path: Path | str) -> bytes:
    """Create a keypair and store the private half with restricted permissions.

    Deliberately unencrypted: a passphrase would mean no agent could start
    unattended, which defeats the point. The key is exactly as sensitive as the
    machine it sits on, and file permissions are what say so.
    """
    ed25519, serialization = _ed25519()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(pem)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600; a no-op on some filesystems
    except OSError:
        pass
    return public_bytes(load_private_key(path))


def load_private_key(path: Path | str):
    ed25519, serialization = _ed25519()
    path = Path(path)
    if not path.is_file():
        raise IdentityError(f"No private key at {path}")
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise IdentityError(f"{path} is not an Ed25519 private key")
    return key


def load_or_create(path: Path | str):
    path = Path(path)
    if not path.is_file():
        generate_private_key(path)
    return load_private_key(path)


#: Where this machine's key lives unless it was told otherwise. Defined here
#: rather than in the CLI because the code that *signs* needs it too, and two
#: definitions of where a key lives is one too many.
DEFAULT_KEY_PATH = Path.home() / ".project-memory" / "client_key.pem"


def load_if_present(path: Path | str | None = None):
    """This machine's signing key, or None if it has never enrolled anywhere.

    Absence is the normal case and not an error: a local-only store never runs
    `join`, and a machine that federates with a bearer token does not need a key
    either. Callers treat None as "authenticate some other way".

    Deliberately does **not** create one. `load_or_create` exists for `join`,
    where a person asked for an identity. A background sweep that minted a
    keypair as a side effect would be enrolling a client nobody asked for.

    Read from disk each time rather than cached. It is a small file, the callers
    are about to wait on a network anyway, and caching would mean a key enrolled
    while the server is running does nothing until somebody restarts it.
    """
    path = Path(path) if path is not None else DEFAULT_KEY_PATH
    if not path.is_file():
        return None
    try:
        return load_private_key(path)
    except IdentityError:
        # A corrupt or wrong-type key file is not a reason to stop delivering
        # work that a bearer token could carry. It is reported by `join`, which
        # is where somebody can act on it.
        return None


def public_bytes(private_key) -> bytes:
    _, serialization = _ed25519()
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)


def encode_public(public: bytes) -> str:
    return base64.b64encode(public).decode("ascii")


def decode_public(encoded: str) -> bytes:
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception as error:
        raise IdentityError("Public key is not valid base64") from error
    if len(raw) != 32:
        raise IdentityError("An Ed25519 public key is 32 bytes")
    return raw


def fingerprint(public: bytes) -> str:
    """A stable short name for a key, in the shape OpenSSH uses.

    This is what attribution records. It means the same thing on every server,
    which a display name never can.
    """
    digest = base64.b64encode(hashlib.sha256(public).digest()).decode("ascii").rstrip("=")
    return f"SHA256:{digest}"


def canonical_request(method: str, path: str, timestamp: str, body: bytes) -> bytes:
    """The exact bytes a signature covers.

    Includes the body hash, so a signature authorises one request rather than
    one endpoint, and the timestamp, so it authorises it once.
    """
    return "\n".join([
        method.upper(), path, timestamp, hashlib.sha256(body).hexdigest(),
    ]).encode("utf-8")


def sign_request(private_key, method: str, path: str, body: bytes,
                 timestamp: str | None = None) -> dict[str, str]:
    """Headers proving this request came from the holder of this key."""
    timestamp = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    signature = private_key.sign(canonical_request(method, path, timestamp, body))
    return {
        "X-PM-Key": fingerprint(public_bytes(private_key)),
        "X-PM-Timestamp": timestamp,
        "X-PM-Signature": base64.b64encode(signature).decode("ascii"),
    }


def verify_request(public: bytes, headers: Any, method: str, path: str, body: bytes,
                   now: datetime | None = None) -> None:
    """Raise unless these headers prove the request came from this key, recently."""
    ed25519, _ = _ed25519()
    timestamp = headers.get("X-PM-Timestamp") or ""
    encoded = headers.get("X-PM-Signature") or ""
    if not timestamp or not encoded:
        raise IdentityError("Missing signature headers")
    try:
        sent = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise IdentityError("Timestamp must be ISO-8601 UTC") from error
    drift = abs((now or datetime.now(timezone.utc)) - sent).total_seconds()
    if drift > MAX_CLOCK_SKEW_SECONDS:
        raise IdentityError(f"Timestamp is {int(drift)}s away from now; refusing a possible replay")
    try:
        signature = base64.b64decode(encoded, validate=True)
        ed25519.Ed25519PublicKey.from_public_bytes(public).verify(
            signature, canonical_request(method, path, timestamp, body))
    except Exception as error:
        raise IdentityError("Signature does not verify") from error
