"""Web tools for the swarm: keyless search (ddgs), page reading (httpx + trafilatura),
a headless browser for JavaScript-heavy pages (Playwright Chromium) and claim checking
against a whole source with SubQ Snap (needs SUBQ_API_KEY)."""

import functools
import io
import ipaddress
import logging
import os
import socket
import time
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura
from ddgs import DDGS
from langchain_core.tools import tool
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from pypdf import PdfReader

# Browsers live in the persistent workspace so they survive devlab restarts.
DEVLAB_BROWSERS = "/workspace/.cache/ms-playwright"
if os.path.isdir(DEVLAB_BROWSERS):
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", DEVLAB_BROWSERS)

# pypdf logs a warning for every odd font or xref in real-world PDFs.
logging.getLogger("pypdf").setLevel(logging.ERROR)

USER_AGENT = "swarm-lab/0.1 (research agent)"
TIMEOUT = 20
MAX_REDIRECTS = 5

_robots: dict[str, RobotFileParser] = {}


def _is_public(url: str) -> bool:
    """Block localhost, LAN and cloud-metadata addresses (other devlabs share this network)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    return _host_is_public(parsed.hostname)


@functools.lru_cache(maxsize=1024)
def _host_is_public(hostname: str) -> bool:
    # Cached: a browsed page can make dozens of sub-requests to the same hosts.
    try:
        infos = socket.getaddrinfo(hostname, 443)
    except socket.gaierror:
        return False
    return all(ipaddress.ip_address(info[4][0]).is_global for info in infos)


def _robots_allows(url: str) -> bool:
    parsed = urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    if root not in _robots:
        rp = RobotFileParser()
        try:
            r = httpx.get(root + "/robots.txt", timeout=10, headers={"User-Agent": USER_AGENT})
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except httpx.HTTPError:
            rp.parse([])
        _robots[root] = rp
    return _robots[root].can_fetch(USER_AGENT, url)


def _get(url: str) -> httpx.Response:
    """GET that re-checks every redirect hop against the public-address rule."""
    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            if not _is_public(url):
                raise ValueError(f"refusing non-public address: {url}")
            r = client.get(url)
            if not r.is_redirect:
                r.raise_for_status()
                return r
            url = urljoin(url, r.headers["location"])
    raise ValueError("too many redirects")


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web. Returns numbered results with title, URL and snippet."""
    try:
        results = DDGS().text(query, max_results=min(max_results, 10))
    except Exception as e:  # ddgs raises its own exception types per backend
        return f"Search failed: {e}"
    if not results:
        return "No results."
    return "\n\n".join(
        f"[{i}] {r['title']}\n{r['href']}\n{r['body']}" for i, r in enumerate(results, 1)
    )


class PageError(Exception):
    """A page could not be read; the message is written for the agent."""


def _page_text(url: str, max_pdf_pages: int | None = None, max_chars: int | None = None) -> str:
    """Full main text of a public web page or PDF (robots.txt respected).
    max_pdf_pages limits the PDF pages read; max_chars stops reading PDF pages once
    that much text is in hand (the caller still truncates)."""
    if not _is_public(url):
        raise PageError(f"Refusing non-public or invalid address: {url}")
    if not _robots_allows(url):
        raise PageError(f"robots.txt disallows fetching {url}")
    try:
        r = _get(url)
    except (httpx.HTTPError, ValueError) as e:
        raise PageError(f"Fetch failed: {e}") from e

    if "pdf" in r.headers.get("content-type", "") or url.lower().endswith(".pdf"):
        try:
            parts, size = [], 0
            for page in PdfReader(io.BytesIO(r.content)).pages[:max_pdf_pages]:
                parts.append(page.extract_text() or "")
                size += len(parts[-1]) + 1
                if max_chars and size > max_chars:
                    break
            text = "\n".join(parts)
        except Exception as e:  # pypdf raises many parser-specific errors
            raise PageError(f"Could not read PDF: {e}") from e
    else:
        text = trafilatura.extract(r.text, url=url) or ""

    if not text.strip():
        raise PageError("No readable text found (the page may need JavaScript; try browse_page).")
    return text


@tool
def fetch_page(url: str, max_chars: int = 8000) -> str:
    """Read a web page or PDF and return its main text. Use it on URLs from web_search."""
    try:
        return _truncate(_page_text(url, max_pdf_pages=30), max_chars)
    except PageError as e:
        return str(e)


@tool
def browse_page(url: str, max_chars: int = 8000) -> str:
    """Open a page in a headless browser, running its JavaScript, and return its main text.
    Slower than fetch_page; use it when fetch_page finds no readable text."""
    if not _is_public(url):
        return f"Refusing non-public or invalid address: {url}"
    if not _robots_allows(url):
        return f"robots.txt disallows fetching {url}"

    def guard(route):
        # Every sub-request (scripts, XHR, redirects) gets the same public-address check.
        if _is_public(route.request.url):
            route.continue_()
        else:
            route.abort()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(user_agent=USER_AGENT)
                page.route("**/*", guard)
                page.goto(url, timeout=TIMEOUT * 1000, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except PlaywrightError:
                    pass  # pages that keep polling never go idle; read what has rendered
                html = page.content()
            finally:
                browser.close()
    except PlaywrightError as e:
        return f"Browse failed: {e}"

    text = trafilatura.extract(html, url=url) or ""
    if not text.strip():
        return "No readable text found even after rendering."
    return _truncate(text, max_chars)


# SubQ Snap: a fast decision model that judges claims against a whole source in one call.
SUBQ_URL = "https://api.subq.ai/v1/systemone"
SUBQ_MODEL = "subq-snap-preview"
SUBQ_MAX_QUESTIONS = 16  # per request (API limit)
SUBQ_MAX_SOURCE_CHARS = 1_200_000  # ~300K tokens, well under the 393,216-token window
SUBQ_RETRIES = 4  # for 429, 5xx and network errors; 400 and 422 are never retried
MAX_CLAIMS = 64
MAX_CLAIM_CHARS = 1000
VERDICTS = {
    "supported": "The source states the claim or clearly implies it.",
    "contradicted": "The source states something incompatible with the claim.",
    "not_in_source": "The source does not address the claim.",
}


class SubQError(Exception):
    pass


def _subq_decide(state: str, questions: dict, key: str) -> dict:
    """One /v1/systemone call, retrying rate limits and server errors with exponential backoff."""
    body = {"model": SUBQ_MODEL, "state": state, "questions": questions}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    for attempt in range(SUBQ_RETRIES + 1):
        try:
            r = httpx.post(SUBQ_URL, json=body, headers=headers, timeout=120)
        except httpx.TransportError as e:
            if attempt == SUBQ_RETRIES:
                raise SubQError(f"network error: {e}") from e
        else:
            if r.status_code == 200:
                return r.json()
            if (r.status_code != 429 and r.status_code < 500) or attempt == SUBQ_RETRIES:
                try:
                    detail = r.json()["detail"]
                    msg = f"{detail.get('error_type')}: {detail.get('message')}"
                except (ValueError, KeyError, TypeError, AttributeError):
                    msg = r.text[:300]
                raise SubQError(f"HTTP {r.status_code} {msg}")
        time.sleep(2**attempt)
    raise SubQError("unreachable")


@tool
def verify_claims(claims: list[str], source_url: str) -> str:
    """Check up to 16 claims against the full text of one source page or PDF in a single fast
    call to SubQ Snap. Returns, per claim, supported / contradicted / not_in_source with a confidence."""
    key = os.environ.get("SUBQ_API_KEY", "").strip()
    if not key:
        return "SubQ is not configured (SUBQ_API_KEY is missing), so claims cannot be verified; check them with fetch_page instead."
    claims = [c.strip()[:MAX_CLAIM_CHARS] for c in claims if c and c.strip()]
    if not claims:
        return "No claims given."
    dropped = max(0, len(claims) - MAX_CLAIMS)
    claims = claims[:MAX_CLAIMS]
    try:
        text = _page_text(source_url, max_chars=SUBQ_MAX_SOURCE_CHARS)
    except PageError as e:
        return f"Could not verify against {source_url}: {e}"
    truncated = len(text) > SUBQ_MAX_SOURCE_CHARS
    state = f"Source: {source_url}\n\n{text[:SUBQ_MAX_SOURCE_CHARS]}"

    lines, tokens = [], 0
    for start in range(0, len(claims), SUBQ_MAX_QUESTIONS):
        batch = {
            f"claim_{i}": {
                "type": "choice",
                "instructions": (
                    "Judge this claim only against the source text, not against outside knowledge. "
                    f'Claim: "{claim}"'
                ),
                "criteria": VERDICTS,
            }
            for i, claim in enumerate(claims[start:start + SUBQ_MAX_QUESTIONS], start + 1)
        }
        try:
            result = _subq_decide(state, batch, key)
        except SubQError as e:
            lines.append(f"Claims {start + 1}-{start + len(batch)}: SubQ call failed ({e}).")
            continue
        tokens += (result.get("usage") or {}).get("input_tokens", 0)
        answers = result.get("answers") or {}
        for name in batch:
            i = int(name.split("_")[1])
            a = answers.get(name) or {}
            verdict = a.get("choice", "unknown")
            conf = a.get("confidence")
            if conf is None:
                conf = (a.get("probabilities") or {}).get(verdict) or 0.0
            lines.append(f"{i}. {verdict} ({conf:.2f}) — {_truncate(claims[i - 1], 200)}")

    note = f", truncated to the first {SUBQ_MAX_SOURCE_CHARS:,}" if truncated else ""
    head = f"Claims checked against {source_url} ({len(text):,} characters of source{note}; {tokens:,} SubQ input tokens):"
    tail = [f"({dropped} claims beyond the first {MAX_CLAIMS} were not checked.)"] if dropped else []
    return "\n".join([head, *lines, *tail])


def _truncate(text: str, max_chars: int) -> str:
    return text[:max_chars] + ("\n…[truncated]" if len(text) > max_chars else "")
