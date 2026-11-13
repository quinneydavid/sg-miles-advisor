#!/usr/bin/env python3
"""Retake the README screenshots (docs/screenshots/*.png) from a throwaway installation with invented data.

Starts a Postgres of its own in a temporary folder (pgserver), runs the app on 127.0.0.1 with one person and no
login, loads the demo household (seed.py), photographs the pages with a headless Chromium and removes everything
again. It never uses the database, key or settings named in the caller's environment.

    pip install -r requirements.txt pgserver playwright     # Python 3.9 to 3.12, in a virtualenv
    playwright install chromium                             # or pass --chrome /path/to/chrome
    python tools/demo/screenshots.py

With --searx URL (or SEARX_URL) the app's own picture search (`catalogue --only images`) runs first, so the cards show
real pictures instead of drawn ones. The address is a SearXNG /search endpoint with JSON output; only card names are
sent to it, it is given to that one run only and is not stored, so no page shows it. Look at the pictures it chose:
without a model to check them, a wrong one can get through.

Not part of the app and not in the Docker image."""
import argparse
import base64
import io
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "screenshots"
DESKTOP, PHONE = (1280, 800), (390, 844)
# file name, path, viewport, tallest it may get in CSS pixels (longer pages are cut there)
SHOTS = [
    ("pay", "/?q=FairPrice&pay=mobile", DESKTOP, 1250),
    ("pay-phone", "/?q=Shopee&pay=online", PHONE, 1500),
    ("wallet", "/wallet", DESKTOP, 1900),
    ("spend", "/spend", DESKTOP, 1500),
    ("transactions", "/spend/lines", DESKTOP, 1250),
    ("offers", "/offers", DESKTOP, 1250),
    ("settings", "/admin", DESKTOP, 1700),
]
LIMIT = 400_000   # bytes; a bigger picture is saved again with a palette of 256 colours or fewer


def clean_env(port, uri):
    """The caller's environment without anything that could point the app at a real installation."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CARDS_", "PG", "DATABASE_", "VAULT_", "LLM_", "TELEGRAM_", "CF_", "SEARX_"))}
    env.update(DATABASE_URL=uri, CARDS_LOCAL_KEY=base64.b64encode(os.urandom(32)).decode(), CARDS_SINGLE_USER="alex",
               CARDS_PUBLIC_PORT=str(port + 1), TZ="Asia/Singapore", PYTHONDONTWRITEBYTECODE="1")
    return env


def wait_for(url, seconds=30):
    end = time.time() + seconds
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return
        except OSError:
            time.sleep(0.3)
    sys.exit(f"the app did not start at {url}")


def shoot(base, chrome, only):
    from playwright.sync_api import sync_playwright
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chrome or None)
        for name, path, (w, h), tallest in SHOTS:
            if only and name not in only:
                continue
            ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=2, color_scheme="light",
                                      locale="en-SG", timezone_id="Asia/Singapore", is_mobile=w < 500, has_touch=w < 500)
            page = ctx.new_page()
            page.goto(base + path, wait_until="networkidle")
            page.evaluate("document.activeElement && document.activeElement.blur()")
            # the page ends under its last line of text, not at the bottom of the window
            end = page.evaluate("Math.ceil(document.querySelector('main').lastElementChild.getBoundingClientRect().bottom + scrollY)")
            height = end + (96 if w < 500 else 32)                 # a phone keeps its tab bar at the bottom
            if height > tallest:                                   # too long: cut under the last whole row that fits
                height = page.evaluate(
                    "t => Math.max(0, ...[...document.querySelectorAll('main .row, main .tx, main .panel, main form')]"
                    ".map(x => Math.ceil(x.getBoundingClientRect().bottom + scrollY)).filter(b => b <= t))", tallest) or tallest
            page.set_viewport_size({"width": w, "height": height})
            data = page.screenshot(clip={"x": 0, "y": 0, "width": w, "height": height})
            ctx.close()
            if len(data) > LIMIT:
                from PIL import Image
                full = Image.open(io.BytesIO(data)).convert("RGB")
                for colours in (256, 128, 64):
                    small = io.BytesIO()
                    full.quantize(colours, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(small, "PNG", optimize=True)
                    data = small.getvalue()
                    if len(data) <= LIMIT:
                        break
            (OUT / f"{name}.png").write_bytes(data)
            print(f"{name}.png  {len(data) // 1000} kB")
        browser.close()


def main():
    ap = argparse.ArgumentParser(description="Retake the README screenshots from a throwaway installation.")
    ap.add_argument("--port", type=int, default=18080, help="the app listens on 127.0.0.1:PORT (and PORT + 1)")
    ap.add_argument("--chrome", default=os.environ.get("CHROME"), help="a Chromium or Chrome to use instead of Playwright's own")
    ap.add_argument("--searx", default=os.environ.get("SEARX_URL"), metavar="URL",
                    help="a SearXNG /search endpoint: find real card pictures with the app's own search first")
    ap.add_argument("--only", nargs="*", help="only these pictures, by name")
    ap.add_argument("--keep", action="store_true", help="leave the app running afterwards, to look around (Ctrl-C stops it)")
    args = ap.parse_args()
    import pgserver
    tmp = tempfile.mkdtemp(prefix="cards-demo-")
    web = None
    try:
        srv = pgserver.get_server(os.path.join(tmp, "pg"))
        env = clean_env(args.port, srv.get_uri())
        subprocess.run([sys.executable, "-m", "cards", "seed"], cwd=ROOT, env=env, check=True)
        web = subprocess.Popen([sys.executable, "-m", "cards", "web", "--port", str(args.port)], cwd=ROOT, env=env)
        base = f"http://127.0.0.1:{args.port}"
        wait_for(base + "/healthz")
        if web.poll() is not None:   # something else answered: this run's app did not get the port
            sys.exit(f"port {args.port} is taken; pass another with --port")
        subprocess.run([sys.executable, str(pathlib.Path(__file__).with_name("seed.py")), "--url", base, "--throwaway"], check=True)
        if args.searx:   # in this one run's environment only: the app never stores or shows the address
            subprocess.run([sys.executable, "-m", "cards", "catalogue", "--only", "images"], cwd=ROOT,
                           env=dict(env, SEARX_URL=args.searx))
            time.sleep(6)    # the running app looks for catalogue changes every 5 seconds
        shoot(base, args.chrome, args.only)
        if args.keep:
            print(f"the demo is on {base} until Ctrl-C")
            web.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if web:
            web.terminate()
            web.wait(timeout=10)
        try:
            srv.cleanup()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
