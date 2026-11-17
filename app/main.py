from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import service
from app.config import settings
from app.db import Base, engine, get_session
from app.models import Link
from app.schemas import LinkCreate, LinkOut


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Fine for a learning project. A production app would use migrations (Alembic).
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="URL Shortener", version="0.1.0", lifespan=lifespan)


def to_out(link: Link) -> LinkOut:
    return LinkOut(
        code=link.code,
        short_url=f"{settings.base_url.rstrip('/')}/{link.code}",
        target_url=link.target_url,
        created_at=link.created_at,
        expires_at=link.expires_at,
        clicks=link.clicks,
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/links", response_model=LinkOut, status_code=status.HTTP_201_CREATED)
def create(body: LinkCreate, session: Session = Depends(get_session)) -> LinkOut:
    try:
        link = service.create_link(session, str(body.url), body.alias, body.expires_in_days)
    except service.AliasTaken:
        raise HTTPException(status.HTTP_409_CONFLICT, "Alias is already taken")
    except service.CodeSpaceExhausted:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Could not allocate a code")
    return to_out(service.get_link(session, link.code))


@app.get("/api/links/{code}", response_model=LinkOut)
def stats(code: str, session: Session = Depends(get_session)) -> LinkOut:
    link = service.get_link(session, code)
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found")
    return to_out(link)


@app.get("/{code}")
def redirect(code: str, session: Session = Depends(get_session)) -> RedirectResponse:
    link = service.get_link(session, code)
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found")
    if service.is_expired(link):
        raise HTTPException(status.HTTP_410_GONE, "Link has expired")
    service.record_click(session, code)
    # 302 (temporary) instead of 301 (permanent): browsers cache 301s forever,
    # so later clicks would never reach us and could not be counted.
    return RedirectResponse(link.target_url, status_code=status.HTTP_302_FOUND)
