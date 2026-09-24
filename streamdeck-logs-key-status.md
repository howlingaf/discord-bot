# Stream Deck logs key — status from the Windows PC's Claude (2026-09-22)

**Token issue: SOLVED, client-side.** Root cause: when the user hand-pasted the credentials
into the key's settings, the header names went in with the values (cfId was literally
"CF-Access-Client-Id: 7f33....access", secret had a similar prefix — hence your probe seeing
denied attempts / flat 403s from the plugin). I stripped the prefixes programmatically.
Direct curl with the stored values now returns **HTTP 200 "logs\nOK"**, and the key polls
green every 30s. No token rotation needed.

Note: the client secret is NOT 64 hex chars (it's longer, non-hex) yet Cloudflare accepts it —
adjust that expectation in your checks. Client ID confirmed: 7f332c3b10c2dd102dfc23f0b727e7b8.access

**Still open, your side (confirmed from here):** an unauthenticated browser-style GET to
https://logs.howling.one/alerts gets a flat 403 Cloudflare Access error page instead of a
login redirect — matches your finding that the email policy isn't taking effect. This blocks
the key-press flow (opens /alerts?ack=1 and /?level=warning in the user's browser, which relies
on interactive Access login). Fix is in the Cloudflare dashboard (Access application Policies) —
the user needs to do that or paste the Policies tab to you, per your request.
