"""Which organisation does this email belong to? (Backend Plan §7.2)

This file is small and it is the most security-sensitive code in the project.

SpeakQL identifies a company by its email domain. If a free-mail domain were
ever allowed to resolve like a company domain, `resolve('gmail.com')` would
return one organisation and **every Gmail user in the world would land inside
it**, reading each other's warehouses. That is the worst failure this system
can have.

The guarantee, and it is structural rather than procedural:

    A personal workspace is stored with `domain` NULL, and the resolver asks
        WHERE domain = ? AND kind = 'company'
    A NULL never equals anything. So a personal workspace cannot be reached by
    domain -- not by a bug, not by a crafted address, not ever.

Two people signing up with gmail.com get two organisations, not one shared
tenant. The only way a second person enters one is an invitation issued from
inside it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from auth.public_domains import is_free_mail
from db.entities import Organisation

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


class FreeEmailMode(str, Enum):
    PERSONAL_WORKSPACE = "personal_workspace"
    INVITE_ONLY = "invite_only"
    BLOCKED = "blocked"


class SignupPath(str, Enum):
    JOIN_EXISTING = "join_existing"        # the domain is already registered
    CREATE_COMPANY = "create_company"      # first person from this domain
    PERSONAL_WORKSPACE = "personal_workspace"
    INVITE_REQUIRED = "invite_required"
    REFUSED = "refused"


@dataclass(frozen=True)
class Resolution:
    path: SignupPath
    organisation: Organisation | None = None
    domain: str | None = None
    reason: str = ""


def normalise(email: str) -> str:
    return (email or "").strip().lower()


def is_valid_email(email: str) -> bool:
    return bool(_EMAIL.match(normalise(email)))


def domain_of(email: str) -> str:
    return normalise(email).split("@")[-1]


def resolve_company(session: Session, domain: str) -> Organisation | None:
    """The only query that maps a domain to an organisation.

    Both conditions are load-bearing. `kind = 'company'` is belt to the null's
    braces: even if a personal row somehow acquired a domain, it would still
    not be returned here.
    """
    return session.scalar(
        select(Organisation).where(
            func.lower(Organisation.domain) == domain.lower(),
            Organisation.kind == "company",
        )
    )


def resolve(session: Session, email: str, mode: FreeEmailMode) -> Resolution:
    """Decide what happens when this address tries to sign in."""
    email = normalise(email)
    if not is_valid_email(email):
        return Resolution(SignupPath.REFUSED, reason="that is not an email address")

    domain = domain_of(email)

    # --- free mail ----------------------------------------------------------
    if is_free_mail(domain):
        if mode is FreeEmailMode.BLOCKED:
            return Resolution(
                SignupPath.REFUSED,
                reason="this deployment accepts work addresses only",
            )
        if mode is FreeEmailMode.INVITE_ONLY:
            return Resolution(
                SignupPath.INVITE_REQUIRED, domain=domain,
                reason="a free-mail address needs an invitation here",
            )
        # The default. A workspace of one, with no domain at all.
        return Resolution(
            SignupPath.PERSONAL_WORKSPACE, domain=None,
            reason=(
                f"{domain} is not a company, so this becomes a personal "
                "workspace. Nobody can join it by email domain, because it "
                "has no domain to match."
            ),
        )

    # --- a work address -----------------------------------------------------
    existing = resolve_company(session, domain)
    if existing is not None:
        return Resolution(
            SignupPath.JOIN_EXISTING, organisation=existing, domain=domain,
            reason=f"{existing.name} is already here. You will join as a member.",
        )

    return Resolution(
        SignupPath.CREATE_COMPANY, domain=domain,
        reason=f"No one from {domain} is here yet.",
    )


def assert_personal_is_unreachable(org: Organisation) -> None:
    """A tripwire, called wherever a personal workspace is created.

    If this ever raises, the isolation guarantee has been broken by a code
    change and the process should stop rather than carry on quietly.
    """
    if org.kind == "personal" and org.domain is not None:
        raise RuntimeError(
            f"organisation {org.id} is personal but carries domain "
            f"{org.domain!r}; domain resolution could match it. Refusing to "
            "continue -- see auth/domain_resolver.py."
        )
