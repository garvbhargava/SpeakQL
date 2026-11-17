"""Seed two demo organisations, each attached to its own warehouse.

Run by bootstrap.sh after the databases and roles exist. Safe to run again:
every row is found before it is created.

    Northwind Group   northwind.co     owner garv@northwind.co     northwind_dw
    Harbor Supply     harborsupply.co  owner owner@harborsupply.co harbor_dw

Two, because the most important property of this system -- that a question
runs on the warehouse it was about and on no other -- cannot be demonstrated
with one. Harbor's data is deliberately different from Northwind's
(sql/22_second_tenant.sql), so a cross-tenant read would be visible.

Uses the application's own modules rather than raw SQL, so the registry this
writes is exactly the one the API would write -- including the per-connection
engines, which means this script exercises the tenant fix on every bootstrap.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.config import load  # noqa: E402
from db.engines import Engines  # noqa: E402
from db.entities import Connection, Organisation, Person, SchemaColumn  # noqa: E402
from db.introspect import introspect  # noqa: E402
from db.session import SessionFactory  # noqa: E402

DEMO = [
    {
        "org": "Northwind Group", "domain": "northwind.co",
        "owner": "garv@northwind.co",
        "connection": "northwind_sales", "database": "northwind_dw",
    },
    {
        "org": "Harbor Supply", "domain": "harborsupply.co",
        "owner": "owner@harborsupply.co",
        "connection": "harbor_sales", "database": "harbor_dw",
    },
]

# What a viewer may read. Everything else stays private until an owner says
# otherwise -- defaulting to public would mean a viewer could read a column the
# day it was added.
PUBLIC = {
    "regions": {"region_id", "region_name"},
    "orders": {"order_id", "order_date", "amount", "customer_id"},
    "customers": {"customer_id", "region_id"},
    "shipments": {"shipment_id", "shipped_on", "carrier", "units"},
}


def main() -> None:
    settings = load()
    engines = Engines(settings)
    sessions = SessionFactory(engines.meta)

    try:
        with sessions.begin() as session:
            for demo in DEMO:
                org = session.scalar(select(Organisation).where(
                    Organisation.domain == demo["domain"],
                    Organisation.kind == "company",
                ))
                if org is None:
                    org = Organisation(name=demo["org"], kind="company",
                                       domain=demo["domain"])
                    session.add(org)
                    session.flush()

                owner = session.scalar(select(Person).where(
                    Person.email == demo["owner"]
                ))
                if owner is None:
                    session.add(Person(email=demo["owner"], org_id=org.id,
                                       product_role="owner", state="active"))

                connection = session.scalar(select(Connection).where(
                    Connection.org_id == org.id,
                    Connection.name == demo["connection"],
                ))
                if connection is None:
                    connection = Connection(
                        org_id=org.id, name=demo["connection"], kind="internal",
                        database_name=demo["database"],
                    )
                    session.add(connection)
                    session.flush()

                # This connection's own read engine -- the tenant fix, used.
                result = introspect(session, engines.tenants.read(connection),
                                    connection.id, only_schemas=("public",))

                for column in session.scalars(select(SchemaColumn).where(
                    SchemaColumn.connection_id == connection.id
                )):
                    column.is_public = (
                        column.column_name in PUBLIC.get(column.table_name, set())
                    )

                print(f"  {demo['org']:<16} {demo['connection']:<16} "
                      f"-> {demo['database']:<13} {result.summary}")
    finally:
        engines.dispose()


if __name__ == "__main__":
    main()
