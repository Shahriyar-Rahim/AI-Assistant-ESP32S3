from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.dependencies import require_user
from app.services import news

router = APIRouter()


@router.get("/news")
async def latest_news(
    request: Request,
    genre: str | None = Query(default=None, pattern="^(world|business|technology|entertainment|sports|science|health)$"),
    topic: str | None = Query(default=None, min_length=2, max_length=80),
    country: str | None = Query(default=None, min_length=2, max_length=2),
    limit: int = Query(default=8, ge=1, le=20),
    _: object = Depends(require_user),
) -> dict[str, object]:
    code = (country or request.app.state.settings.news_default_country).upper()
    if code not in news.COUNTRIES:
        raise HTTPException(422, "Unsupported country code")
    selection = news.NewsRequest(
        None if topic else genre, topic, code, news.COUNTRIES[code][0] if country else None,
        (topic or genre or news.COUNTRIES[code][0] if country else "world").upper(),
    )
    try:
        headlines = await news.fetch_headlines(selection, limit)
    except news.NewsError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {
        "label": selection.label,
        "headlines": [{"title": item.title, "source": item.source, "published": item.published} for item in headlines],
    }
