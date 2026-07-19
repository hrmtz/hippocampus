"""Company multi-user roster provisioning (epic #75 Phase 5 seed helper).

Seeds the identity tables and creates one PostgreSQL LOGIN role per contributor,
mapped to that principal via org.users.db_role, with exactly the grant bundle the
operator-mode read/write path needs. Codifies the staging `roles.sql` recipe that
the Slice 3/4 isolation tests were proven against.

Per user it ensures, idempotently:
  - org.tenants / org.teams / org.team_memberships rows;
  - a LOGIN role (fresh random password on create, or on --rotate);
  - the org.users row with db_role mapping (the write-identity trigger + the
    share/unshare definer functions resolve identity through this);
  - grants: SELECT+INSERT on personal.conversations/messages, SELECT on the read
    join tables (topic_clusters / conversation_segments / extracted_facts), USAGE
    on personal.messages_id_seq, EXECUTE on share_conversation/unshare_conversation.

The per-user wrapper DSN (with the generated password) is written ONLY to a 0600
env file for that user's Claude Desktop wrapper — never printed to stdout/logs.
The trigger is the real (tenant,user) binding; this helper just makes session_user
resolvable and least-privilege.

Usage:
  hippocampus roster provision --from roster.yaml [--out-dir DIR] [--rotate]
  hippocampus roster provision --tenant T --user U [--db-role R] [--teams a,b]
                               [--display-name NAME] [--email E] [--out-dir DIR]
  hippocampus roster list
  hippocampus roster disable --tenant T --user U     # offboard (sets disabled_at)

PG_URL must be the admin/superuser DSN for the company database.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import secrets as _secrets
from pathlib import Path

import psycopg2
from psycopg2 import sql

READ_JOIN_TABLES = ("topic_clusters", "conversation_segments", "extracted_facts")


def _admin_conn():
    url = os.environ.get("PG_URL", "")
    if not url:
        print("PG_URL not set — need the admin/superuser DSN for the company DB.",
              file=sys.stderr)
        raise SystemExit(2)
    conn = psycopg2.connect(url, connect_timeout=10)
    conn.autocommit = True
    return conn, url


def _default_db_role(user_id: str) -> str:
    return f"{user_id}_login"


def _role_exists(cur, role: str) -> bool:
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    return cur.fetchone() is not None


def _ensure_role(cur, role: str, *, rotate: bool) -> str | None:
    """Create the LOGIN role (or rotate its password). Returns the new password,
    or None when the role already existed and --rotate was not given."""
    exists = _role_exists(cur, role)
    if exists and not rotate:
        return None
    password = _secrets.token_urlsafe(24)
    verb = "ALTER" if exists else "CREATE"
    cur.execute(sql.SQL(verb + " ROLE {} WITH LOGIN PASSWORD {}").format(
        sql.Identifier(role), sql.Literal(password)))
    return password


def _grant_bundle(cur, role: str) -> None:
    ident = sql.Identifier(role)
    stmts = [
        "GRANT USAGE ON SCHEMA personal TO {}",
        "GRANT SELECT, INSERT ON personal.conversations, personal.messages TO {}",
        ("GRANT SELECT ON personal.topic_clusters, "
         "personal.conversation_segments, personal.extracted_facts TO {}"),
        "GRANT USAGE ON SEQUENCE personal.messages_id_seq TO {}",
        ("GRANT EXECUTE ON FUNCTION "
         "personal.share_conversation(text, text, text, text) TO {}"),
        "GRANT EXECUTE ON FUNCTION personal.unshare_conversation(text, text) TO {}",
    ]
    for s in stmts:
        cur.execute(sql.SQL(s).format(ident))


def _ensure_tenant(cur, tenant: str) -> None:
    cur.execute("INSERT INTO org.tenants (tenant_id) VALUES (%s) "
                "ON CONFLICT DO NOTHING", (tenant,))


def _ensure_team(cur, tenant: str, team: str) -> None:
    cur.execute("INSERT INTO org.teams (tenant_id, team_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING", (tenant, team))


def _upsert_user(cur, tenant: str, user_id: str, db_role: str,
                 display_name: str | None, email: str | None) -> None:
    cur.execute(
        "INSERT INTO org.users (tenant_id, user_id, db_role, display_name, email) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (tenant_id, user_id) DO UPDATE SET "
        "  db_role = EXCLUDED.db_role, "
        "  display_name = COALESCE(EXCLUDED.display_name, org.users.display_name), "
        "  email = COALESCE(EXCLUDED.email, org.users.email)",
        (tenant, user_id, db_role, display_name, email))


def _ensure_membership(cur, tenant: str, team: str, user_id: str) -> None:
    cur.execute(
        "INSERT INTO org.team_memberships (tenant_id, team_id, user_id) "
        "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (tenant, team, user_id))


def _derive_dsn(admin_url: str, role: str, password: str) -> str:
    parts = urllib.parse.urlsplit(admin_url)
    host = parts.hostname or "localhost"
    netloc = f"{role}:{urllib.parse.quote(password, safe='')}@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urllib.parse.urlunsplit(
        ("postgresql", netloc, parts.path or "", "", ""))


def _write_wrapper_env(out_dir: str, tenant: str, user_id: str,
                       teams: list[str], dsn: str) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{tenant}__{user_id}.env"
    body = (
        "# hippocampus per-user wrapper env — distribute to THIS user's machine only.\n"
        "# Contains a DB password. Keep mode 0600, never commit or paste in chat.\n"
        "HIPPOCAMPUS_MULTIUSER=1\n"
        f"HIPPOCAMPUS_TENANT_ID={tenant}\n"
        f"HIPPOCAMPUS_USER_ID={user_id}\n"
        f"HIPPOCAMPUS_TEAM_IDS={','.join(teams)}\n"
        f"PG_URL={dsn}\n"
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(body)
    return path


def _load_roster(path: str) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    if path.endswith((".yaml", ".yml")):
        import yaml  # noqa: PLC0415
        return yaml.safe_load(text)
    return json.loads(text)


def _provision_users(conn, admin_url: str, tenant: str, teams: list[str],
                     users: list[dict], *, out_dir: str, rotate: bool) -> int:
    written, skipped = 0, 0
    with conn.cursor() as cur:
        _ensure_tenant(cur, tenant)
        for team in teams:
            _ensure_team(cur, tenant, team)
        for u in users:
            user_id = str(u["user_id"]).strip()
            db_role = str(u.get("db_role") or _default_db_role(user_id)).strip()
            u_teams = [str(t).strip() for t in (u.get("teams") or [])]
            for team in u_teams:              # a user's team must exist as a team
                _ensure_team(cur, tenant, team)
            password = _ensure_role(cur, db_role, rotate=rotate)
            _upsert_user(cur, tenant, user_id, db_role,
                         u.get("display_name"), u.get("email"))
            for team in u_teams:
                _ensure_membership(cur, tenant, team, user_id)
            _grant_bundle(cur, db_role)
            if password is None:
                print(f"  {user_id} -> role {db_role}: exists (grants re-applied; "
                      "use --rotate to reset password + emit DSN)")
                skipped += 1
                continue
            dsn = _derive_dsn(admin_url, db_role, password)
            path = _write_wrapper_env(out_dir, tenant, user_id, u_teams, dsn)
            print(f"  {user_id} -> role {db_role}: provisioned, "
                  f"wrapper env {path} (0600, password inside)")
            written += 1
    print(f"roster provision done: tenant={tenant} provisioned={written} "
          f"existing={skipped} teams={len(teams)}")
    return 0


def _cmd_provision(args: argparse.Namespace) -> int:
    if args.from_file:
        roster = _load_roster(args.from_file)
        tenant = str(roster["tenant"]).strip()
        teams = [str(t).strip() for t in (roster.get("teams") or [])]
        users = roster.get("users") or []
    else:
        if not (args.tenant and args.user):
            print("provide --from FILE, or both --tenant and --user",
                  file=sys.stderr)
            return 2
        tenant = args.tenant.strip()
        teams = [t.strip() for t in (args.teams or "").split(",") if t.strip()]
        users = [{
            "user_id": args.user, "db_role": args.db_role,
            "display_name": args.display_name, "email": args.email,
            "teams": teams,
        }]
    if not users:
        print("roster has no users", file=sys.stderr)
        return 2
    conn, admin_url = _admin_conn()
    try:
        return _provision_users(conn, admin_url, tenant, teams, users,
                                out_dir=args.out_dir, rotate=args.rotate)
    finally:
        conn.close()


def _cmd_list(_args: argparse.Namespace) -> int:
    conn, _ = _admin_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT tenant_id, user_id, db_role, "
                        "COALESCE(display_name,''), disabled_at IS NOT NULL "
                        "FROM org.users ORDER BY tenant_id, user_id")
            rows = cur.fetchall()
            if not rows:
                print("no users provisioned")
                return 0
            print(f"{'tenant':<12} {'user':<16} {'db_role':<18} {'name':<16} status")
            for tenant, user, role, name, disabled in rows:
                print(f"{tenant:<12} {user:<16} {role:<18} {name:<16} "
                      f"{'DISABLED' if disabled else 'active'}")
            cur.execute("SELECT tenant_id, team_id, count(*) FROM org.team_memberships "
                        "GROUP BY 1,2 ORDER BY 1,2")
            teams = cur.fetchall()
            if teams:
                print("\nteams:")
                for tenant, team, n in teams:
                    print(f"  {tenant}/{team}: {n} member(s)")
    finally:
        conn.close()
    return 0


def _cmd_disable(args: argparse.Namespace) -> int:
    if not (args.tenant and args.user):
        print("disable needs --tenant and --user", file=sys.stderr)
        return 2
    conn, _ = _admin_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE org.users SET disabled_at = now() "
                        "WHERE tenant_id = %s AND user_id = %s AND disabled_at IS NULL",
                        (args.tenant, args.user))
            if cur.rowcount == 0:
                print("no matching active user (already disabled or absent)")
                return 1
        print(f"disabled {args.tenant}/{args.user}. The write-identity trigger and "
              "share functions now refuse this principal; consider "
              f"`ALTER ROLE ... NOLOGIN` on the login role for defense in depth.")
    finally:
        conn.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hippocampus roster",
        description="Company multi-user roster provisioning (epic #75).")
    sub = p.add_subparsers(dest="sub", required=True)

    pv = sub.add_parser("provision", help="create/seed tenants, users, roles, grants")
    pv.add_argument("--from", dest="from_file", metavar="FILE",
                    help="roster YAML/JSON (tenant, teams, users[])")
    pv.add_argument("--tenant")
    pv.add_argument("--user")
    pv.add_argument("--db-role", help="default: <user>_login")
    pv.add_argument("--teams", help="comma-separated team ids for the single user")
    pv.add_argument("--display-name")
    pv.add_argument("--email")
    pv.add_argument("--out-dir", default="./roster-wrappers",
                    help="dir for per-user 0600 wrapper env files (default ./roster-wrappers)")
    pv.add_argument("--rotate", action="store_true",
                    help="reset the password of already-existing roles + re-emit DSN")
    pv.set_defaults(func=_cmd_provision)

    ls = sub.add_parser("list", help="show provisioned tenants/users/teams (no secrets)")
    ls.set_defaults(func=_cmd_list)

    ds = sub.add_parser("disable", help="offboard a user (sets org.users.disabled_at)")
    ds.add_argument("--tenant")
    ds.add_argument("--user")
    ds.set_defaults(func=_cmd_disable)
    return p


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
