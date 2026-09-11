"""Flush domain changes without committing the caller-owned transaction."""
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


def flush_or_conflict(db: Session) -> None:
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Municipal operation conflicts with existing data") from None
