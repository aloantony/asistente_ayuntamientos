from dataclasses import dataclass
from typing import Annotated

from fastapi import Query, Response
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

DEFAULT_PAGE_LIMIT = 100
MAX_PAGE_LIMIT = 200

TOTAL_COUNT_HEADER = "X-Total-Count"


@dataclass
class PageParams:
    limit: int
    offset: int


def page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_LIMIT)] = DEFAULT_PAGE_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PageParams:
    return PageParams(limit=limit, offset=offset)


def paginate(
    db: Session,
    query: Select,
    page: PageParams,
    response: Response,
) -> Select:
    """Expose the unpaginated total via X-Total-Count and slice the query."""
    total = (
        db.scalar(
            select(func.count()).select_from(query.order_by(None).subquery())
        )
        or 0
    )
    response.headers[TOTAL_COUNT_HEADER] = str(total)
    return query.limit(page.limit).offset(page.offset)
