"""The server's view of who is allowed to do what.

Three things a shared token cannot give, each of which something else depends on:

- **Attribution.** Every write records which client made it, so provenance is a
  fact rather than a guess. Per-replica counters need it too.
- **Revocation.** A lost laptop is one row, not a rotation everybody has to
  follow - which is the rotation nobody performs.
- **Permissions.** Without them the rule that agents may mark a memory wrong but
  not delete it is advisory, and anyone who reaches the port can empty the store.

The shared token still works. Turning it off is a deployment decision, not a
consequence of enrolling the first client, so an existing setup keeps running
while clients are migrated one at a time.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import uuid as uuid_module
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import identity
from .validation import StoreError

ROLES = ("contributor", "admin")
CODE_TTL_MINUTES = 15
#: Deliberately short and unambiguous: this gets read aloud or pasted into chat.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Client:
    client_id: str
    name: str
    fingerprint: str | None
    role: str
    project_scope: tuple[str, ...] | None  # None means every project

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def may_access(self, project: str) -> bool:
        return self.project_scope is None or project in self.project_scope

    def describe(self) -> dict[str, Any]:
        return {"client_id": self.client_id, "name": self.name, "role": self.role,
                "fingerprint": self.fingerprint,
                "projects": list(self.project_scope) if self.project_scope else "all"}


#: The identity attached to writes made with the legacy shared token. Named
#: rather than left blank so that history from before enrollment reads honestly
#: instead of looking like it came from nobody.
SHARED_TOKEN_CLIENT = Client("shared-token", "shared token", None, "admin", None)


def _row_to_client(row: sqlite3.Row) -> Client:
    scope = row["project_scope"]
    return Client(
        client_id=row["client_id"], name=row["name"], fingerprint=row["fingerprint"],
        role=row["role"], project_scope=tuple(scope.split(",")) if scope else None)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_code(connection: sqlite3.Connection, name: str | None = None,
                role: str = "contributor", projects: list[str] | None = None) -> dict[str, Any]:
    """Mint a single-use enrollment code."""
    if role not in ROLES:
        raise StoreError(f"role must be one of {', '.join(ROLES)}")
    code = "-".join("".join(secrets.choice(CODE_ALPHABET) for _ in range(4)) for _ in range(2))
    expires = datetime.now(timezone.utc) + timedelta(minutes=CODE_TTL_MINUTES)
    with connection:
        connection.execute(
            "INSERT INTO enrollment_codes(code, name, role, project_scope, expires_at, created) "
            "VALUES (?,?,?,?,?,?)",
            (code, name, role, ",".join(projects) if projects else None,
             expires.strftime("%Y-%m-%dT%H:%M:%SZ"), _now()),
        )
    return {"code": code, "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "role": role, "projects": projects or "all", "valid_for_minutes": CODE_TTL_MINUTES}


def redeem_code(connection: sqlite3.Connection, code: str, name: str,
                public_key: str | None = None) -> dict[str, Any]:
    """Turn a code into a client.

    With a public key, nothing secret is created or returned - the client keeps
    the only half that matters. Without one, a bearer token is issued for clients
    that cannot sign, and only its hash is stored.
    """
    row = connection.execute("SELECT * FROM enrollment_codes WHERE code=?", (code,)).fetchone()
    if row is None:
        raise StoreError("Unknown enrollment code.")
    if row["used_at"]:
        raise StoreError("That enrollment code has already been used.")
    if row["expires_at"] < _now():
        raise StoreError("That enrollment code has expired.")

    client_id = str(uuid_module.uuid4())
    fingerprint = token = token_hash = None
    if public_key:
        raw = identity.decode_public(public_key)
        fingerprint = identity.fingerprint(raw)
        if connection.execute("SELECT 1 FROM clients WHERE fingerprint=?", (fingerprint,)).fetchone():
            raise StoreError("That key is already enrolled on this server.")
    else:
        token = secrets.token_urlsafe(32)
        token_hash = _hash_token(token)

    with connection:
        connection.execute(
            "INSERT INTO clients(client_id, name, public_key, fingerprint, token_hash, role, "
            "project_scope, created) VALUES (?,?,?,?,?,?,?,?)",
            (client_id, name or row["name"] or "client", public_key, fingerprint, token_hash,
             row["role"], row["project_scope"], _now()),
        )
        connection.execute("UPDATE enrollment_codes SET used_at=?, used_by=? WHERE code=?",
                           (_now(), client_id, code))
    result = {"client_id": client_id, "name": name or row["name"], "role": row["role"],
              "fingerprint": fingerprint,
              "projects": row["project_scope"].split(",") if row["project_scope"] else "all"}
    if token:
        result["token"] = token  # returned once, never stored in the clear
    return result


def authenticate(connection: sqlite3.Connection, headers: Any, method: str, path: str,
                 body: bytes, shared_token: str | None) -> Client:
    """Identify the caller, or raise.

    Signature first: it proves possession of a key without putting anything
    reusable on the wire, which matters when the deployment has no TLS. A bearer
    token is accepted for clients that cannot sign, and the shared token is
    accepted last so that existing setups keep working.
    """
    key_id = headers.get("X-PM-Key")
    if key_id:
        row = connection.execute(
            "SELECT * FROM clients WHERE fingerprint=?", (key_id,)).fetchone()
        if row is None:
            raise StoreError("Unknown key fingerprint; this client is not enrolled here.")
        if row["revoked_at"]:
            raise StoreError("This client has been revoked.")
        try:
            identity.verify_request(identity.decode_public(row["public_key"]),
                                    headers, method, path, body)
        except identity.IdentityError as error:
            raise StoreError(str(error)) from error
        _touch(connection, row["client_id"])
        return _row_to_client(row)

    header = headers.get("Authorization") or ""
    if not header.startswith("Bearer "):
        raise StoreError("Authenticate with a signature or 'Authorization: Bearer <token>'.")
    presented = header[len("Bearer "):].strip()
    if presented.startswith("${") and presented.endswith("}"):
        raise StoreError(f"token was sent unexpanded as {presented!r} - the environment "
                         "variable is not set where the MCP client starts")

    digest = _hash_token(presented)
    for row in connection.execute("SELECT * FROM clients WHERE token_hash IS NOT NULL"):
        if hmac.compare_digest(digest, row["token_hash"]):
            if row["revoked_at"]:
                raise StoreError("This client has been revoked.")
            _touch(connection, row["client_id"])
            return _row_to_client(row)

    if shared_token and hmac.compare_digest(presented, shared_token):
        return SHARED_TOKEN_CLIENT
    raise StoreError("invalid token")


def client_for_token(connection: sqlite3.Connection, presented: str,
                     shared_token: str | None = None) -> Client | None:
    """Which client holds this bearer token, or None. The UI login path.

    Replaces a `token_is_valid` that answered yes or no. That was the whole bug:
    the browser session it authorised remembered *that* somebody signed in and
    not *who*, so a client scoped to one project got a session scoped to all of
    them - and the UI, unlike /mcp, never checked.

    Deliberately not merged with `authenticate` even though the token lookup is
    the same. That one raises to say which of "unknown" and "revoked" happened,
    because an MCP client needs to know whether to re-enroll or give up. This
    one returns None either way: an unauthenticated browser must not be told
    that a token it presented was real but withdrawn.
    """
    if not presented:
        return None
    digest = _hash_token(presented)
    for row in connection.execute("SELECT * FROM clients WHERE token_hash IS NOT NULL"):
        if hmac.compare_digest(digest, row["token_hash"]):
            return None if row["revoked_at"] else _row_to_client(row)
    # Last, and only if configured, matching the order in `authenticate`: the
    # shared token is the legacy credential and holds no scope, so a deployment
    # that has moved to per-client tokens should not find it shadowing one.
    if shared_token and hmac.compare_digest(presented, shared_token):
        return SHARED_TOKEN_CLIENT
    return None


def _touch(connection: sqlite3.Connection, client_id: str) -> None:
    with connection:
        connection.execute("UPDATE clients SET last_seen=? WHERE client_id=?", (_now(), client_id))


def list_clients(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute("SELECT * FROM clients ORDER BY created").fetchall()
    out = []
    for row in rows:
        entry = _row_to_client(row).describe()
        entry.update({"created": row["created"], "last_seen": row["last_seen"],
                      "revoked_at": row["revoked_at"],
                      "auth": "key" if row["fingerprint"] else "token"})
        out.append(entry)
    return out


def revoke(connection: sqlite3.Connection, client_id: str) -> dict[str, Any]:
    """Take a client's access away without erasing what it wrote.

    The row stays: attribution is history, and history should not be editable by
    whoever holds the newest credential.
    """
    row = connection.execute("SELECT 1 FROM clients WHERE client_id=?", (client_id,)).fetchone()
    if row is None:
        raise StoreError(f"Unknown client: {client_id}")
    with connection:
        connection.execute("UPDATE clients SET revoked_at=? WHERE client_id=?", (_now(), client_id))
    return {"revoked": client_id}
