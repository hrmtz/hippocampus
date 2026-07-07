"""Shared ingest DB + auth helpers (single SoT).

get_conn / the Anthropic key precedence were byte-duplicated across
diary / summarize / extract_facts; hoisted here so a change to the connect
contract or key resolution lands in one place.
"""
import os

import psycopg2
from pgvector.psycopg2 import register_vector

from ..config import Settings


def get_conn():
    """psycopg2 connection through the Settings boundary (actionable ConfigError
    on a missing PG_URL, not a bare KeyError), pgvector registered, with a
    connect timeout so a hung server fails instead of blocking forever."""
    conn = psycopg2.connect(Settings.load().pg_url, connect_timeout=10)
    register_vector(conn)
    return conn


def resolve_anthropic_key() -> str | None:
    """Anthropic key with the ingest precedence: dedicated ingest key, then the
    Cloudflare-gateway key, then the generic key. None if none is set."""
    return (os.environ.get('ANTHROPIC_API_KEY_INGEST')
            or os.environ.get('CF_ANTHROPIC_API_KEY')
            or os.environ.get('ANTHROPIC_API_KEY'))
