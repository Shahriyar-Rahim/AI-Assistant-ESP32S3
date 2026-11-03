from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote
from xml.etree import ElementTree

import httpx

# Google News publishes public RSS feeds for headlines, topics and searches; no key or scraping is needed.
GOOGLE_NEWS = "https://news.google.com/rss"
MAX_FEED_BYTES = 2_000_000

GENRES = {
    "world": "WORLD",
    "business": "BUSINESS",
    "technology": "TECHNOLOGY",
    "entertainment": "ENTERTAINMENT",
    "sports": "SPORTS",
    "science": "SCIENCE",
    "health": "HEALTH",
}
GENRE_WORDS = {
    "world": "world", "international": "world", "global": "world",
    "business": "business", "economy": "business", "economic": "business", "finance": "business",
    "financial": "business", "market": "business", "markets": "business", "stock": "business", "stocks": "business",
    "tech": "technology", "technology": "technology", "gadgets": "technology", "ai": "technology",
    "entertainment": "entertainment", "movie": "entertainment", "movies": "entertainment",
    "celebrity": "entertainment", "film": "entertainment", "music": "entertainment",
    "sport": "sports", "sports": "sports", "football": "sports", "cricket": "sports",
    "soccer": "sports", "basketball": "sports", "tennis": "sports",
    "science": "science", "space": "science", "health": "health", "medical": "health", "medicine": "health",
}

COUNTRIES = {
    "US": ("United States", ("united states", "usa", "u s a", "america", "american")),
    "GB": ("the United Kingdom", ("united kingdom", "uk", "u k", "britain", "british", "england")),
    "IN": ("India", ("india", "indian")),
    "BD": ("Bangladesh", ("bangladesh", "bangladeshi")),
    "PK": ("Pakistan", ("pakistan", "pakistani")),
    "CA": ("Canada", ("canada", "canadian")),
    "AU": ("Australia", ("australia", "australian")),
    "NZ": ("New Zealand", ("new zealand",)),
    "IE": ("Ireland", ("ireland", "irish")),
    "ZA": ("South Africa", ("south africa",)),
    "NG": ("Nigeria", ("nigeria", "nigerian")),
    "KE": ("Kenya", ("kenya", "kenyan")),
    "EG": ("Egypt", ("egypt", "egyptian")),
    "SA": ("Saudi Arabia", ("saudi arabia", "saudi")),
    "AE": ("the United Arab Emirates", ("uae", "u a e", "emirates", "dubai")),
    "QA": ("Qatar", ("qatar",)),
    "TR": ("Turkey", ("turkey", "turkish")),
    "IL": ("Israel", ("israel", "israeli")),
    "IR": ("Iran", ("iran", "iranian")),
    "RU": ("Russia", ("russia", "russian")),
    "UA": ("Ukraine", ("ukraine", "ukrainian")),
    "DE": ("Germany", ("germany", "german")),
    "FR": ("France", ("france", "french")),
    "IT": ("Italy", ("italy", "italian")),
    "ES": ("Spain", ("spain", "spanish")),
    "NL": ("the Netherlands", ("netherlands", "dutch", "holland")),
    "SE": ("Sweden", ("sweden", "swedish")),
    "NO": ("Norway", ("norway", "norwegian")),
    "CH": ("Switzerland", ("switzerland", "swiss")),
    "PL": ("Poland", ("poland", "polish")),
    "GR": ("Greece", ("greece", "greek")),
    "CN": ("China", ("china", "chinese")),
    "JP": ("Japan", ("japan", "japanese")),
    "KR": ("South Korea", ("south korea", "korea", "korean")),
    "TW": ("Taiwan", ("taiwan",)),
    "HK": ("Hong Kong", ("hong kong",)),
    "SG": ("Singapore", ("singapore",)),
    "MY": ("Malaysia", ("malaysia", "malaysian")),
    "ID": ("Indonesia", ("indonesia", "indonesian")),
    "TH": ("Thailand", ("thailand", "thai")),
    "VN": ("Vietnam", ("vietnam", "vietnamese")),
    "PH": ("the Philippines", ("philippines", "filipino")),
    "LK": ("Sri Lanka", ("sri lanka",)),
    "NP": ("Nepal", ("nepal", "nepali")),
    "BR": ("Brazil", ("brazil", "brazilian")),
    "AR": ("Argentina", ("argentina",)),
    "MX": ("Mexico", ("mexico", "mexican")),
    "CL": ("Chile", ("chile",)),
    "CO": ("Colombia", ("colombia",)),
}
_ALIASES = sorted(
    ((alias, code) for code, (_, aliases) in COUNTRIES.items() for alias in aliases),
    key=lambda pair: -len(pair[0]),
)

_NEWS_WORD = re.compile(r"\b(news|headlines?|newspaper|what(?:'s|s| is) happening|what is going on|latest on)\b")
_FILLER = re.compile(
    r"\b(?:please|buddy|hey|me|give|tell|show|read|get|fetch|bring|up|us|the|a|an|latest|top|hottest|breaking|"
    r"topic|topics|story|stories|trending|happening|going|current|recent|today'?s?|news|headlines?|about|on|of|from|in|for|any|some|your|my|what|is|are|there|"
    r"can|could|you|i|want|to|know|hear|see|with|regarding|related)\b"
)


@dataclass(frozen=True)
class NewsRequest:
    genre: str | None
    query: str | None
    country_code: str
    country_name: str | None
    label: str


@dataclass(frozen=True)
class Headline:
    title: str
    source: str
    published: str


class NewsError(RuntimeError):
    pass


def is_news_question(lowered: str) -> bool:
    return bool(_NEWS_WORD.search(lowered))


def parse_news_request(text: str, default_country: str = "US") -> NewsRequest | None:
    lowered = re.sub(r"\s+", " ", re.sub(r"[^\w\s']", " ", text.casefold())).strip()
    if not is_news_question(lowered):
        return None
    country_code = default_country
    country_name: str | None = None
    remainder = lowered
    matched_alias = ""
    for alias, code in _ALIASES:
        pattern = rf"\b{re.escape(alias)}\b"
        if re.search(pattern, remainder):
            country_code, country_name, matched_alias = code, COUNTRIES[code][0], alias
            remainder = re.sub(pattern, " ", remainder, count=1)
            break
    topic = re.search(r"\b(?:about|regarding|related to|concerning|around)\s+(.+)$", remainder)
    topic_text = topic.group(1) if topic else None
    genre: str | None = None
    for word in re.findall(r"[a-z]+", remainder):
        if word in GENRE_WORDS:
            genre = GENRE_WORDS[word]
            break
    if topic_text is None:
        residue = re.sub(r"\s+", " ", _FILLER.sub(" ", remainder)).strip()
        leftover = [word for word in residue.split() if GENRE_WORDS.get(word) != genre or genre is None]
        if genre is None and leftover:
            topic_text = " ".join(leftover)
    elif topic_text:
        topic_text = re.sub(r"\s+", " ", _FILLER.sub(" ", topic_text)).strip() or topic_text.strip()
    if topic_text:
        genre = None
        label = topic_text[:24].upper()
        if matched_alias:
            topic_text = f"{topic_text} {matched_alias}"
            country_code = default_country
    elif genre:
        label = genre.upper()
    else:
        label = (country_name or "WORLD").upper()
    return NewsRequest(genre, topic_text or None, country_code, country_name, label)


def _feed_url(request: NewsRequest, *, search_fallback: bool = False) -> str:
    edition = f"hl=en-US&gl={request.country_code}&ceid={request.country_code}:en"
    if request.query or search_fallback:
        query = request.query or " ".join(
            part for part in (request.country_name or "", request.genre or "") if part
        ) or "world"
        return f"{GOOGLE_NEWS}/search?q={quote(query + ' when:2d')}&{edition}"
    if request.genre:
        return f"{GOOGLE_NEWS}/headlines/section/topic/{GENRES[request.genre]}?{edition}"
    if request.country_name:
        return f"{GOOGLE_NEWS}?{edition}"
    return f"{GOOGLE_NEWS}/headlines/section/topic/WORLD?{edition}"


def parse_feed(content: bytes, limit: int) -> tuple[list[Headline], str]:
    if len(content) > MAX_FEED_BYTES or b"<!ENTITY" in content or b"<!DOCTYPE" in content:
        raise NewsError("news feed was rejected as unsafe")
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise NewsError("news feed could not be parsed") from exc
    channel_link = root.findtext("channel/link") or ""
    headlines: list[Headline] = []
    seen: set[str] = set()
    for item in root.findall("channel/item"):
        raw = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "").strip()
        if source and raw.endswith(f" - {source}"):
            raw = raw[: -len(source) - 3].rstrip()
        elif " - " in raw and not source:
            raw, _, source = raw.rpartition(" - ")
        key = re.sub(r"\W+", "", raw.casefold())[:60]
        if not raw or key in seen:
            continue
        seen.add(key)
        headlines.append(Headline(raw, source, (item.findtext("pubDate") or "").strip()))
        if len(headlines) >= limit:
            break
    return headlines, channel_link


async def fetch_headlines(request: NewsRequest, limit: int = 5) -> list[Headline]:
    async def get(url: str) -> bytes:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=4), follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": "edge-ai-assistant/1.0"})
        response.raise_for_status()
        return response.content

    try:
        headlines, channel_link = parse_feed(await get(_feed_url(request)), limit)
        unsupported_edition = (
            request.country_name and not request.query and not request.genre
            and f"gl={request.country_code}" not in channel_link
        )
        if unsupported_edition or not headlines:
            headlines, _ = parse_feed(await get(_feed_url(request, search_fallback=True)), limit)
    except httpx.HTTPError as exc:
        raise NewsError("the news service could not be reached") from exc
    return headlines


def wrap_lines(text: str, width: int = 30, max_lines: int = 8) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        while len(word) > width:
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:width])
            word = word[width:]
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: width - 3].rstrip() + "..."
    return lines
