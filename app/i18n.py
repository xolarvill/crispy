"""Bilingual support for the server-rendered legacy dashboard.

The legacy console was written in English and renders through Jinja templates.
Rather than wrapping every literal in a translation call, the locale middleware
rewrites rendered HTML in place when Chinese is selected. A small client-side
observer applies the same dictionary to text that JavaScript inserts later.

English stays the default. The active locale comes from ``?lang=`` first, then
the ``crispy.locale`` cookie written by the language switch.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextvars import ContextVar
from functools import lru_cache
from html import escape, unescape
from typing import Literal

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.i18n_legacy_zh import LEGACY_ZH, LEGACY_ZH_PATTERNS

Locale = Literal["en", "zh"]
LOCALE_COOKIE = "crispy.locale"

_locale: ContextVar[Locale] = ContextVar("crispy_locale", default="en")


def normalize_locale(value: str | None) -> Locale:
    """Only an explicit Chinese preference selects Chinese; everything else is English."""
    return "zh" if value and value.strip().lower().startswith("zh") else "en"


def current_locale() -> Locale:
    return _locale.get()


def translate(value: str, locale: Locale | None = None) -> str:
    """Translate one exact UI string, falling back to the English source."""
    active = locale or current_locale()
    if active != "zh" or not value:
        return value
    translated = LEGACY_ZH.get(value)
    if translated is not None:
        return translated
    for pattern, replacement in LEGACY_ZH_PATTERNS:
        if pattern.fullmatch(value):
            return pattern.sub(replacement, value)
    return value


# Blocks whose contents must never be rewritten: code, user data and scripts.
_PROTECTED_RE = re.compile(
    r"<!--[\s\S]*?-->"
    r"|<(script|style|pre|code|textarea|template)\b[^>]*>[\s\S]*?</\1\s*>",
    re.IGNORECASE,
)
_TEXT_NODE_RE = re.compile(r">([^<>]+)<")
_ATTRIBUTE_RE = re.compile(
    r"\b(title|placeholder|aria-label|alt)=([\"'])(.*?)\2",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"^(\s*)([\s\S]*?)(\s*)$")


def _translate_value(value: str, *, attribute: bool = False) -> str:
    match = _WHITESPACE_RE.match(value)
    if not match:
        return value
    lead, body, trail = match.groups()
    if not body:
        return value
    plain = unescape(body)
    translated = LEGACY_ZH.get(plain)
    if translated is None:
        for pattern, replacement in LEGACY_ZH_PATTERNS:
            if pattern.fullmatch(plain):
                translated = pattern.sub(replacement, plain)
                break
    if translated is None:
        return value
    # Chinese needs no entity escaping, but translations keep ASCII punctuation
    # such as ``&``; escape for safe insertion into HTML text and attributes.
    escaped = escape(translated, quote=attribute)
    return f"{lead}{escaped}{trail}"


def _translate_segment(segment: str) -> str:
    segment = _TEXT_NODE_RE.sub(
        lambda match: ">" + _translate_value(match.group(1)) + "<", segment
    )
    segment = _ATTRIBUTE_RE.sub(
        lambda match: (
            f"{match.group(1)}={match.group(2)}"
            f"{_translate_value(match.group(3), attribute=True)}"
            f"{match.group(2)}"
        ),
        segment,
    )
    return segment


def translate_html(html: str, locale: Locale | None = None) -> str:
    """Translate server-rendered HTML without touching scripts, styles or code."""
    active = locale or current_locale()
    if active != "zh" or not html:
        return html
    parts: list[str] = []
    cursor = 0
    for match in _PROTECTED_RE.finditer(html):
        parts.append(_translate_segment(html[cursor : match.start()]))
        parts.append(match.group(0))
        cursor = match.end()
    parts.append(_translate_segment(html[cursor:]))
    return "".join(parts)


@lru_cache(maxsize=1)
def i18n_script() -> str:
    """Serve the dictionary to the browser for content inserted after load."""
    payload = {
        "dict": LEGACY_ZH,
        "patterns": [[pattern.pattern, replacement] for pattern, replacement in LEGACY_ZH_PATTERNS],
    }
    return "window.CRISPY_I18N = " + json.dumps(payload, ensure_ascii=False) + ";"


@lru_cache(maxsize=1)
def i18n_version() -> str:
    """Stable cache-busting token that changes whenever the dictionary changes."""
    payload = json.dumps(
        {
            "dict": LEGACY_ZH,
            "patterns": [[pattern.pattern, replacement] for pattern, replacement in LEGACY_ZH_PATTERNS],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


class LegacyLocaleMiddleware(BaseHTTPMiddleware):
    """Resolve the dashboard locale and translate HTML responses for Chinese."""

    async def dispatch(self, request: Request, call_next):
        requested = request.query_params.get("lang") or request.cookies.get(LOCALE_COOKIE)
        locale = normalize_locale(requested)
        token = _locale.set(locale)
        try:
            response = await call_next(request)
            content_type = response.headers.get("content-type", "").split(";")[0].strip()
            if locale != "zh" or response.status_code != 200 or content_type != "text/html":
                return response
            body = b"".join([chunk async for chunk in response.body_iterator])
            translated = translate_html(body.decode("utf-8", "replace"), locale)
            headers = [
                (key, value)
                for key, value in response.raw_headers
                if key.lower() not in (b"content-length", b"transfer-encoding")
            ]
            out = Response(
                content=translated,
                status_code=response.status_code,
                media_type="text/html",
                background=response.background,
            )
            out.raw_headers = headers + [
                (b"content-length", str(len(out.body)).encode("ascii"))
            ]
            return out
        finally:
            _locale.reset(token)
