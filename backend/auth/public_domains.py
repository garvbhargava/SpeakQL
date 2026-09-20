"""Free-mail domains (Backend Plan §7.2).

Used only to decide *which signup path* an address takes -- never to refuse
anyone outright. A free-mail address gets a personal workspace, which is a
real, usable tenant of one.

The list does not need to be exhaustive. A free-mail domain we fail to
recognise becomes a company organisation, which is wrong but not dangerous:
that person owns it and nobody else can reach it without an invitation. The
dangerous direction -- a shared tenant -- is impossible either way, because a
company organisation is created by its first member and joined only by people
who can receive mail at that domain.
"""

from __future__ import annotations

FREE_MAIL_DOMAINS: frozenset[str] = frozenset({
    # the big four
    "gmail.com", "googlemail.com",
    "outlook.com", "hotmail.com", "live.com", "msn.com",
    "yahoo.com", "yahoo.co.in", "yahoo.co.uk", "ymail.com", "rocketmail.com",
    "icloud.com", "me.com", "mac.com",
    # privacy-oriented
    "proton.me", "protonmail.com", "pm.me", "tutanota.com", "tuta.io",
    "fastmail.com", "hushmail.com",
    # regional, common in India
    "rediffmail.com", "sify.com", "indiatimes.com",
    # other widely used
    "aol.com", "gmx.com", "gmx.net", "mail.com", "zoho.com", "yandex.com",
    "inbox.com", "mail.ru",
    # disposable -- these should never become a company
    "mailinator.com", "guerrillamail.com", "10minutemail.com",
    "temp-mail.org", "throwawaymail.com", "yopmail.com", "trashmail.com",
})

# Some providers use many country suffixes. Matching the stem catches them
# without listing every one.
_FREE_STEMS: tuple[str, ...] = ("yahoo.", "hotmail.", "live.", "outlook.")


def is_free_mail(domain: str) -> bool:
    domain = (domain or "").strip().lower().lstrip("@")
    if not domain:
        return False
    if domain in FREE_MAIL_DOMAINS:
        return True
    return any(domain.startswith(stem) for stem in _FREE_STEMS)


def is_disposable(domain: str) -> bool:
    """Reported to the owner on the People screen rather than blocked. A
    disposable address in a company is a governance question, not a security
    one -- the grant still decides what they can reach."""
    domain = (domain or "").strip().lower()
    return domain in {
        "mailinator.com", "guerrillamail.com", "10minutemail.com",
        "temp-mail.org", "throwawaymail.com", "yopmail.com", "trashmail.com",
    }
