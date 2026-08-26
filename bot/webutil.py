"""Bits shared by every module that scrapes a problem site."""

import re
from html import unescape

# Codeforces and a few others 403 a default aiohttp user agent.
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120 Safari/537.36")


def strip_tags(html: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", html)).strip()
