"""In personal tables every column not on the plain allow-list is bytea (encrypted)."""
import re
from importlib import resources

PERSONAL = {"statements", "stmt_lines", "advice"}
PLAIN = {"id", "person", "uploaded_at", "status", "period_from", "period_to", "n_lines", "checked", "statement_id",
         "card_id", "section", "txn_date", "subject", "facts_hmac", "at"}


def columns(table):
    sql = resources.files("cards").joinpath("schema.sql").read_text()
    body = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", sql, re.S).group(1)
    for line in body.splitlines():
        m = re.match(r"\s+([a-z_]+)\s+([a-z]+)", line)
        if m and m.group(1) not in ("primary", "unique"):
            yield m.group(1), m.group(2)


def test_personal_values_are_encrypted():
    for t in PERSONAL:
        for col, typ in columns(t):
            assert col in PLAIN or typ == "bytea", f"{t}.{col} must be bytea (encrypted) or on the plain allow-list"
