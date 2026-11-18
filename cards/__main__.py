"""python -m cards <web|migrate|seed|catalogue|ingest|notify|wealthfolio|import-env>"""
import argparse
import sys

from . import __version__, db


def main():
    ap = argparse.ArgumentParser(prog="cards", description=f"cards {__version__}")
    ap.add_argument("cmd", choices=["web", "migrate", "seed", "catalogue", "ingest", "notify", "wealthfolio", "import-env"])
    ap.add_argument("mode", nargs="?", default="summary", help="notify: summary | alerts")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--dry-run", action="store_true", help="catalogue: fetch + extract, write nothing")
    ap.add_argument("--only", help="catalogue: one source (wiki, milelion, mainlymiles, images, links, tc, awards, seats, matches)")
    ap.add_argument("--redo-images", action="store_true", help="catalogue: re-pick every searched card picture")
    ap.add_argument("--every", type=int, metavar="MINUTES", help="ingest: keep running, once every so many minutes")
    ap.add_argument("--card", help="catalogue --only tc: one card id")
    ap.add_argument("--force", action="store_true", help="catalogue --only tc: re-read documents that have not changed")
    ap.add_argument("--person", metavar="ID", help="wealthfolio: only this person (also when their import is switched off)")
    ap.add_argument("--since", metavar="YYYY-MM-DD", help="wealthfolio --person ID: first read that person's history from this date")
    ap.add_argument("--all", action="store_true", help="wealthfolio --person ID: first read everything Wealthfolio has")
    ap.add_argument("--admin", metavar="ID", help="import-env: also make this existing person an admin")
    a = ap.parse_args()
    if a.cmd in ("web", "migrate", "seed"):
        print(f"schema v{db.migrate()}", flush=True)
    if a.cmd == "seed":
        from . import seed
        with db.connect() as conn, db.startup_lock(conn):
            seed.seed(conn)
        from . import people
        who = ", ".join(p for p, _ in people.configured())
        print(f"seeded; people from the environment: {who or 'none (the first person to sign in is added)'}", flush=True)
    elif a.cmd == "web":
        from . import web
        web.serve(a.port)
    elif a.cmd == "notify":
        from . import notify
        db.migrate()
        sys.exit(notify.run(a.mode))
    elif a.cmd == "ingest":
        from . import ingest
        db.migrate()
        if not a.every:
            sys.exit(ingest.run())
        import time
        while True:   # the compose file's way of running it without cron
            try:
                ingest.run()
            except Exception as ex:
                print(f"ingest: {type(ex).__name__}", flush=True)
            time.sleep(max(1, a.every) * 60)
    elif a.cmd == "wealthfolio":
        from . import wealthfolio
        db.migrate()
        since = None
        if a.since:
            import datetime
            try:
                since = datetime.date.fromisoformat(a.since)
            except ValueError:
                sys.exit("--since wants a date like 2025-01-31")
        if (a.since or a.all) and not a.person:
            sys.exit("--since and --all need --person ID")
        sys.exit(wealthfolio.run(a.person, since, history_too=bool(a.since or a.all)))
    elif a.cmd == "import-env":
        from . import config
        db.migrate()
        with db.connect() as conn:
            for line in config.import_env(conn):
                print(line, flush=True)
            if a.admin:
                n = conn.execute("UPDATE people SET admin = true WHERE id = %s", (a.admin,)).rowcount
                conn.commit()
                print(f"{a.admin}: admin" if n else f"{a.admin}: no such person, nothing changed", flush=True)
    elif a.cmd == "catalogue":
        import os
        from . import catalogue
        if a.redo_images:
            os.environ["IMAGES_REDO"] = "1"
        if a.card:
            os.environ["TC_ONLY"] = a.card
        if a.force:
            os.environ["TC_FORCE"] = "1"
        db.migrate()
        sys.exit(catalogue.run(dry_run=a.dry_run, only=a.only))


main()
