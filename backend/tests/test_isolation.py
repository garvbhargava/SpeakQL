"""Tenant isolation, credentials and the overlay -- without a database server.

Every test here pins down a bug the first version of this backend actually
had, or the fix for one:

    one shared read engine, so a question ran on whichever warehouse the DSN
    named rather than the one it was about          -> tenant routing tests
    a password that could move the connection to another host,
    and a host that could resolve somewhere else a second time -> host tests
    the API connecting to its own database as a superuser      -> config tests

The Postgres half of the story -- that the roles really cannot do what they
must not -- is tests/test_privileges.py, which needs the container.
"""

from __future__ import annotations

import json
import socket
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url

from app import config
from core import executor
from core.executor import ExecutionError, OverlaySpec, rewrite_for_overlay
from core.validator import Permitted, validate
from db.crypto import CredentialError, seal, unseal
from db.host_guard import HostRefusal, HostRefused, HostVerdict, external_url, pin
from db.tenant_engine import ConnectionUnavailable, TenantEngines, WriteNotSupported

KEY = "test-only-secret-key"


def _settings(**overrides):
    base = dict(
        env="test", secret_key=KEY,
        meta_dsn="postgresql+psycopg://speakql_app:a@db:5432/speakql_meta",
        ro_dsn="postgresql+psycopg://speakql_ro:r@db:5432/northwind_dw",
        write_dsn="postgresql+psycopg://speakql_write:w@db:5432/northwind_dw",
        edits_dsn="postgresql+psycopg://speakql_edits_rw:e@db:5432/northwind_dw",
        uploads_dsn="postgresql+psycopg://speakql_upload_ddl:u@db:5432/speakql_uploads",
        llm_mode="local", llm_model="gemma3:4b", llm_endpoint="http://llm:11434",
        confidence_threshold=0.55, free_email_mode="personal_workspace",
        statement_timeout_ms=10_000, max_rows=5_000, invite_ttl_hours=168,
        rate_ask_per_hour=60, rate_upload_per_day=20, rate_export_per_hour=30,
        rate_codes_per_address_hour=10, rate_codes_per_ip_hour=30,
    )
    base.update(overrides)
    return config.Settings(**base)


class _FakeEngine:
    def __init__(self, url, **options):
        self.url = make_url(url)
        self.options = options
        self.disposed = False

    def dispose(self):
        self.disposed = True


def _tenants() -> TenantEngines:
    return TenantEngines(_settings(), _FakeEngine)


def _connection(id_, kind, database_name, **extra):
    return SimpleNamespace(id=id_, org_id=extra.pop("org_id", id_), kind=kind,
                           database_name=database_name, host=extra.pop("host", None),
                           port=extra.pop("port", None),
                           secret_cipher=extra.pop("secret_cipher", None))


NORTHWIND = _connection(1, "internal", "northwind_dw")
HARBOR = _connection(2, "internal", "harbor_dw")
UPLOADS = _connection(3, "uploaded", "speakql_uploads")


# ====================================================== tenant routing ======

def test_each_warehouse_is_read_from_its_own_database():
    """THE isolation test. The first version would have returned the same
    engine, on northwind_dw, for both of these."""
    tenants = _tenants()
    northwind, harbor = tenants.read(NORTHWIND), tenants.read(HARBOR)

    assert northwind is not harbor
    assert northwind.url.database == "northwind_dw"
    assert harbor.url.database == "harbor_dw"


def test_the_role_never_changes_only_the_database():
    tenants = _tenants()
    assert tenants.read(HARBOR).url.username == "speakql_ro"
    assert tenants.write(HARBOR).url.username == "speakql_write"
    assert tenants.edits(HARBOR).url.username == "speakql_edits_rw"
    assert {tenants.write(HARBOR).url.database,
            tenants.edits(HARBOR).url.database} == {"harbor_dw"}


def test_reads_are_read_only_and_time_limited():
    options = _tenants().read(HARBOR).options
    assert options["read_only"] is True
    assert options["timeout_ms"] == 10_000


def test_an_upload_is_read_from_the_uploads_database_as_the_read_role():
    tenants = _tenants()
    engine = tenants.read(UPLOADS)
    assert engine.url.database == "speakql_uploads"
    assert engine.url.username == "speakql_ro", "never the DDL role on the read path"
    assert tenants.expected_database(UPLOADS) == "speakql_uploads"
    assert tenants.expected_database(HARBOR) == "harbor_dw"


def test_nothing_off_this_server_can_be_written():
    tenants = _tenants()
    external = _connection(9, "external", "sales")
    for connection in (UPLOADS, external):
        with pytest.raises(WriteNotSupported):
            tenants.write(connection)
        with pytest.raises(WriteNotSupported):
            tenants.edits(connection)


def test_engines_are_cached_per_connection_and_can_be_forgotten():
    tenants = _tenants()
    first = tenants.read(HARBOR)
    assert tenants.read(HARBOR) is first
    tenants.forget(HARBOR.id)
    assert first.disposed
    assert tenants.read(HARBOR) is not first


# ====================================== the identity check at execution =====

class _Server:
    """A connection that says which database it is on, and records what ran."""

    dialect = SimpleNamespace(name="postgresql")

    def __init__(self, database):
        self.database = database
        self.ran: list[str] = []

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement):
        sql = str(statement)
        self.ran.append(sql)
        if "current_database" in sql:
            return SimpleNamespace(scalar_one=lambda: self.database)
        raise AssertionError("the question ran")


def test_a_question_routed_to_the_wrong_database_is_refused_before_it_runs():
    """The second check. Even if routing were wrong, the executor asks the
    server where it is and refuses before the question's own SQL runs."""
    server = _Server("northwind_dw")
    permitted = Permitted(tables=frozenset({"public.regions"}))

    with pytest.raises(ExecutionError, match="wrong database"):
        executor.execute(server, "SELECT region_name FROM public.regions",
                         permitted, max_rows=10, expected_database="harbor_dw")

    assert server.ran == ["SELECT current_database()"]


# ======================================================= external hosts =====

def test_a_password_cannot_move_the_host():
    """The first version pasted the URL together. This password moved the
    connection to 127.0.0.1 -- a host check_host never saw."""
    password = "x@127.0.0.1:5432/harbor_dw?"

    pasted = make_url(
        f"postgresql+psycopg://reader:{password}@warehouse.example.com:5432/sales"
    )
    assert pasted.host == "127.0.0.1", "the bug, reproduced"

    built = external_url(username="reader", password=password,
                         host="warehouse.example.com", port=5432, database="sales")
    assert built.host == "warehouse.example.com"
    assert built.password == password
    assert built.database == "sales"


def test_the_connection_goes_to_the_address_that_was_judged():
    verdict = HostVerdict(True, resolved=("2606:2800:220:1::1", "93.184.216.34"))
    url = external_url(username="r", password="p", host="warehouse.example.com",
                       port=5432, database="sales")
    pinned = pin(url, verdict)
    assert pinned.query["hostaddr"] == "93.184.216.34", "IPv4 first"
    assert pinned.query["sslmode"] == "require"
    assert pinned.host == "warehouse.example.com", "kept for TLS"


def test_a_refused_host_cannot_be_pinned():
    url = external_url(username="r", password="p", host="internal.example.com",
                       port=5432, database="sales")
    with pytest.raises(HostRefused):
        pin(url, HostVerdict(False, HostRefusal.PRIVATE_RANGE, "10.0.0.5"))


def _external(host="warehouse.example.com", cipher=None):
    return _connection(
        7, "external", "sales", host=host, port=5432,
        secret_cipher=cipher if cipher is not None else seal(
            KEY, json.dumps({"username": "reader", "password": "p@ss/w?rd"})),
    )


def _resolves_to(monkeypatch, address):
    def fake(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake)


def test_an_external_warehouse_is_pinned_and_forced_onto_tls(monkeypatch):
    _resolves_to(monkeypatch, "93.184.216.34")
    engine = _tenants().read(_external())
    assert engine.url.query["hostaddr"] == "93.184.216.34"
    assert engine.url.query["sslmode"] == "require"
    assert engine.url.password == "p@ss/w?rd", "special characters survive intact"
    assert engine.url.database == "sales"


def test_a_host_that_later_resolves_inside_is_refused_at_connect(monkeypatch):
    """DNS rebinding. Registration saw a public address; today the name
    answers with a private one. The engine is never built."""
    _resolves_to(monkeypatch, "10.0.0.5")
    with pytest.raises(ConnectionUnavailable, match="address check"):
        _tenants().read(_external())


def test_unreadable_credentials_are_a_clear_failure_not_a_crash(monkeypatch):
    _resolves_to(monkeypatch, "93.184.216.34")
    with pytest.raises(ConnectionUnavailable, match="credentials"):
        _tenants().read(_external(cipher=b"not a fernet token"))


# ========================================================== credentials =====

def test_credentials_round_trip_and_are_not_stored_in_clear():
    cipher = seal(KEY, "reader:hunter2")
    assert b"hunter2" not in cipher
    assert unseal(KEY, cipher) == "reader:hunter2"


def test_a_different_key_cannot_read_them():
    with pytest.raises(CredentialError):
        unseal("a-rotated-key", seal(KEY, "reader:hunter2"))


def test_tampered_credentials_fail_rather_than_decrypt_to_something_else():
    cipher = bytearray(seal(KEY, "reader:hunter2"))
    cipher[-5] ^= 0x01
    with pytest.raises(CredentialError):
        unseal(KEY, bytes(cipher))


def test_missing_credentials_are_refused():
    with pytest.raises(CredentialError):
        unseal(KEY, b"")


# =============================================================== config =====

_RUNTIME = {
    "SECRET_KEY": KEY,
    "META_DSN": "postgresql+psycopg://speakql_app:a@db:5432/speakql_meta",
    "RO_DSN": "postgresql+psycopg://speakql_ro:r@db:5432/northwind_dw",
    "WRITE_DSN": "postgresql+psycopg://speakql_write:w@db:5432/northwind_dw",
    "EDITS_DSN": "postgresql+psycopg://speakql_edits_rw:e@db:5432/northwind_dw",
    "UPLOADS_DSN": "postgresql+psycopg://speakql_upload_ddl:u@db:5432/speakql_uploads",
}


def _environment(monkeypatch, **overrides):
    for name, value in {**_RUNTIME, **overrides}.items():
        monkeypatch.setenv(name, value)


def test_least_privilege_roles_are_accepted(monkeypatch):
    _environment(monkeypatch)
    assert config.load().meta_dsn.startswith("postgresql+psycopg://speakql_app:")


@pytest.mark.parametrize("variable,user", [
    ("META_DSN", "speakql_owner"),
    ("RO_DSN", "postgres"),
    ("UPLOADS_DSN", "SPEAKQL_OWNER"),
])
def test_the_api_refuses_to_start_as_a_superuser(monkeypatch, variable, user):
    """The first version connected to its own database as speakql_owner --
    a superuser -- while its documentation said it never held those
    credentials. Now the process will not start that way."""
    _environment(monkeypatch, **{variable: f"postgresql+psycopg://{user}:p@ss@db/x"})
    with pytest.raises(config.ConfigError, match="superuser"):
        config.load()


# ============================================================== overlay =====

SHIPMENTS = OverlaySpec(
    overlay_table='"member_edits"."p42_shipments"',
    pk_column="shipment_id",
    columns=(("shipment_id", "bigint"), ("carrier", "text"), ("units", "integer")),
    corrected=frozenset({"units"}),
)


def test_the_overlay_replaces_only_the_corrected_table_and_keeps_its_alias():
    sql = ("SELECT s.carrier, SUM(s.units) AS units FROM public.shipments AS s "
           "JOIN public.regions AS r ON r.region_id = s.shipment_id "
           "GROUP BY s.carrier")
    rewritten, overlaid = rewrite_for_overlay(sql, {"shipments": SHIPMENTS})

    assert overlaid == ("shipments",)
    assert '"member_edits"."p42_shipments"' in rewritten
    # The member's pending value where there is one, the real value otherwise.
    assert ("COALESCE(CAST(NULLIF(o_units.after_value, '') AS INT), s.\"units\") "
            "AS \"units\"") in rewritten
    assert 's."carrier"' in rewritten and "o_carrier" not in rewritten, \
        "an uncorrected column is read straight through"
    assert ") AS s" in rewritten, "the alias survives, so s.units still resolves"
    assert "public.regions AS r" in rewritten, "an uncorrected table is untouched"


def test_an_unaliased_table_is_overlaid_under_its_own_name():
    rewritten, _ = rewrite_for_overlay("SELECT carrier FROM shipments",
                                       {"shipments": SHIPMENTS})
    assert ") AS shipments" in rewritten


def test_no_overlay_means_the_statement_is_untouched():
    sql = "SELECT carrier FROM public.shipments"
    assert rewrite_for_overlay(sql, {}) == (sql, ())


def test_nobody_can_name_the_overlay_schema_directly():
    """The overlay is the executor's substitution, never a statement anybody
    supplied. Another member's pending cells are one schema away -- the
    validator refuses the reach."""
    permitted = Permitted(tables=frozenset({"public.shipments"}))
    verdict = validate("SELECT * FROM member_edits.p42_shipments", permitted)
    assert not verdict.allowed
