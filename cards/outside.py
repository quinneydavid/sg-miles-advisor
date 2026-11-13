"""The optional outside services the scheduled runs keep fresh: match results (fixtures.py) and each person's
KrisFlyer balance from AwardWallet (points.py). Each one is off without its key, asks at most every few hours, and a
failure of one never stops the run or the other. The seat scan is not here: it is weekly, with the catalogue run."""


def refresh(conn):
    """-> {"match days": n, "awardwallet": n} for what was fetched this time (absent = nothing to do)."""
    from . import fixtures, points
    out = {}
    try:
        n = fixtures.refresh(conn)
        if n is not None:
            out["match days"] = n
    except Exception as ex:
        conn.rollback()
        print(f"match results: {type(ex).__name__}", flush=True)
    try:
        n = points.aw_refresh_all(conn)
        if n:
            out["awardwallet"] = n
    except Exception as ex:
        conn.rollback()
        print(f"awardwallet: {type(ex).__name__}", flush=True)
    return out
