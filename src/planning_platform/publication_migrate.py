"""Explicit publication-journal migration command; application startup never migrates."""

from __future__ import annotations

import os

from .publication_journal import PostgresPublicationJournal


def main() -> None:
    database_url = os.environ.get("PLANNING_LIFECYCLE_DATABASE_URL")
    if not database_url:
        raise SystemExit("PLANNING_LIFECYCLE_DATABASE_URL is required")
    journal = PostgresPublicationJournal(database_url)
    journal.setup()
    if not journal.ready():
        raise SystemExit("publication journal schema is not ready")


if __name__ == "__main__":
    main()
