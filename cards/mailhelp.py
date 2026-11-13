"""Help with the hard part of setup: getting bank mail into one folder the app may read.

  PROVIDERS            the mail providers the Settings page knows: server, how to make an app password, where its
                       rules live. Providers move their menus; the steps name the page to look for.
  senders(banks)       the mail domains of the person's banks (ingest.SENDER_DOMAINS); a bank with no known sender
                       is listed as unknown, never guessed.
  gmail_filters(...)   a Gmail filter file (Settings > Filters and Blocked Addresses > Import filters): one filter
                       per bank that labels its mail. Atom feed in the form Gmail itself exports.
  rules_text(...)      the same rules in words, for providers that cannot import a file.
"""
import datetime
from xml.sax.saxutils import escape, quoteattr

from .ingest import SENDER_DOMAINS

DEFAULT_FOLDER = "Cards"

# id: name, server, port, user-name hint, can the app sign in with an app password, steps to get one, where rules live
PROVIDERS = {
    "gmail": {
        "name": "Gmail", "host": "imap.gmail.com", "port": 993, "works": True, "import": "gmail",
        "user": "The full Gmail address.",
        "steps": [
            "Turn on 2-Step Verification for the Google account (myaccount.google.com, Security). App passwords "
            "exist only with it on.",
            "Open myaccount.google.com/apppasswords, name the password (for example \"cards\") and copy the 16 "
            "letters shown. The spaces do not matter.",
            "Paste it below as the app password. It only works for mail programs; the normal Google password is "
            "never needed here.",
            "Import the filter file below (Gmail on a computer: Settings, See all settings, Filters and Blocked "
            "Addresses, Import filters). Gmail creates the label itself.",
        ],
        "rules": "Settings, See all settings, Filters and Blocked Addresses",
    },
    "icloud": {
        "name": "iCloud Mail", "host": "imap.mail.me.com", "port": 993, "works": True, "import": None,
        "user": "The iCloud Mail address. If the sign-in is refused, try only the part before the @.",
        "steps": [
            "Two-factor authentication must be on for the Apple Account.",
            "Open account.apple.com, Sign-In and Security, App-Specific Passwords, and make one.",
            "Paste it below as the app password.",
            "Make the folder in iCloud Mail, then add one rule per bank on icloud.com (Mail, Settings, Rules) with the "
            "senders listed below.",
        ],
        "rules": "icloud.com, Mail, Settings, Rules",
    },
    "fastmail": {
        "name": "Fastmail", "host": "imap.fastmail.com", "port": 993, "works": True, "import": None,
        "user": "The full Fastmail address.",
        "steps": [
            "Open Settings, Privacy & Security, and under app passwords make a new one with IMAP access. Read-only "
            "access is enough.",
            "Paste it below as the app password.",
            "Make the folder, then add one rule per bank (Settings, Filters & Rules) with the senders listed below.",
        ],
        "rules": "Settings, Filters & Rules",
    },
    "yahoo": {
        "name": "Yahoo Mail", "host": "imap.mail.yahoo.com", "port": 993, "works": True, "import": None,
        "user": "The full Yahoo address.",
        "steps": [
            "Open the Yahoo account's Security page and choose Generate app password.",
            "Paste it below as the app password.",
            "Make the folder, then add one filter per bank (Settings, More Settings, Filters) with the senders listed "
            "below.",
        ],
        "rules": "Settings, More Settings, Filters",
    },
    "outlook": {
        "name": "Outlook.com / Hotmail", "host": "outlook.office365.com", "port": 993, "works": False, "import": None,
        "user": "The full Outlook.com address.",
        "steps": [
            "Outlook.com stopped accepting passwords and app passwords for IMAP in September 2024; it only allows a "
            "Microsoft sign-in window, which this app does not have. The mailbox cannot be read directly.",
            "Way round: keep the bank mail arriving at Outlook.com and forward it. Make a mailbox at a provider "
            "from this list, connect that one here, and add one Outlook.com rule per bank (Settings, Mail, Rules) that "
            "forwards mail from the senders listed below to it.",
            "Or upload statement PDFs on the Statements page, or drop them in the drop folder.",
        ],
        "rules": "Settings, Mail, Rules",
    },
    "other": {
        "name": "Another provider (IMAP)", "host": "", "port": 993, "works": True, "import": None,
        "user": "Usually the full mail address.",
        "steps": [
            "Find the provider's IMAP server name (port 993, TLS) and whether it wants an app password.",
            "Make the folder, then add one rule per bank with the senders listed below.",
        ],
        "rules": "the provider's rules or filters page",
    },
}

def senders(banks):
    """banks = [(id, name)] -> ([(id, name, [domains])] with known senders, [names without])."""
    known, unknown = [], []
    for bid, name in banks:
        if SENDER_DOMAINS.get(bid):
            known.append((bid, name, SENDER_DOMAINS[bid]))
        else:
            unknown.append(name)
    return known, unknown


def gmail_filters(banks, label=DEFAULT_FOLDER, skip_inbox=False, now=None):
    """The filter file for Gmail's "Import filters": one filter per bank, labelling mail from its domains.
    skip_inbox also archives the mail (it then shows only under the label). Returns XML text."""
    known, _ = senders(banks)
    now = (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    prop = lambda name, value: f"    <apps:property name={quoteattr(name)} value={quoteattr(value)}/>"
    out = ["<?xml version='1.0' encoding='UTF-8'?>",
           "<feed xmlns='http://www.w3.org/2005/Atom' xmlns:apps='http://schemas.google.com/apps/2006'>",
           "  <title>Mail Filters</title>",
           "  <id>tag:mail.google.com,2008:filters:</id>",
           f"  <updated>{now}</updated>",
           "  <author><name>sg-miles-advisor</name></author>"]
    for bid, name, domains in known:
        out += ["  <entry>",
                "    <category term='filter'></category>",
                f"    <title>Mail Filter: {escape(name)}</title>",
                "    <content></content>",
                prop("from", " OR ".join(domains)),
                prop("label", label)]
        if skip_inbox:
            out.append(prop("shouldArchive", "true"))
        out += [prop("shouldNeverSpam", "true"), "  </entry>"]
    out.append("</feed>")
    return "\n".join(out) + "\n"


def rules_text(banks, folder=DEFAULT_FOLDER):
    """The same rules in words: [(bank name, "from contains ... -> move to folder")]."""
    known, _ = senders(banks)
    return [(name, " or ".join("@" + d for d in domains), folder) for _, name, domains in known]
