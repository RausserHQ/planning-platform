"""Explicit lifecycle schema migration; Windmill job startup never runs DDL."""

from __future__ import annotations

import os

from planning_platform.publication_journal import PostgresPublicationJournal

from .dedupe import PostgresDeliveryDeduplicator
from .store import PostgresLifecycleStore


def main() -> None:
    database_url = os.environ.get("PLANNING_LIFECYCLE_DATABASE_URL")
    if not database_url:
        raise SystemExit("PLANNING_LIFECYCLE_DATABASE_URL is required")
    dedupe = PostgresDeliveryDeduplicator(database_url)
    store = PostgresLifecycleStore(database_url)
    publication = PostgresPublicationJournal(database_url)
    dedupe.setup()
    store.setup()
    publication.setup()
    ready = dedupe.ready() and store.ready() and publication.ready()
    publication.close()
    if not ready:
        raise SystemExit("planning lifecycle schema is not ready")


if __name__ == "__main__":
    main()
