"""Read operator-selected HTTPS RSS/Atom feeds and reduce them to bounded plain text."""

import asyncio
import hashlib
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit
from xml.etree import ElementTree

from stage_01_model_tools_py.transport_py import Http, ProviderError, ProviderFormatError

MAX_FEED_BYTES = 1_000_000
MAX_ITEMS = 12
ATOM = "{http://www.w3.org/2005/Atom}"


@dataclass(frozen=True)
class Article:
    id: str
    title: str
    text: str
    url: str


@dataclass(frozen=True)
class Intake:
    items: tuple[Article, ...]
    unavailable: int = 0


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def clean(text: str, limit: int) -> str:
    parser = PlainText()
    parser.feed(text)
    parser.close()
    return " ".join(" ".join(parser.parts).split())[:limit]


def article(title: str, text: str, url: str) -> Article | None:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username:
            return None
    except ValueError:
        return None
    title = clean(title, 240)
    if not title or len(url) > 2000:
        return None
    # Ignore the fragment, which often just points at a heading on the same story.
    link = parsed._replace(fragment="").geturl()
    digest = hashlib.sha256(link.encode()).hexdigest()[:20]
    return Article(digest, title, clean(text, 900), link)


def parse_feed(raw: bytes) -> list[Article]:
    if len(raw) > MAX_FEED_BYTES:
        raise ProviderFormatError("feed is too large")
    try:
        text = raw.decode("utf-8-sig")
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            raise ValueError("XML document type declarations are excluded")
        root = ElementTree.fromstring(text)
    except (UnicodeError, ValueError, ElementTree.ParseError):
        raise ProviderFormatError("feed must be valid UTF-8 RSS or Atom") from None

    if root.tag == "rss":
        rows = (
            (
                item.findtext("title", ""),
                item.findtext("description", ""),
                item.findtext("link", ""),
            )
            for item in root.findall("channel/item")[:MAX_ITEMS]
        )
    elif root.tag == f"{ATOM}feed":
        rows = (
            (
                entry.findtext(f"{ATOM}title", ""),
                entry.findtext(f"{ATOM}summary", "") or entry.findtext(f"{ATOM}content", ""),
                next(
                    (
                        link.get("href", "")
                        for link in entry.findall(f"{ATOM}link")
                        if link.get("rel", "alternate") == "alternate"
                    ),
                    "",
                ),
            )
            for entry in root.findall(f"{ATOM}entry")[:MAX_ITEMS]
        )
    else:
        raise ProviderFormatError("feed root is neither RSS nor Atom")
    items = (article(title, body, url.strip()) for title, body, url in rows)
    return [item for item in items if item is not None]


async def fetch_feeds(http: Http, urls: list[str]) -> Intake:
    if not 1 <= len(urls) <= 5:
        raise ValueError("provide one to five feed URLs")
    for url in urls:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None:
            raise ValueError("feed URLs must use HTTPS with no embedded credentials")

    async def fetch(url: str):
        try:
            # Feed addresses come from the operator. Embedded article URLs are
            # retained as citations and are never fetched.
            response = await http.request(
                "RSS feed", "GET", url, retry=True, max_bytes=MAX_FEED_BYTES
            )
            return parse_feed(response.content)
        except (ProviderError, ProviderFormatError):
            return None

    outcomes = await asyncio.gather(*(fetch(url) for url in urls))
    if all(outcome is None for outcome in outcomes):
        raise ProviderError("all RSS sources")
    unique: dict[str, Article] = {}
    for outcome in outcomes:
        for item in outcome or ():
            unique.setdefault(item.id, item)
    return Intake(tuple(unique.values())[:MAX_ITEMS], outcomes.count(None))


async def demo_feed() -> Intake:
    data = b"""<rss version="2.0"><channel>
      <item><title>Service restores access</title>
        <link>https://example.invalid/restored</link><description>Customers praise the recovery.</description>
      </item>
      <item><title>Payment outage frustrates customers</title>
        <link>https://example.invalid/outage</link><description>Several regions reported delays.</description>
      </item>
      <item><title>Company schedules earnings call</title>
        <link>https://example.invalid/earnings</link><description>The call is Thursday.</description>
      </item>
    </channel></rss>"""
    return Intake(tuple(parse_feed(data)))
