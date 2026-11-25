# Live-order driver protocol

The live-order matrix (UAT and mock-session modes, with every gate and limit) exists once, in the
shared harness. A language SDK joins it through a **driver**: a small program that holds that
language's real SDK and executes the matrix's calls with it. This document is the protocol between
the harness and a driver. It is the same for every language.

A driver covers the **state-changing calls** the matrix makes: `place_order`, `place_sliced_order`,
`modify_order`, `cancel_order`, `exit_order`, `place_gtt`, `modify_gtt`, `cancel_gtt`, `place_oco`, `modify_oco` and `cancel_oco`. The protocol
also carries the read methods and `request`, so a driver can be tested in full, but the harness keeps
its own reads (order book, account match, limits check, quotes, instruments, teardown checks) on its
own Python client as an independent referee. The async-client and stream family of the matrix runs for
Python only and is skipped, with a reason, for every driver.

## Transport

- The harness starts the driver as a child process and talks to it in **JSON lines**: one JSON object
  per line, UTF-8, `\n` terminated, on the driver's **stdin** (requests) and **stdout** (the handshake
  and replies). Nothing else is ever written to stdout.
- **stderr** carries the driver's logs. They are redacted: never a token, secret key, checksum, auth
  code or account id.
- The driver handles **one request at a time**, in order, and answers each before reading the next.
- When stdin reaches end of file, the driver finishes any call in flight, writes its reply if it can,
  and **exits**. It never keeps running without the harness.
- The driver exits with a non-zero status on a protocol violation (a line that is not a JSON object,
  a request without `id` or `method`, an unknown method (the driver exits 1 with no reply), a last line
  without its newline). It never guesses.

## Start-up refusal (defence in depth)

The driver holds no gate of its own beyond this: it **refuses to start**, writing one line to stderr
and exiting non-zero before any handshake, unless all of these hold:

1. `TRADESMART_LIVE_DRIVER_RUN` is set to a non-empty per-run nonce. The harness creates it for each
   run and passes it only to the drivers it starts; a driver started by hand has none and refuses.
2. `TRADESMART_LIVE_TESTS=1` and `TRADESMART_LIVE_ORDER_TESTS=1` (both order opt-ins).

Every other gate (environment, mock-session date and typed confirmation, funded mode refused, account
match, limits check, pacing, circuit breakers, caps, the stop file) stays in the harness and is
re-checked before every place and modify. A driver never decides whether an order may be sent.

The driver builds its client from the same `TRADESMART_*` environment variables and the same saved
login as the SDK's own live tests, and never writes them anywhere.

## Handshake

The first line the driver writes, once its client is built:

```json
{"hello": {"sdk": "javascript", "version": "2.1.0", "environment": "uat", "client_id": "<id>", "run": "<nonce>", "protocol": 2}}
```

- `sdk`: the SDK's name (`python`, `javascript`), `version`: its version.
- `environment`: the environment the client was built for (`uat` or `production`, the
  `TRADESMART_ENV` name; `production` when it is unset).
- `client_id`: the account the client holds. This is the **one place** a driver writes an account
  id (see "What a driver must not do"). The harness compares it, ignoring letter case, with
  `TRADESMART_LIVE_ORDER_ACCOUNT` and with its own Python client's id, and refuses to start on a
  mismatch, before any request. The harness never records it.
- `run`: the nonce of `TRADESMART_LIVE_DRIVER_RUN`, echoed unchanged, so the harness knows the
  driver is the one it started for this run.
- `protocol`: the version of this document, `2` (version 2 added the OCO calls; a driver that reports a lower version is refused at start-up, before any order).

The harness sends nothing before the hello. A first line that is not this object, a missing or
different field (an unknown `sdk`, no `version`, another `environment`, `run` or `client_id`, a
`protocol` other than `2`), or no line within the reply timeout: the harness kills the driver's
process tree and refuses the run before any order.

## Request

```json
{"id": 7, "method": "cancel_order", "args": {"order_id": "240101000012345"}}
```

- `id`: an integer chosen by the harness, unique within the run; the reply echoes it.
- `method`: one contract method name in snake_case (as in the SDK behaviour spec).
- `args`: an object of the method's arguments by their contract (snake_case) names. Positional
  arguments are given by name too. An argument that is absent is not sent.
- **One request is exactly one SDK call.** The driver never retries, batches, splits or adds calls of
  its own, whatever the outcome. In particular it never repeats a place, modify, cancel, exit, GTT or OCO
  call, and never reads the order book to find out what happened.

Argument values use the **value encoding of the conformance vectors**: Decimals as strings, `null`
for "no value", dates `YYYY-MM-DD`, instants as ISO 8601 with the IST offset, times `HH:MM` in IST,
enums by their string value. A driver converts them to its language's own types before calling the SDK
(a price string becomes the SDK's exact decimal type, never a binary float).

The OCO calls (protocol 2) take the client's own keywords: `place_oco` and `modify_oco` carry
`exchange`, `tradingsymbol`, `transaction_type`, `quantity`, `product`, `stop_loss`, `target`,
`validity` and `alert_validity` (`place_oco` also an optional `tag`; `modify_oco` also `alert_id`);
`cancel_oco` carries `alert_id`. Each leg (`stop_loss`, `target`) is a JSON object
`{"trigger_value": "95", "price": "94.5", "order_type": "LMT", "trigger_price": "95"}` (prices as
strings). Their replies are those of the GTT calls: the alert id as `result`, the mandatory `raw` and
`http_status`, classified by the harness with the Python parser for `PlaceOCOOrder`,
`ModifyOCOOrder` or `CancelOCOOrder`.

## Reply

A call that returned:

```json
{"id": 7, "result": "240101000012345"}
```

`result` is the SDK's return value in the same value encoding: a string for an id, and for a model an
object of its contract field names with Decimals as strings, plus `raw` (the response body as the
SDK kept it). `place_sliced_order` returns its result object: `requested_quantity`,
`slice_quantities`, `order_ids`, `placed_quantity`, `unsent_quantity`, `complete`, `error` (the
error object below, or `null`) and the mandatory `slices`:

```json
"slices": [{"order_id": "240101000012345", "raw": {"stat": "Ok", "norenordno": "240101000012345"}, "http_status": 200}]
```

one entry per **placed** slice, in order, so `len(slices) == len(order_ids)`: the slice's order id,
its response body (`raw`, account-id keys removed as below) and its HTTP status (an integer or
`null`). The body of a slice that failed is not an entry: it stays in the error's `raw`. Every
`sliced_result` carries `slices`, the one returned and the partial one inside an error reply.

A reply to a state-changing call that returns an id (`place_order`, `modify_order`, `cancel_order`,
`exit_order`, `place_gtt`, `modify_gtt`, `cancel_gtt`, `place_oco`, `modify_oco`, `cancel_oco`) **must** also carry, beside `result`:

```json
{"id": 7, "result": "240101000012345", "raw": {"stat": "Ok", "norenordno": "240101000012345"}, "http_status": 200}
```

- `raw` (**mandatory**): the response body of that call as the SDK received it (parsed JSON), with
  the account-id keys (`client_id`, `user_id`, `account_id`, `uid`, `actid`) removed at any depth;
- `http_status`: its HTTP status, an integer, or `null`. Anywhere in a reply, an `http_status` that
  is present and not an integer (a string, a float, a boolean) is a broken reply, never read as
  `null`.

**The harness acts on the wire body, never on `result`.** It parses and classifies `raw` with the
Python SDK's own parser for that endpoint, exactly as the Python client would, and the id that parse
gives (the order id, the alert id) is the one the run records as its own and later cancels. `result`
is only compared with it. Each of these is an unknown outcome (`OrderStatusUnknownError`, CHECK THE
ACCOUNT, the driver stopped as for a broken reply below): a success reply without `raw`; a `raw` that
the Python parse refuses or does not classify as a success (`stat: Not_Ok`, a GTT word other than the
documented one, no id); a `result` id that differs from the parse of `raw` (also recorded as a
finding). The harness records `raw` with the request in the run's ledger (redacted there), as it
does for its own Python calls. A read's `result` is used as returned.

`place_sliced_order` makes several calls, so its result carries one wire body per placed slice
(`slices`). The harness parses each entry's `raw` as a `PlaceOrder` body in the same way, records
each, and builds `order_ids` from those parses, never from the driver's list. Each of these is an
unknown outcome (CHECK THE ACCOUNT, the driver stopped): `slices` missing, or not one entry per
order id; an entry without `raw` or `order_id`; an entry whose `raw` is not a success; an entry's
`order_id`, or `order_ids[i]`, that differs from its parse; a result the Python SDK could never
build, by the rules it plans and accounts slices with:

- the plan: `requested_quantity` is the `quantity` the harness sent, and `slice_quantities` are
  positive integers that add up to `requested_quantity`;
- the order budget (the harness reserved `max_slices` orders for the case): no more slices than
  the `max_slices` sent (the SDK default when none was sent), none above the `max_slice_quantity`
  sent when one was, and the shape `plan_slices` builds, all equal except a smaller last one;
- the accounting: `placed_quantity` is the sum of the placed slices' quantities, `unsent_quantity`
  is requested minus placed, `complete` is true exactly when every slice has an id, and no order id
  appears twice;
- a returned result: a complete result names no `error`, an incomplete one names its `error`
  (the slice's definite failure), and that error is never an `OrderStatusUnknownError` (an
  ambiguous slice is raised, with the partial result, never returned).

The same rules hold for the partial `sliced_result` inside an error reply, which is never complete
and carries no `error` of its own (the error reply is its error).

A call that raised:

```json
{"id": 7, "error": {"type": "APIError", "message": "...", "stat": "Not_Ok", "emsg": "...", "http_status": 200, "raw": {}}}
```

- `type`: the **contract error name** (`APIError`, `AuthenticationError`, `RateLimitError`,
  `NotFoundError`, `ServerError`, `OrderRejectedError`, `ResponseParseError`,
  `OrderStatusUnknownError`, `NetworkError`, `RequestTimeoutError`, `RequestValidationError`,
  `ConfigurationError`, `ValueError`), as listed in the conformance vectors. A driver maps its
  language's error to the contract name; an unknown error is reported as `OrderStatusUnknownError` for a
  state-changing call (the outcome is ambiguous), never as a success.
- `message`: the error's message.
- `raw`: the error's **wire body**, the response body as the SDK kept it on the error, filled from
  exactly one field per class: an `APIError` (and every subclass) its `raw`;
  `OrderStatusUnknownError` and `ResponseParseError` their `body` (the redacted text of at most
  2000 characters, `specifications/sdk-behaviour.md`), sent as the JSON value it parses to, or as
  the text itself when it does not parse; every other error `null`. As for a returned call, the
  account-id keys are removed from it at any depth. The harness maps it back the
  same way: `raw` becomes the Python `APIError.raw`, and the `body` of `OrderStatusUnknownError`
  and `ResponseParseError` (JSON text, or the text itself).
- `stat`: the error's own `stat` when its class has one; otherwise (no SDK error class has one
  today) `raw["stat"]` when `raw` is an object with a `stat`, else `null`. Never taken from the
  message.
- `emsg`, `http_status`: the error's own fields when its class has them (`emsg`: `APIError` and
  subclasses; `http_status`: `APIError`, `OrderStatusUnknownError`, `ResponseParseError`), else
  `null`.
- `sliced_result`: for `place_sliced_order` only, the partial result when a slice had a failed or
  unknown outcome (the same object as a returned result), else `null`. A `sliced_result` in an
  error of any other method is a broken reply.
- `fields`: for `RequestValidationError`, the sorted wire keys the SDK named, else an empty list.
- `sent`: for `RateLimitError` only: `"sent": false` when the SDK's own client-side limiter refused
  the call before anything went out, `"sent": true` (or absent) when the server answered 429. The harness
  decides "nothing was sent" for a driver from this field and the `type` alone, never from the
  message text. Anything but `true` or `false` is a broken reply, and so is `"sent": false` with
  an `http_status` that is not `null` (a server answered, so it was sent).

The `type`, never the message, is what the harness acts on. An ambiguous outcome
(`OrderStatusUnknownError`) is never turned into a rejection or a success by the driver.

## Reply timeout and broken replies

The harness waits for each reply for the **reply timeout**: the driver's configured connect and read
timeouts plus its order rate limiter's longest wait plus a margin of 14 s for the process itself. With
the SDK defaults (connect 5 s, read 10 s, `order_rate_limit_max_wait` 1 s) that is **30 seconds** per
SDK call; `place_sliced_order` waits that times (`max_slices` + 2), for its slices and its two reads.
The same timeout applies to the handshake. A driver must therefore keep the SDK's default timeouts
(or the harness must be given the longer value).

The request line is written **once**. When the reply does not come within the reply timeout, or it is
a partial line, not a JSON object, the answer to another `id` (out of order), without exactly one of
`result` and `error`, an `error` whose `type` is not a contract error name, a reply or an error
`raw` nested too deeply to read (whichever overflows first, on every Python version), or the
driver's output ends (the process exited): the harness

- raises `OrderStatusUnknownError` for a state-changing call (the call may have been executed; the
  matrix then reads the order book, never resends) and `NetworkError` for a read;
- kills the driver's whole process tree (it starts the driver in a process group of its own);
- stops that SDK's run: no further request reaches this driver (every later call is refused before
  anything is written), the matrix skips that SDK's remaining cases with the reason, and its ledger
  names the stop. A broken reply to a state-changing call is CHECK THE ACCOUNT, which also stops
  every later SDK of the run.

The same happens when the harness itself is interrupted after the request line was written (Ctrl-C,
a test timeout): the driver is stopped and killed, so a reply that comes late can never answer a
later request.

On `close` the harness closes the driver's stdin, gives it 5 s to exit, then kills its process tree.

**Teardown is fail-closed (owner decision, 2026-11-16).** After a driver has been stopped, nothing
more goes out for that SDK's run: not through the driver, and not through the harness's Python
client either. The run's orders still open are listed in the end-of-session CHECK THE ACCOUNT notice
and in the ledger, for the matrix's cleanup tool. While a driver runs, the teardown cancels a
book row by id only when the row also carries the tag the run's ledger holds for that id (an order
a GTT of the run placed keeps its own rule); any other row is listed, not cancelled.

## What a driver must not do

- Hold, relax or re-implement a gate; send a call the harness did not request; send two calls for one
  request; retry; look up an order after an ambiguous reply.
- Write a token, secret key, checksum, auth code or account id to stdout, stderr or a file. The one
  exception is the handshake's `client_id` on stdout (the harness's account match needs it); no
  other line carries an account id. (The harness reads stderr all the time and masks in it, and in
  every error `message` and `emsg`, the secret values it knows (the environment's, its own client's
  token from the saved login, app key and secret key), the account ids, the nonce, bearer tokens,
  and the value of every secret-named key in `key: value`, `key=value` or JSON form. It keeps the
  masked stderr only in the run's ledger (`driver_stderr`) or its driver stderr log, never in a
  refusal, a skip reason or the terminal. That is a second line of defence, not a licence.)
- Run in funded mode or in the live GTT test (the harness never starts a driver there).
- Keep running after stdin closes.

## Testing a driver offline

A driver is tested without any live call: against the SDK's offline broker or a mocked transport, with
a script of requests on stdin and the expected replies on stdout. The tests cover the start-up
refusal (no nonce, a missing opt-in), the handshake, one reply per request, one SDK call per request,
each error type and its fields, the value encoding, no retry after a failed call, and exit on end of
file.
