"""HTTP layer: turns requests into service calls, and errors into status codes."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import __version__, service
from app.config import settings
from app.db import Base, engine, get_session
from app.limits import LimitBodySize
from app.models import Link
from app.schemas import LinkCreate, LinkOut


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Creates the tables if they are missing. Fine for one instance, but it
    # checks first and creates second, so several instances starting at the same
    # moment on an empty database would race. Production apps run migrations
    # (e.g. Alembic) once, before any instance starts.
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="URL Shortener", version=__version__, lifespan=lifespan)
# Refuse oversized request bodies before reading them (NASA/JPL rule 3).
app.add_middleware(LimitBodySize)

# "Annotated" dependencies: declared once, reused by every route that needs them.
SessionDep = Annotated[Session, Depends(get_session)]


def to_out(link: Link) -> LinkOut:
    return LinkOut(
        code=link.code,
        short_url=f"{settings.base_url.rstrip('/')}/{link.code}",
        target_url=link.target_url,
        created_at=link.created_at,
        expires_at=link.expires_at,
        clicks=link.clicks,
    )


# HEAD is GET without the body. Link checkers and uptime monitors use it, so
# every page should answer it. (Hidden from the API docs to avoid duplicates.)
@app.head("/health", include_in_schema=False)
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/links", status_code=status.HTTP_201_CREATED)
def create(body: LinkCreate, session: SessionDep) -> LinkOut:
    try:
        link = service.create_link(session, str(body.url), body.alias, body.expires_in_days)
    except service.AliasTakenError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "Alias is already taken") from exc
    except service.CodeSpaceExhaustedError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Could not allocate a code"
        ) from exc
    return to_out(link)


@app.get("/api/links/{code}")
def stats(code: str, session: SessionDep) -> LinkOut:
    link = service.get_link(session, code)
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found")
    return to_out(link)


@app.head("/{code}", include_in_schema=False)
@app.get("/{code}")
def redirect(code: str, request: Request, session: SessionDep) -> RedirectResponse:
    link = service.get_link(session, code)
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found")
    if service.is_expired(link):
        raise HTTPException(status.HTTP_410_GONE, "Link has expired")
    # HEAD asks "where does this go?" without going there: a link checker or a
    # chat app's preview, not a person clicking. Only GET counts as a click.
    if request.method == "GET":
        service.record_click(session, code)
    # 302 (temporary) instead of 301 (permanent): browsers cache 301s forever,
    # so later clicks would never reach us and could not be counted.
    return RedirectResponse(link.target_url, status_code=status.HTTP_302_FOUND)
