# Authentication — the `tradesmart` Python SDK

How the Python SDK logs you in, where it keeps the token and what it guarantees. API access on
the TradeSmart web platform, the OAuth flow, the checksum and the 06:00 IST token rule are the
same for every SDK and are described in [API access and login](../../docs/api-access.md).

## The flow in the SDK

| Step of the flow | SDK |
|---|---|
| Authorise: open the login URL | `client.login_url()` or `build_authorize_url(app_key)` (the key is URL-quoted; the base is `ClientConfig.authorize_url`) |
| Checksum: SHA-256 of App Key + Secret Key + code | `compute_checksum(app_key, secret_key, code)` (all three must be non-empty, otherwise `ConfigurationError`) |
| Exchange: `POST /GenAcsTok` | `client.generate_session(code)`: builds the checksum, sends the call, parses the result into an `AccessToken` and stores it on the client |
| Use: `Authorization: Bearer <access_token>` | every later client call and the WebSocket login |

The worked checksum example from TradeSmart's documentation, *illustrative:*

```python
from tradesmart import compute_checksum

assert compute_checksum("ABC", "123", "x1y2z3") == (
    "7b482d7b380a3067eaba4c9c909b19253c4fa0edb5833e246401b8497c99a9c3"
)
```

## Logging in from the command line (your own account)

For your own trading account the SDK does the whole flow for you, once per trading day:

```bash
python -m tradesmart login
```

It asks for the Client ID, App Key (shown as App ID on the API Dashboard) and Secret Key (the
keys without echo), or reads `TRADESMART_CLIENT_ID`, `TRADESMART_APP_KEY` and
`TRADESMART_SECRET_KEY`. It opens the login URL in the browser, receives the `code` on
`http://127.0.0.1:8080/callback` (the Redirect URL to register), exchanges it once and saves the
token. It never prints the token.

**Account check:** TradeSmart's OAuth has no `state` parameter, so the command compares the `uid`
and `actid` of the returned token with the Client ID you entered and refuses, saving nothing, when
they differ.

### Daily login: the App Key and Secret Key are saved

The first time, type the App Key and Secret Key at the prompts. After a **successful** login the
command saves what you typed (or gave with `--app-key`) for that Client ID, so every later day is
just:

```bash
python -m tradesmart login --client-id YOUR_CLIENT_ID   # then log in in the browser
```

- **Where:** `credentials.json` in the same folder as the token file (see
  [Where the token is saved](#where-the-token-is-saved); with `--token-file PATH`, beside that
  file). One entry per Client ID, so several accounts on one computer each keep their own keys;
  saving one account's keys keeps the others.
- **Which keys win:** for each key, `--app-key` / `TRADESMART_APP_KEY` and `TRADESMART_SECRET_KEY`
  first, then the keys saved for that Client ID, then the prompt. Keys taken from the environment
  variables are never saved (they are already stored there). The command prints which keys it
  used from the file, but never the keys themselves.
- **Not saving:** `python -m tradesmart login --no-save-keys`.
- **Protection:** the file is written and checked like the token file (replaced atomically). On
  Linux, macOS and in Docker it is owner-only and a file readable by others is refused. On a
  Windows computer the permission bits are not enforced: the file relies on your user profile
  folder, which other non-administrator users cannot read. It is **not encrypted**: anyone who
  can read that folder, or the Docker volume it lives in, can read the keys. If that is not
  acceptable, use `--no-save-keys` and the prompt or the environment variables.
- **Two logins at once:** the file is not locked, so two logins at the same time that both save
  keys may keep only one of the new entries; the other login asks for its keys again next time.
- **Keys changed on the API Dashboard?** A login with outdated saved keys fails (for example
  with a checksum or app key error); the command then says the saved keys were used and shows the
  command to forget them. Run it and log in again; you are asked for the new keys.
- **A damaged or unknown `credentials.json`** stops the login with a message naming the file
  (never its content); remove it with `python -m tradesmart logout --forget-keys`.

**Logging out** (`--token-file PATH` works with each, as for login):

| Command | Removes |
|---|---|
| `python -m tradesmart logout` | the saved login (token) only; the saved keys stay |
| `python -m tradesmart logout --forget-keys --client-id YOUR_CLIENT_ID` | the token and that Client ID's saved keys |
| `python -m tradesmart logout --forget-keys` | the token and the saved keys of every Client ID (it lists them) |

In Docker (from the SDK repository's `sdk-python` folder) prefix the same commands with
`docker compose run --rm dev`, for example
`docker compose run --rm dev python -m tradesmart logout --forget-keys --client-id YOUR_CLIENT_ID`.

### No browser, another port, or Docker

**No browser, or the redirect does not arrive** (a server you reach over SSH, port 8080 already
in use, or a different Redirect URL): run `python -m tradesmart login --no-browser`. It prints the
login URL; open it in a browser on any device and log in. The browser then tries to open
`http://127.0.0.1:8080/callback?code=...`; on another device that page fails to load, which is
expected. Copy the whole address from the address bar (or just the `code` value) and paste it
into the terminal. The printed URL contains your App Key (as `client_id`, the way TradeSmart's
login page expects it): do not paste it into shared logs, chats or tickets. The code works once
and only for a short time: if the login fails, run the
command again. Without `--no-browser` the command falls back to the same prompt by itself when no
redirect arrives within 120 seconds.

**Another port:** pass `--port N`; the Redirect URL must then be `http://127.0.0.1:N/callback`.

**Docker, from a clone of the SDK repository:** in its `sdk-python` folder, run (with your own
Client ID):

```bash
docker compose run --rm --service-ports login --client-id YOUR_CLIENT_ID
```

No browser can open inside the container. Open the URL it prints in your browser and log in;
the login then completes by itself. The warning about `0.0.0.0` is expected here, because the
service publishes its port on your machine's `127.0.0.1` only. This service always uses port 8080
(`--port` does not change the published port). The token, and the keys you typed, are saved in a
Docker volume that the repository's other compose services (`dev`, `py311`, ...) mount, so
`from_env()` finds the token there and the next login finds the keys.

**Docker, your own image:** publish the port on the loopback only and let the command listen on
the container's address (IPv4 addresses only), for example
`docker run --rm -it -p 127.0.0.1:8080:8080 YOUR_IMAGE python -m tradesmart login --listen-host 0.0.0.0`,
and keep the token file on a volume.

### Where the token is saved

The token is saved to a per-user **token file**: `%APPDATA%\tradesmart\token.json` on Windows,
`~/.config/tradesmart/token.json` (or `$XDG_CONFIG_HOME/tradesmart/token.json`) on Linux and
macOS, or `TRADESMART_TOKEN_FILE` / `--token-file PATH` (made absolute). It is replaced
atomically on every login and created with mode `0600` (owner only); on Linux and macOS a file
owned by another user, or readable or writable by group or others, is refused. The stored expiry
never goes past the 06:00 IST rule, and a file whose `issued_at` lies more than 5 minutes ahead
of the computer's clock is refused (the message asks you to check this computer's clock and log in again).

**`from_env()` precedence** (clients and streams): Client ID = `TRADESMART_CLIENT_ID`, else the saved
login's. Token = `TRADESMART_ACCESS_TOKEN` (only together with `TRADESMART_CLIENT_ID`; a token without
a Client ID raises `ConfigurationError`), else the saved login's token when it was issued for the
same Client ID (an `AccessToken`, so the 06:00 IST check applies; an expired one raises
`AuthenticationError`). Else, for a client (not a stream), `TRADESMART_APP_KEY` +
`TRADESMART_SECRET_KEY` allow a programmatic `generate_session()`; a stream ignores
`TRADESMART_APP_KEY`.
`TradeSmart.from_env()` then uses it: no keys or tokens in your code. `python -m tradesmart
token-path` shows the file, `python -m tradesmart logout` deletes it. Step-by-step:
[Getting started](getting-started.md#3-log-in-once-a-day).

## Using the SDK in your own login flow

For a server that logs in other users (a platform app), run the flow yourself.

*Illustrative:*

```python
from tradesmart import AuthenticationError, TradeSmart

client = TradeSmart.from_env()   # TRADESMART_CLIENT_ID, TRADESMART_APP_KEY, TRADESMART_SECRET_KEY
url = client.login_url()
# ... user logs in; your Redirect URL handler receives `code` ...
try:
    token = client.generate_session(code)
except AuthenticationError as exc:   # e.g. a reused or wrong code
    print(exc.emsg or exc.message)
else:
    print(token.expires_at)          # never print token.access_token
```

A complete, runnable localhost receiver is `examples/auth_server_flow.py` in the SDK's source
repository (stdlib `http.server` bound to `127.0.0.1`, access log silenced because the request
line contains the code, prints only `expires_at`). When the token's `uid` or
`actid` is not `TRADESMART_CLIENT_ID` it stops and saves nothing. With the opt-in `--save-token PATH` it writes
the access token — and nothing else — to `PATH`, creating the file with owner-only permissions
(`0o600`); `examples/first_call.py --token-file PATH` then uses it.

SDK guarantees:

- `GenAcsTok` is **never retried** (the code is single-use). If the outcome is unknown — a
  timeout or dropped connection after sending, or a body that is not a JSON `stat` answer (e.g. a
  504 HTML gateway page) — it raises `AuthenticationError("GenAcsTok outcome unknown: the
  authorisation code is single-use; restart the login")` (`http_status` set when a response
  arrived): the code may already be consumed, so run the login again.
- A success answer the SDK cannot read (e.g. a missing `access_token`) also raises
  `AuthenticationError` telling you to restart the login, for the same reason.
- Every error answer from `GenAcsTok` (`stat: Not_Ok` or an HTTP error status) is raised as
  `AuthenticationError` (message `GenAcsTok failed: <emsg>`, or `GenAcsTok failed (no error
  message from the server)` when the answer has no usable `emsg`), without secrets. Provably-unsent
  failures keep their own types (`NetworkError`, `RequestTimeoutError` for connect failures):
  the code was not used, so you may try again with it.
- `app_key` and `secret_key` are required for `generate_session()` (otherwise
  `ConfigurationError`); `login_url()` needs `app_key`.
- The checksum, code, secret key and token never appear in logs, `repr()` or exception messages.

## Token lifetime in the SDK

The SDK applies TradeSmart's rule (valid until the next 06:00 IST; see
[Token lifetime](../../docs/api-access.md#token-lifetime)) as follows.

- `AccessToken.issued_at` is the time of the token exchange (IST); `expires_at` is computed by
  the SDK (`AccessToken.from_response(data, issued_at=None)`). `is_expired(now=None)` compares
  against it.
- With an `AccessToken` held by the client and `ClientConfig.check_token_expiry=True` (default),
  a call made after expiry raises `AuthenticationError` locally, without contacting the API.
  Set `check_token_expiry=False` to let the server decide (e.g. if the machine's clock is not
  reliable).
- A token given as a plain string (`access_token="..."`, `TRADESMART_ACCESS_TOKEN`) has no known
  issue time and is not checked locally; the server answers `Session Expired : Invalid Session Key`
  (→ `AuthenticationError`).
- A token is checked when it is set (constructor, `set_access_token()`, `from_env()`): a `str`
  token is stripped of surrounding whitespace (a token read from a file with its newline works);
  a token with a space, a control character or a non-ASCII character raises
  `ConfigurationError` (the value is never shown); a blank token counts as no token
  (`AuthenticationError` on the first call, nothing sent).
- The WebSocket stream checks an `AccessToken` (for example from `TradeSmartStream.from_env()`
  or `from_client()`) before connecting, under the same `check_token_expiry` setting; a plain
  string token is not checked locally. A rejected login (`ak` with `s=Not_Ok`) is terminal.
- `expires_in` and `refresh_token` are kept as-is on the `AccessToken`. No refresh or logout
  endpoint is documented, so the SDK offers none: log in again after 06:00 IST.

## Keeping credentials safe

The [security rules](../../docs/api-access.md#security-rules) apply. In the SDK:

- The `tradesmart` loggers redact the token, code, checksum, secret key and app key; your own logs
  must not contain them either.
- `token.access_token` is a `SecretStr`: call `.get_secret_value()` only where the raw value is
  needed.
- Keep credentials in the environment variables `TRADESMART_CLIENT_ID`, `TRADESMART_ACCESS_TOKEN`,
  `TRADESMART_APP_KEY` and `TRADESMART_SECRET_KEY` (or the saved login) rather than in code.
- The login command's saved keys (`credentials.json`) are owner-only but not encrypted; forget
  them with `python -m tradesmart logout --forget-keys` on a shared or retired computer.
