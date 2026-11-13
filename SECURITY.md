# Security

## Reporting

Report a vulnerability through GitHub's private reporting ("Report a vulnerability" on the Security tab of the
repository). Please do not open a public issue for it.

Only the latest release is supported.

## What the app assumes

- Port 8080 trusts the `Remote-User` header. Only the signing-in proxy may reach it.
- `CARDS_SINGLE_USER` gives everyone who can reach port 8080 that person's data. It is for a private machine. In
  this mode the app refuses (421) a request whose `Host` is not `127.0.0.1`, `localhost`, `[::1]` or a name listed in
  `CARDS_HOSTS`, so a web page that points its own name at 127.0.0.1 (DNS rebinding) cannot read the app. `/healthz`
  is not checked.
- Port 8081 (the public door) is off unless `CF_ACCESS_AUD` is set. It relies on Cloudflare Access in front: it
  checks the audience and expiry of the Access token, not its signature. cloudflared must verify the token and be the
  only thing that can reach the port.
- Any signed-in person can set a Wealthfolio address, and the server then sends requests to it. That can be any host
  the server can reach, so only add people you trust.
- Personal data is encrypted before it is stored. The key that wraps the data keys (key file or Vault Transit key)
  must be kept apart from database backups.
- Statement text and Ask questions go to the configured model endpoint.
- Feed and bot tokens are bearer secrets, one per person. The app stores only their SHA-256.
- Mailbox app passwords, the model key and the Telegram bot token are stored encrypted and are sent only to the
  mail server, the model endpoint and Telegram. An admin can replace them but not read them.
- The first person to open Settings on a new installation becomes the admin. Open it before sharing the address.
- The mailbox is opened read-only. The app never sends, moves or deletes mail.
