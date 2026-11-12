"""Request-scoped FastAPI dependencies for the web server.

Settings, the pricing resolver and the template engine are immutable and built
once at startup (stored on ``app.state``); the SQLite connection is opened per
request and always closed.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates

from wdi.config import Settings
from wdi.metrics.pricing import PricingResolver


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_pricing(request: Request) -> PricingResolver:
    return request.app.state.pricing


def get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def get_db(
    settings: Annotated[Settings, Depends(get_settings)],
) -> Iterator[sqlite3.Connection]:
    from wdi.db.connection import connect

    conn = connect(settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


SettingsDep = Annotated["Settings", Depends(get_settings)]
PricingDep = Annotated["PricingResolver", Depends(get_pricing)]
TemplatesDep = Annotated["Jinja2Templates", Depends(get_templates)]
DbDep = Annotated[sqlite3.Connection, Depends(get_db)]
