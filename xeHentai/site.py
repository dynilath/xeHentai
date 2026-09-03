#!/usr/bin/env python
# coding:utf-8
"""Single home for every e-hentai / exhentai site URL literal.

Host names, scheme+host constants, page URLs and the host-bearing regex
patterns live here. The rest of the codebase must reference these constants
instead of spelling out site URLs inline, so a host change is a one-file edit.

The regex patterns below are preserved byte-for-byte as they historically
were spelled: they are matched against user-pasted and crawled URLs, so
"cleaning them up" (escaping, sub-domain handling) would silently change
matching behaviour.
"""

# ---- hosts (scheme-less) ----
DOMAIN_EHENTAI = "e-hentai.org"
DOMAIN_EXHENTAI = "exhentai.org"
DOMAIN_FORUMS_EHENTAI = "forums." + DOMAIN_EHENTAI

# ---- hosts (with scheme) ----
HOST_EHENTAI = "https://" + DOMAIN_EHENTAI
HOST_EXHENTAI = "https://" + DOMAIN_EXHENTAI
HOST_FORUMS_EHENTAI = "https://" + DOMAIN_FORUMS_EHENTAI

# ---- specific pages ----
LOGIN_URL = HOST_FORUMS_EHENTAI + "/index.php?act=Login&CODE=01"

# Substring of the 509 "bandwidth limit exceeded" placeholder image URL,
# matched against page bodies / final URLs (see filters.py, request_wrapper.py).
# Deliberately without the "e-"/"ex-" prefix so mirror hosts also match.
QUOTA_509_GIF_FRAGMENT = "hentai.org/img/509.gif"

# ---- example URLs shown in the WebUI as input hints ----
EXAMPLE_GALLERY_EHENTAI = HOST_EHENTAI + "/g/1234567/abc123def4/"
EXAMPLE_GALLERY_EXHENTAI = HOST_EXHENTAI + "/g/7654321/fed321cba0/"
EXAMPLE_GALLERY_SUBSCRIPTION = HOST_EHENTAI + "/g/1234567/somehash/"

# ---- host-bearing regex patterns (keep byte-for-byte) ----
# any site webpage URL (sub-domain tolerant), used to tell webpage from image
RE_STR_WEBPAGE = r'^https*:\/\/([^\.]+\.)*(?:[g\.]*e-|ex)hentai.org'
# restrict task URLs to site gallery pages (usually used via "%s" formatting)
RESTR_SITE = r"https?:\/\/(?:e-|ex)hentai\.org"
# exhentai-only prefix: such URLs are rejected when the user is not logged in
RE_STR_EXHENTAI_PREFIX = r"^https*://exhentai\.org"
# e-hentai prefix with the rest captured, used to migrate a task to exhentai
RE_STR_EHENTAI_MIGRATE = r"(?:https*://[g\.]*e\-hentai\.org)(.+)"
