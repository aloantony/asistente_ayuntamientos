"""Canonical transaction lock for authorization-graph changes.

PostgreSQL row locks cannot safely cover a permission decision spread across
several many-to-many tables: a joined ``FOR UPDATE`` both obscures which rows
are locked and can invert the order used by membership mutators. The single
transaction-scoped advisory lock below is the explicit serialization point for
short authorization checks and mutations. It is deliberately database-global
because role/permission changes can affect users in many organizations at once.
"""

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session, SessionTransaction

# Signed int32 keys accepted by pg_advisory_xact_lock(int, int). The first is
# the ASCII-ish namespace ``AUTH``; the second versions this lock contract.
AUTHORIZATION_GRAPH_LOCK_NAMESPACE = 1_095_584_328
AUTHORIZATION_GRAPH_LOCK_VERSION = 1


@dataclass(frozen=True)
class AuthorizationGraphLock:
    transaction: SessionTransaction


def lock_authorization_graph(db: Session) -> AuthorizationGraphLock:
    """Serialize an RBAC evidence check or graph mutation until transaction end."""

    db.execute(
        text(
            "SELECT pg_advisory_xact_lock("
            ":lock_namespace, :lock_version)"
        ),
        {
            "lock_namespace": AUTHORIZATION_GRAPH_LOCK_NAMESPACE,
            "lock_version": AUTHORIZATION_GRAPH_LOCK_VERSION,
        },
    )
    transaction = db.get_transaction()
    if transaction is None:
        raise RuntimeError("Authorization graph lock has no active transaction")
    return AuthorizationGraphLock(transaction=transaction)


def require_authorization_graph_lock(
    db: Session,
    authorization_lock: AuthorizationGraphLock,
) -> None:
    """Fail closed if a boundary token no longer belongs to this transaction."""

    if db.get_transaction() is not authorization_lock.transaction:
        raise RuntimeError("Authorization graph lock is not active")
