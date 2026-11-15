# API access and login — TradeSmart API V2

How to get API access to a TradeSmart account, how a session is obtained, how long it lasts and
the security rules that apply. This page does not depend on the programming language; the Python guide's
[Authentication](../sdk-python/docs/authentication.md) page shows how the SDK's login works.
Source: TradeSmart's official "App Creation" and "Authentication & Login Flow" documentation
(https://apidocs.tradesmartonline.in/v2/).

API access needs a TradeSmart trading account. No account yet? [Open one online][open-account].

[open-account]: https://signup.tradesmartonline.in/?utm_source=PythonSDK&utm_medium=Organic&utm_campaign=SDK_Open_Demat&utm_content=api_access&channel=Organic&platform=web

## Credentials

| Credential | Issued by | Used for | Secret? |
|---|---|---|---|
| App Key | TradeSmart, at app registration (shown as **App ID** on the API Dashboard) | `client_id` query parameter of the login URL; checksum input | **Treat as private.** It is not a password (it appears in the login URL), but do not publish it; the SDK redacts it in logs |
| Secret Key | TradeSmart, at app registration | checksum input | **Yes — server-side only** |
| Client ID | your trading account | `uid` / `actid` in request bodies, WebSocket login | No |
| Redirect URL | you register it | receives the one-time `code` | Must be a URL **you control** |
| Static IP | you register it (IPv4 for now) | API access from that IP | Can be changed at most **2 times per day** (exchange rule) |

The SDK reads these from the environment variables `TRADESMART_CLIENT_ID`, `TRADESMART_APP_KEY`
and `TRADESMART_SECRET_KEY` (and an existing token from `TRADESMART_ACCESS_TOKEN`), so no key needs
to appear in your code.

## Where the credentials come from: the API Dashboard

API access is self-service. Log in to the TradeSmart web platform (`https://web.tradesmartonline.in`)
and open `https://web.tradesmartonline.in/api` (top-right user id, Tools, API): the **API
Dashboard**. "Generate New API Key" takes a Name, a Primary IP address (required), an optional
Secondary IP address and a **Redirect URL**, plus acceptance of the API terms. Each app is then listed
under Active Credentials with its **App ID** and a masked **Secret Key** (shown with "Show
Credentials"). **The App ID shown there is the App Key** of the SDK and of the official docs.

For local use, register the loopback Redirect URL `http://127.0.0.1:8080/callback`: the
dashboard accepts it, and an SDK's local login receives the code there on your own machine. If
an App Key or Secret Key leaks, remove the app on the dashboard, create a new one, update your
environment variables (or `.env` file) and log in again.

**IPv4 for now.** Register an IPv4 static IP. IPv6 clients must use a separate API domain, which
TradeSmart has not yet published.

## The flow

```
browser ──► login URL ──► TradeSmart login ──► redirect to your URL with ?code=...
                                                            │
your server: checksum = SHA-256(app_key + secret_key + code)│
             POST /GenAcsTok  jData={"code": ..., "checksum": ...}   (no Authorization header)
                                                            │
             ◄── {"stat":"Ok","access_token": ..., "uid": ..., ...}
             every later call:  Authorization: Bearer <access_token>
```

1. **Authorise.** Open
   `https://v2api.tradesmartonline.in/OAuthlogin/authorize/oauth?client_id=<APP_KEY>` in the user's
   browser (the App Key URL-quoted).
2. **Redirect.** After login TradeSmart redirects to the registered Redirect URL with a short-lived
   `code` in the query string. The code is valid for **a single** token exchange.
3. **Checksum.** Concatenate App Key + Secret Key + code with no separators and take the SHA-256
   hex digest.
4. **Exchange.** `POST {base_url}/GenAcsTok` with `jData={"code": "...", "checksum": "..."}` and
   **no** `Authorization` header. The answer carries the `access_token`.
5. **Use.** Every other REST call carries `Authorization: Bearer <access_token>`; the WebSocket
   login frame carries the same token as `accesstoken`.

TradeSmart's OAuth has no `state` parameter, so a login tool should check that the `uid` and
`actid` of the returned token match the Client ID it expected. Because the code is single-use,
the SDK never retries `GenAcsTok`: when its outcome is unknown (a timeout or a dropped connection
after sending, or an answer that is not a TradeSmart reply), start the login again.

### Worked checksum example (from TradeSmart's documentation)

| Input | Value |
|---|---|
| App Key | `ABC` |
| Secret Key | `123` |
| Code | `x1y2z3` |
| Combined string | `ABC123x1y2z3` |
| SHA-256 hex | `7b482d7b380a3067eaba4c9c909b19253c4fa0edb5833e246401b8497c99a9c3` |

## Token lifetime

**TradeSmart's rule:** an access token is **valid until the next 06:00 IST** (Asia/Kolkata, fixed
UTC+05:30, no DST) after it was issued.

| Issued at (IST) | Expires at |
|---|---|
| 05:59:59 on day D | 06:00 on day D |
| 06:00:00 on day D | 06:00 on day D+1 |
| 15:30:00 on day D | 06:00 on day D+1 |

- After expiry the server answers `Session Expired : Invalid Session Key`. The SDK can also check
  the expiry locally, before sending, for a token whose issue time they know.
- `expires_in` (it looks like an absolute epoch in TradeSmart's examples) and `refresh_token` are
  returned as-is. **No refresh or logout endpoint is documented**, so the SDK offers none: log in
  again after 06:00 IST, once per trading day.

## Security rules

1. **Exchange the code server-side only.** The Secret Key must never be shipped to a browser,
   mobile app or any other client a user controls.
2. **Own your Redirect URL.** Anyone who can read the redirect can obtain a token from the code.
   For local tools bind the receiver to `127.0.0.1` only.
3. **Never log or print** the token, code, checksum, secret key or app key. The SDK's loggers
   redact them; your own logs must not contain them either.
4. **Store tokens like passwords** (environment variables, a secrets manager, or a file only you
   can read; never in source control).
5. **Keep credentials in environment variables** rather than code.
6. **Static IP.** API access is tied to the registered static IP (IPv4 for now), which can be
   changed at most twice a day; plan deployments accordingly.
7. Sample tokens printed in TradeSmart's documentation are real-looking values — never copy them
   into code, tests or fixtures.
