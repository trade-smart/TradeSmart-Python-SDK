# Live order tests (separately opt-in, test account only)

These tests send **real orders** to TradeSmart. By default they run only on the zero-funds test
account, which has **no funds and no securities**, so every order is expected to be
**rejected**. Separately opted-in modes change this: UAT mode (a non-production test
environment, below) and [funded mode](#funded-mode-a-small-funded-production-account) (an
approved production account that holds a little money, with tight money limits); on UAT the
[UAT live-order matrix](#uat-live-order-matrix-uat_matrix) adds a third opt-in
(`TRADESMART_LIVE_ORDER_UAT_MATRIX`) for its full and stress tiers. They exercise
the real order path end to end:

- the place request and its acknowledgement or rejection (raw `stat` / `emsg`);
- the order's status in `orders()` and `order_history()` (`rejreason`, the order of the
  history entries);
- the SDK's error mapping: `OrderRejectedError` from `place_order()` versus an
  acknowledgement followed by a `REJECTED` row in the order book;
- optionally, the real `ModifyOrder` answer and the answer to cancelling an already rejected
  order.

A plain `pytest` run never collects them: `addopts` in `pyproject.toml` carries
`-m "not live_orders"`, and the `-m live_orders` of the run command below overrides it (the last
`-m` wins).

**Never run them on a funded account**, except in funded mode on an account approved for it
that holds little money (at most the balance ceiling, ₹25,000 by default). The suite refuses
(skips) unless every check below passes, but the checks are a safety net, not permission.

**UAT mode.** The same suite can also run against a non-production test environment of
TradeSmart (UAT), where orders do not move real money. UAT mode applies only when **all** of
these hold: `TRADESMART_ENV=uat`, and the REST host of the client's base URL (parsed as a URL:
the host part only, never the path, query or user info) is a plain host name that contains
`uat` and is not, and does not contain, the production REST host (the host of the SDK's
default base URL; case-insensitive, trailing dots ignored). In UAT mode the zero-funds check is
skipped, the order cap is 40 (the existing UAT tests; the UAT live-order matrix below has its
own ledger and no order cap), the UAT order suites run at any hour and the UAT preflight runs
before any order (both in the table below); every other rule below still applies. With any other
`TRADESMART_ENV` value (or `uat` with a host that fails these checks) the suite refuses before
any API call. With `TRADESMART_ENV` unset or `production`, every production rule applies
unchanged. UAT mode also enables the lifecycle tests of `test_uat_lifecycle.py` (see
[UAT lifecycle tests](#uat-lifecycle-tests)) and the sliced-order test of `test_uat_sliced.py`
(see [UAT sliced-order test](#uat-sliced-order-test)); they skip in every other case. The GTT
test of `test_gtt_live.py` runs in UAT mode and on the zero-funds production account, never in
funded mode (see [GTT test](#gtt-test-uat-or-the-zero-funds-production-account)).

A non-production environment (`TRADESMART_ENV` set to a name other than `production`) is
selected by the test helper `tests/_environment.py`:

- the name may hold only lower-case letters, digits and hyphens; anything else is an error;
- the URLs come from `TRADESMART_ENV_BASE_URL`, `TRADESMART_ENV_WS_URL` and
  `TRADESMART_ENV_AUTHORIZE_URL` (`TRADESMART_ENV_SCRIPT_MASTER_URL` is optional); a missing one
  is an error, never a fallback to production;
- its saved login is `<env>-token.json`, next to the production `token.json`. The run stops with
  an error, before any client is built, when `TRADESMART_TOKEN_FILE` names the production token
  file or any file called `token.json`, when `TRADESMART_ACCESS_TOKEN` is set in the shell (it
  would bypass the environment's login), or when `TRADESMART_CLIENT_ID` differs from the client
  id in the environment's token file. Start the run from a fresh shell.

## Safety rules (enforced in `safety.py`, pinned by `tests/unit/test_live_orders_safety.py`)

| Rule | How |
|---|---|
| Double opt-in | Run only with `TRADESMART_LIVE_TESTS=1` **and** `TRADESMART_LIVE_ORDER_TESTS=1`. Otherwise every test is skipped at collection. `pytest -m live` (the read-only suite) never selects them: they carry the separate marker `live_orders`. |
| Environment | Production (`TRADESMART_ENV` unset or `production`; funded mode included) or UAT mode (above); any other environment, or `TRADESMART_ENV=uat` with a host that is not a UAT host, skips every test. Judged on the base URL the client really uses. Each test's result line carries `uat_mode`. |
| Test account only | Skip unless `profile()` user id **and** the client id equal `TRADESMART_LIVE_ORDER_ACCOUNT` (case-insensitive). Unset = skip. |
| Zero funds | Skip unless `margins()` answers `Ok` and every cash / collateral field is absent or ≤ 0: `cash`, `payin`, `remarks_amt`, `cac_daycash`, `brkcollamt`, `unclearedcash`, `daycash`, `collateral`, `cash_coll`, `grcoll` (documented) and `aux_daycash`, `aux_brkcollamt`, `aux_unclearedcash` (undocumented; stricter). Checked before every order. Skipped in UAT mode only (recorded as `funds_check: skipped (UAT mode)`). In funded mode the balance ceiling replaces it ([funded mode](#funded-mode-a-small-funded-production-account)). |
| Order shape | `LIMIT`, `BUY`, `DAY`, a unique tag (`sdklive-<segment>-<hex>`). Never MARKET, never SELL. Quantity: one lot, the script master's `LotSize` in units on every exchange (equity 1; NFO lot in units; MCX too: CRUDEOIL `qty = 100`, as MCX quantities are in trade units, whole multiples of the lot size). Skip unless `quote()` `ls` is absent or equals the script-master `LotSize`; both are recorded (`lot_size_check`). |
| Price | Last price − 10 %, rounded **down** to the tick; if that is below the lower circuit `lc` from `quote()`, `lc` rounded **up** to the tick. If `lc` is unknown, 0.9 × last price is used (may be rejected for price). Skip if there is no last price or tick size, or if the price would be less than 1 % below the last price. |
| Order cap | At most **3** place calls per test run (**40** in UAT mode for the existing UAT tests, **10** in funded mode; counter spent before the request is sent). Further tests skip. The UAT live-order matrix has no order cap: it is sized by need, with at most 50 of its orders open at once, per-segment circuit breakers and the STOP file ([below](#uat-live-order-matrix-uat_matrix)). |
| Serial only | The cap and the next rule are per process, so the run is never parallel or repeated: either opt-in variable overrides `-n` / `--tx` / `-d` / `--dist` so no worker starts and refuses `-f/--looponfail` with a usage error (`tests/conftest.py`); inside a pytest-xdist worker every test skips at collection. |
| One open order | Skip if `orders()` shows any non-final order before placing (funded mode: `CHECK THE ACCOUNT`, run stopped). |
| Never resend | Exactly one `place_order` call per test. A definitive rejection (`stat: Not_Ok`) is recorded. `OrderStatusUnknownError` or a network error is recorded and the order is looked up in `orders()` by its tag; it is never placed again. If it cannot be found, the test fails with `CHECK THE ACCOUNT: order tag <tag>`. |
| Always clean up | If the order is still open or pending after ~8 s (or its status could not be read after an acknowledgement), it is cancelled **once** in a `finally` block (no retry), then `orders()` is polled for up to 15 s for a final state. If it does not reach one, or cannot be read, the test fails with `CHECK THE ACCOUNT: order <id>`. |
| Fills | If any order-book row or history entry seen for the order has `fillshares` > 0, the status is `COMPLETE`, or `trades()` (called once after settle) lists the order id, the test fails with `CHECK THE ACCOUNT: order <id> was (partly) filled`, even when the final status is `CANCELED` or `REJECTED`. The UAT lifecycle tests and the funded stage-2 tests are the only exceptions: there a fill is recorded and closed by a SELL, not a failure. |
| Interrupted placement | Ctrl-C or any other `BaseException` inside `place_order()` prints and records `CHECK THE ACCOUNT: order with tag <tag> may exist; it was interrupted during placement` (stderr and an immediate JSON line), then re-raises. Except after Ctrl-C, it first does one `orders()` lookup by tag and, if that order is open, one cancel. |
| Market hours | Skip outside the segment's window (IST, Mon–Fri): MCX 09:05–23:15, NSE / NFO 09:20–15:15 (5 minutes after the open, 15 before the close). Exchange holidays are not known: a missing last price also skips. Setting `TRADESMART_LIVE_ORDER_OUTSIDE_HOURS=1` lifts this rule only (every other rule still applies); the exchange then usually rejects the order, which is what the suite expects. In funded mode this variable is refused: funded orders are sent only inside the window. **UAT mode:** `test_live_orders.py`, `test_uat_lifecycle.py`, `test_uat_sliced.py` and the UAT live-order matrix have no window at all (no market-hours refusal, no 15:30 fill cut-off): they run at any hour, and a case a closed market refuses or leaves unfilled is recorded as on any other day, never skipped up front. The GTT test runs at any hour on UAT and keeps the window on production; the matrix's AMO family keeps its own windows. |
| UAT preflight | UAT mode only (never production, funded mode or the zero-funds account). Before any order of a UAT order run (the session of the UAT order suites and of the matrix; the maintainers' cancel-only cleanup tool is exempt), one read-only `margins()` must show `cash`, `turnoverlmt` (turnover limit) and `pendordvallmt` (pending-order value limit) each at the minimum set in code or more (`UAT_MIN_LIMIT` and `UAT_PREFLIGHT_FIELDS` in `safety.py`). Otherwise the run is refused before any order with one message listing every unmet item and "ask the UAT RMS team to raise it"; a field that is missing or cannot be read, a reply that is not `Ok` or a failed read refuses too. The three values are recorded (the matrix: `run.json` and the ledger; the other suites: a `session` line of the results file), never an account id. |

If a test ever fails with **CHECK THE ACCOUNT**, open the TradeSmart web platform at once and
cancel any open order with the reported id or tag.

## Segments and instruments

Each test is parametrised by segment id, so `-k mcx` runs only MCX:

| Id | Exchange | Instrument (from the script master) | Product |
|---|---|---|---|
| `mcx` | MCX | `CRUDEOIL` future (`FUTCOM`), nearest expiry after today | `NRML` |
| `nse` | NSE | `SBIN-EQ` | `CNC` |
| `nfo` | NFO | `NIFTY` index future (`FUTIDX`), nearest expiry after today | `NRML` |

The script-master column names (`Exchange`, `Token`, `TradingSymbol`, `Symbol`, `Instrument`,
`Expiry`, `LotSize`, `TickSize`) are not documented by TradeSmart; they are the observed ones
the SDK already uses.

## Tests

| Test | Orders | What it checks |
|---|---|---|
| `test_limit_buy_is_rejected_or_cancelled[<seg>]` | 1 | The order ends `REJECTED` (or `CANCELED` by the test) or `place_order()` raised a definitive rejection. Records the place answer, the order-book row and the history. |
| `test_modify_of_an_open_order[<seg>]` | 1 | If the order is still open after ~8 s: one `modify_order()` (one tick lower when that stays at or above `lc`; otherwise one tick higher if that is still at least 1 % below the last price; otherwise the same price), recorded raw, then cancelled in `finally`. Skips (after recording) if the order was rejected first. |
| `test_cancel_of_a_rejected_order_is_definitive[<seg>]` | 0 | One `cancel_order()` on a rejected order of this suite (from this run, else today's order book by tag); expects `OrderRejectedError`, **or** an `Ok` acknowledgement as long as the order ends `REJECTED` (TradeSmart acknowledges the cancel, then refuses it). Skips if there is none. |

One segment per run therefore sends at most 2 orders; running all segments at once stops at
the cap of 3.

## UAT lifecycle tests

`test_uat_lifecycle.py` runs only in UAT mode. Outside `TRADESMART_ENV=uat` its tests skip
before any API call; with `uat` they also need every gate above (both opt-ins, the account
check, a UAT REST host), and skip when one fails. Orders still go through the same lifecycle:
the one-open-order check, the cap, one place call (never resent), a lookup by tag after an
ambiguous answer and one cancel in `finally` while the order is open. An order that is refused
is recorded and the test passes or skips; it is never placed again.

Unlike the production tests, the UAT tests send other order shapes (only through
`lifecycle.run_tracked`, which refuses outside UAT mode; `run_order` refuses any shape but the
DAY LIMIT BUY, and any fill, unless fills are allowed in UAT mode, and only the UAT files may name another shape,
`tests/unit/test_live_orders_safety.py`):

- **Close what is filled.** Every fill a test creates is closed by one DAY LIMIT SELL, same
  product, at the then last price (rounded down to the tick, kept within `lc` / `uc`), given up
  to 60 s to fill. The SELL quantity is the BUY's filled quantity, capped at the BUY's quantity.
  The filled quantity is the larger of two values: `fillshares` on the BUY's final `orders()`
  row (the order's running total) and the largest `fillshares` in `trades()` (never the sum,
  since each trade row repeats the running total). An earlier row's value is never used. If
  neither shows it, `orders()` is read again for up to 10 s. If the quantity is still unknown,
  no SELL is sent and the test fails with `CHECK THE ACCOUNT: fill quantity unknown for order
  <id>`. The SELL is judged by its own `orders()` row the same way: unless its filled quantity
  equals the SELL quantity, it is cancelled once if open and the test fails with
  `CHECK THE ACCOUNT: position left open (<sold> of <quantity>)`. Finally the account itself is
  checked. Before every BUY the instrument's `positions()` net quantity is read (if it cannot be
  read, no order is sent). After the close it must be back at that value within 10 s, in every
  test; otherwise the test fails with `CHECK THE ACCOUNT: position left open: positions() net
  quantity ...`. A BUY is sent only while the
  run cap and the test's own cap (2 orders per test) leave one more order for that SELL;
  otherwise the test skips. Every case that may fill uses the round-trip product, so the SELL
  closes it the same day. The exception is the far-from-market LIMIT, which keeps
  `CNC` / `NRML`.
- **Market behaviour (UAT, observed).** A LIMIT order at exactly the last price fills within
  seconds; one 0.5 % above it rested. `MARKET` is refused in the place reply ("ALGO_CHK: MKT
  Order type not allowed for API order", `OrderRejectedError`); production refuses it the same
  way. A BSE order was once left `PENDING`, and both its cancel and its modify were refused
  ("SAF:order is not open to modify" / "... to cancel"). Prices on the test environment depend on the time
  of day. Any other refusal of an order type is recorded and the test
  skips with the message. The whole lifecycle set has not yet completed in one run; the counts below
  are the plan.

Segments (UAT instruments differ from production: some production symbols have no trades there):

| Id | Exchange | Instrument | Product (far LIMIT / every other case) |
|---|---|---|---|
| `mcx` | MCX | skipped: no MCX data in UAT yet | — |
| `nse` | NSE | `ITC-EQ` | `CNC` / `MIS` |
| `bse` | BSE | `ITC` (BSE cash symbols carry no `-EQ`) | `CNC` / `MIS` |
| `nfo` | NFO | `NIFTY` index future (`FUTIDX`), nearest expiry after today | `NRML` / `NRML` |
| `nfoopt` | NFO | `NIFTY` index option (`OPTIDX`) CE | `NRML` / `MIS` |
| `bfoopt` | BFO | `SENSEX` index option (`OPTIDX`) CE (script-master Symbol `BSXOPT`, its future `BSXFUT`) | `NRML` / `MIS` |

Options: the nearest expiry after today, the strike closest to the last price of the nearest
index future of the same underlying (BFO SENSEX: future Symbol `BSXFUT`, options `BSXOPT`; never the SENSEX 50 `SX50*` rows); strikes are quoted nearest first (at most 5) until one has a
last price above 0. One lot (script-master `LotSize`). Skips with the reason when the script
master or UAT lacks the data. Every case first loads the segment's script master; a file that
cannot be downloaded or read, or is empty, skips the case before any order ("the BFO script
master is unavailable on this environment ..."), and so does a missing index future ("no SENSEX
index future (FUTIDX) in the BFO script master ...").

| Test | Segments | Orders | What it checks |
|---|---|---|---|
| `test_open_order_is_modified_cancelled_and_all_views_agree[<seg>]` | all | 1 | A LIMIT BUY 10 % below the market (the price rule above), with an order-update stream subscribed before placing. While it is open: one `modify_order()` and `orders()` shows the new price; then one cancel. `orders()`, `order_history()` and the stream must agree: the history and the stream both show `OPEN`, and the final status of all three is `CANCELED`. Skips (after recording) if the order was rejected or filled (then closed), or if the stream does not start (then no order is placed). |
| `test_limit_at_last_price_fills_and_is_closed[<seg>]` | all | 2 | A LIMIT BUY at the last price (round-trip product), given 60 s to fill. It must end `COMPLETE`. The `positions()` net quantity of the instrument changes by +filled quantity after the buy and by −filled quantity after the closing SELL; `trades()` lists both orders. A partial fill is closed with the filled part only (the test then fails on the status). Skips (cancelled once) if it does not fill. |
| `test_ioc_limit_away_from_the_market_never_rests[<seg>]` | nse, bse, nfo | 1 | A LIMIT BUY 10 % below the market with validity `IOC` ends `CANCELED` without the test cancelling it. If it fills, the fill is closed and the test fails. |
| `test_stop_limit_buy_is_trigger_pending_modified_and_cancelled[<seg>]` | all but mcx | 1 | An `SL-LMT` BUY with the trigger 3 % and the limit 4 % above the last price (options: 10 % and 12 %; on the tick, limit within `uc`) is `TRIGGER_PENDING`; one `modify_order()` moves both a tick up and `orders()` shows them; then one cancel ends it `CANCELED`. If it triggers and fills, the fill is closed and the test skips. |
| `test_stop_market_buy_is_refused_or_recorded[<seg>]` | nse, bse, nfo | 1 | An `SL-MKT` BUY with the trigger 3 % above the market (skipped, nothing sent, when that is above `uc`): refused with the ALGO_CHK message, or accepted and recorded (then cancelled once and final). |
| `test_market_order_is_refused_by_the_api_order_check[<seg>]` | all but mcx | 1 | A `MARKET` BUY must be refused with the ALGO_CHK message, either in the place reply (`OrderRejectedError`) or as a `REJECTED` order-book row. If it fills, the fill is closed and the test fails. |
| `test_every_order_tag_is_unique_and_in_the_order_book[<seg>]` | all | 0 | The tags of this run's lifecycle orders are unique and each acknowledged order is in `orders()` under its own tag; no suite tag appears on two orders of the order book. Skips when the run sent no lifecycle order for the segment. |

When UAT behaves as observed the whole lifecycle set sends 31 orders (37 with the three
production-style tests above in the same run), within the UAT cap of 40. Unexpected fills add
closing SELLs; once the cap is near, the remaining tests skip (a BUY is sent only while an order
is left to close it). `tests/integration/test_uat_lifecycle_offline_broker.py` dry-runs every test
body against the offline broker.

The read-only suite has UAT-only checks too (`tests/live/test_live_uat.py`): holdings, positions
and funds of the test account, and quote, security info, option chain and Greeks of the NFO and
BFO options chosen as above.

To run the UAT tests, start from a fresh shell (no `TRADESMART_ACCESS_TOKEN`, no production
`TRADESMART_TOKEN_FILE`), with a saved login for the environment (`<env>-token.json`, see
above), and set `TRADESMART_ENV=uat`, the three `TRADESMART_ENV_*_URL` variables of your UAT
environment and the order gate:

```
TRADESMART_ENV=uat TRADESMART_LIVE_TESTS=1 TRADESMART_LIVE_ORDER_TESTS=1 \
  TRADESMART_LIVE_ORDER_ACCOUNT="<uat-account-id>" \
  docker compose run --rm dev pytest tests/live_orders/test_uat_lifecycle.py -m live_orders -k nse -v -rs
```

## UAT sliced-order test

`test_uat_sliced.py` checks `place_sliced_order` against UAT. It runs only in UAT mode: outside
`TRADESMART_ENV=uat`, or with `TRADESMART_LIVE_ORDER_FUNDED` set to any value, it skips before
any API call, and it skips when the session is not in UAT mode (every gate above still applies:
both opt-ins, the account check, a UAT REST host, serial runs). It never runs on production or in
funded mode (a freeze-size order is far above the funded money limits).

| Test | Orders | What it checks |
|---|---|---|
| `test_far_limit_buy_of_two_lots_goes_out_as_two_slices_and_both_are_cancelled` | 2 | One `place_sliced_order` call: a DAY LIMIT BUY of **2 lots** of the NFO `NIFTY` index future (nearest expiry after today) with `max_slice_quantity` = **1 lot** and `max_slices=2`, priced like every far order (last price − 10 %, never below `lc`; refused unless at least 1 % below the last price). It must plan two slices of one lot, place both (two distinct order ids, `complete`), and both must end `CANCELED` after one cancel each. Skips (after recording) when UAT refuses the first slice or rejects every slice. |

The order run is `sliced.run_sliced` (pinned by `tests/unit/test_uat_sliced_helpers.py`; a dry run of
the test body on the offline broker is `tests/integration/test_uat_sliced_offline_broker.py`). It keeps
the suite's rules for an order that is several orders:

- **Budget.** The two slices count as two orders against the run cap and the test's own cap of 2,
  spent before the call; with less room the test skips and nothing is sent. `max_slices` is the
  number of slices reserved, so the SDK refuses rather than send more.
- **Open orders.** Skips if `orders()` shows any other non-final order before the call; the
  slices of the call are the only orders open at the same time.
- **Never resend.** Exactly one `place_sliced_order` call (the SDK sends the slices one after
  another and stops at the first failure). Every slice carries the same tag, `sdkslice-<hex>`
  (outside the suite's `sdklive-` prefix, whose tags must each be on one order). After an
  ambiguous outcome the slices are looked up in `orders()` by that tag; if the uncertain slice is
  not there, the test fails with `CHECK THE ACCOUNT: tag <tag>` after cancelling the others.
- **Always clean up.** After ~8 s every slice still open is cancelled **once** in a `finally`
  block, then `orders()` is polled for up to 15 s; a slice not final by then (or an unreadable
  order book) fails the test with `CHECK THE ACCOUNT`. A fill of any slice also fails it with
  `CHECK THE ACCOUNT` (no closing SELL is sent: the two orders use the whole cap).
- **Record.** One JSON line, like the other tests: the plan, the place outcome (planned slices,
  order ids, placed and unsent quantity, the stopping error), the order-book rows, the cancels
  and final statuses. No account id is recorded.

```
TRADESMART_ENV=uat TRADESMART_LIVE_TESTS=1 TRADESMART_LIVE_ORDER_TESTS=1 \
  TRADESMART_LIVE_ORDER_ACCOUNT="<uat-account-id>" \
  docker compose run --rm dev pytest tests/live_orders/test_uat_sliced.py -m live_orders -v -rs
```

## GTT test (UAT or the zero-funds production account)

`test_gtt_live.py` checks the GTT methods against TradeSmart. A GTT whose condition is already
true when it is placed **fires at once** and places a real order, so this test runs **only on
UAT (UAT mode) or on the zero-funds production account**, never in funded mode:
`TRADESMART_LIVE_ORDER_FUNDED` set to any value, or a session in funded mode, skips it before
any API call. On UAT it runs at any hour (like the other UAT order tests). On production it runs inside market hours only:
`TRADESMART_LIVE_ORDER_OUTSIDE_HOURS` set to any value skips it there (how long a `DAY` GTT placed after the close lives is undocumented). Every gate above still applies: both opt-ins, a serial run, the account check
(`TRADESMART_LIVE_ORDER_ACCOUNT`) and, on production, the zero-funds check (run by
`safety.prepare_plan` before anything is sent).

| Test | Calls | What it checks |
|---|---|---|
| `test_far_gtt_buy_is_placed_read_back_modified_once_and_cancelled` | 3 (place, modify, cancel) | One GTT on NSE `ITC-EQ`: alert `LTP_A_O` (LTP greater than the trigger) with the trigger at **3 × a fresh last price** (a `quote()` read just before sending; refused before sending when the quote has no last price or the condition would already be true), alert validity `DAY` (a GTT left behind ends with the day). Its order: a CNC DAY LIMIT BUY of 1 share 10 % below the market (never below `lc`; refused unless at least 1 % below). The GTT must be in `pending_gtts()` once by its tag with the trigger, alert type, alert validity, quantity and price sent; its trigger is then modified **once** to 3.1 × a fresh last price (the same checks first) and read back; then cancelled. Skips (after recording) when TradeSmart rejects the GTT. |
| `test_gtt_alert_types_are_a_list_of_words` | 0 | Read-only: `gtt_alert_types()` is a list of non-blank words (recorded). |
| `test_triggered_gtts_are_readable` | 0 | Read-only: `triggered_gtts()` answers a list (its length and any `sdkgtt-` tags recorded). |
| `test_oco_lifecycle` | 4 on UAT, 3 on production | One BUY OCO of 1 `ITC-EQ` (CNC, LIMIT legs, `sdkgtt-` tag): stop-loss trigger 1.10 × and target trigger 0.90 × a fresh last price (refused before sending when a trigger would already be crossed), placed, read back by alert id (`is_oco`, `stop_loss`, `target`), its stop-loss modified once, cancelled and confirmed gone. On UAT one more `modify_oco` on the cancelled id must be `OrderRejectedError` (`Invalid Oi`); on production that probe is not sent. Skips (after recording) when TradeSmart rejects the OCO. |

The run is `gtt.run_gtt` (pinned by the "GTT" tests of `tests/unit/test_live_orders_safety.py`;
a dry run of the test file on the offline broker, in a UAT and a production session, is
`tests/integration/test_gtt_live_offline_broker.py`). Its rules:

- **Budget.** Place, modify and cancel each count as one call against the run cap and the test's
  own cap of 3, each spent just before it is sent; without room for all three before the place,
  the test skips and nothing is sent. The production cap is 3, so on production run this file on
  its own (not after the order tests of `test_live_orders.py`), and the GTT and OCO tests in
  separate runs: `-k "not oco"`, then `-k oco` (each needs the whole run budget). The OCO test
  (`gtt.run_oco_lifecycle`) follows the same budget, never-resend, clean-up and record rules.
- **Never resend.** One place, at most one modify, one cancel per GTT of this run. Each GTT
  carries a unique tag `sdkgtt-<hex>` (outside the suite's `sdklive-` prefix). After an accepted or
  ambiguous place, or an interruption other than Ctrl-C, the GTT is looked up in `pending_gtts()`
  by that tag or its acknowledged alert id, up to 5 times 2 s apart (the list can lag); an
  ambiguous place then fails the test, after the clean-up.
- **Always clean up.** In a `finally` block every GTT of this run is cancelled **once**: the
  pending rows of the tag and every acknowledged alert id, also one not (yet) visible. Then
  `pending_gtts()` must show none of the tag or of those ids (unreadable, or one still pending:
  `CHECK THE ACCOUNT`); an accepted or ambiguous place whose GTT was never seen there and not
  cancelled is `CHECK THE ACCOUNT` as well (it may exist or have fired); an interruption (Ctrl-C,
  a test timeout) at any step prints and records a `CHECK THE ACCOUNT` alert;
  and neither `triggered_gtts()` nor `orders()` may show the tag or its alert id (a GTT that
  fired placed a real order: `CHECK THE ACCOUNT`; both unreadable: `CHECK THE ACCOUNT` too).
- **Record.** One JSON line: the plan, the fresh quotes and triggers, the place / modify / cancel
  outcomes with their raw bodies, the GTT rows read back (never the account id) and what was left.

```
# UAT
TRADESMART_ENV=uat TRADESMART_LIVE_TESTS=1 TRADESMART_LIVE_ORDER_TESTS=1 \
  TRADESMART_LIVE_ORDER_ACCOUNT="<uat-account-id>" \
  docker compose run --rm dev pytest tests/live_orders/test_gtt_live.py -m live_orders -v -rs
# The zero-funds production account (TRADESMART_ENV unset; no TRADESMART_LIVE_ORDER_FUNDED)
TRADESMART_LIVE_TESTS=1 TRADESMART_LIVE_ORDER_TESTS=1 \
  TRADESMART_LIVE_ORDER_ACCOUNT="<approved-account-id>" \
  docker compose run --rm dev pytest tests/live_orders/test_gtt_live.py -m live_orders -v -rs
```

## UAT live-order matrix (`uat_matrix/`)

A broad, repeatable suite of the documented order dimensions and SDK order features, on UAT (or
in the mock-session mode below). It never runs on production outside that mode and never next to
funded mode: every matrix item skips at collection (and the session refuses before any API call)
unless the run is in UAT mode or mock-session mode with both order opt-ins, and whenever
`TRADESMART_LIVE_ORDER_FUNDED` is set to any value (`0` and empty included).

**Mock-session mode** (`mock_session.py`): the matrix on the production host during an exchange
mock trading session, on a weekend or a listed holiday only. `TRADESMART_LIVE_MOCK_SESSION` must
be today's IST date (`YYYY-MM-DD`) and `TRADESMART_LIVE_MOCK_CONFIRM` exactly `mock session
<that date>`; any failed check refuses before any API call, and the check is repeated before every
place and modify (after midnight IST the run stops; cancels still go out). A limits check reads
`margins()` before any order (`MOCK_MIN_LIMIT`). There is no market-hours check and no fill
cut-off, the position ceiling stays, cases are judged as on production (the UAT-only recording
rules are off), and no AMO or GTT is ever sent: those families skip ("AMO and GTT are not sent in
a mock session: they are stored past it"). The OCO family (`m_oco`) skips too unless
`TRADESMART_LIVE_MOCK_OCO` is set by hand to exactly `oco in mock session <today's IST date>` for
that run; then only its three far cases run (no `Invalid Oi` probe, alert validity `DAY`), and
only the OCOs and orders the run placed are cancelled and confirmed gone, else CHECK THE ACCOUNT.
Every module carries `pytestmark = [live_orders, uat_matrix, m_<family>]`; case ids
are `<family>-<role>-<product>-<prctyp>-<ret>-<side>[-<variant>]` (for example
`shapes-nfo-nifty-f-nrml-sl-lmt-day-sell`), so `-k` selects by any part.

### Families

| File | Marker | Family | Cases (smoke) | Notes |
|---|---|---|---|---|
| `test_shapes.py` | `m_shapes` | M1 shape grid: products × price types × validity × side on NSE, NFO futures and options (ATM and the ITM/OTM rungs), BFO | 485 (19) | the BFO canary first |
| `test_modify.py` | `m_modify` | M2 modify variants (price, quantity, trigger, type, product, tag, validity, after cancel) | 26 (3) | |
| `test_reject.py` | `m_reject` | M3 rejection paths (off tick, part lot, beyond the circuits, wrong product, EOS, disclosed quantity) | 43 (6) | |
| `test_m4_fills.py` | `m_fills` | M4 fills and closes, short and cover, a partial-fill probe | 13 (1) | any hour on UAT (no 15:30 cut-off) |
| `test_m5_cobo.py` | `m_cobo` | M5 cover and bracket orders, `exit_order` | 21 (2) | where the account enables them |
| `test_amo.py` | `m_amo` | M6 after-market orders: placed, modified, cancelled | 19 (2) | outside market hours only |
| `test_sliced.py` | `m_sliced` | M7 sliced orders (freeze slicing, IOC, SELL, MCX, disclosed quantity per slice) | 8 (2) | |
| `test_burst.py` | `m_burst` | M8 burst and pacing: 50 orders (full), 650 at the run's 180-per-minute pace (stress) | 2 (0) | |
| `test_views.py` | `m_views` | M9 order book, history, trades and the order stream agree (sync and async) | 20 (5) | |
| `tests/live/test_live_uat_margin.py` | `m_margin` | M10 `order_margin` (read-only; runs with `-m live`) | 283 (8) | |
| `test_tags.py` | `m_tags` | M11 tags and `&` encoding | 9 (2) | |
| `test_m12_hold.py` | `m_hold` | M12 CNC SELL of the account's holdings | 2 (1) | |
| `test_gtt.py` | `m_gtt` | M13 GTT place, modify, cancel; a GTT that fires at once | 4 (3) | any hour on UAT |
| `test_oco.py` | `m_oco` | OCO place, modify, cancel with far triggers (BUY, SELL, an SL-LMT stop-loss leg) and an `Invalid Oi` probe on the cancelled id; OCOs that fire at once (which leg fired, told by the order's price); the docs-example slot order through `request()` (Python only, recorded) | 6 (1) | any hour on UAT; leg `remarks` carry the case tag on both legs; mock session only with the OCO switch `TRADESMART_LIVE_MOCK_OCO`, far cases only |
| `test_bse.py` | `m_bse` | M14 BSE and BFO canaries, the BSE grid | 36 (9) | |
| `test_mcx.py` | `m_mcx` | M15 MCX (CRUDEOIL, GOLD, GOLDM, GOLDPETAL) in trade units | 136 (10) | any hour on UAT |
| `test_z_algo_id.py` | `m_algo` | U15 a test `algo_id`, recorded only | 3 (1) | run last or on its own |

Each family has a dry run on the offline broker with a UAT-like policy
(`tests/integration/test_uat_matrix_<family>_dry.py`; BUY at the last price fills, a SELL does
not, `MKT` / `SL-MKT` refused with `ALGO_CHK`, BSE orders stuck `PENDING`, RMS `RED:` refusals).

### Tiers and commands

| Tier | Opt-ins (besides the UAT selection and both order opt-ins) | Selection | Size |
|---|---|---|---|
| smoke | none | `-m "uat_matrix and smoke"` | 66 cases, about 75 orders, daily |
| full | `TRADESMART_LIVE_ORDER_UAT_MATRIX=1` | `-m "uat_matrix and not stress"` | about 550-700 orders, 45-60 min |
| stress | `TRADESMART_LIVE_ORDER_UAT_MATRIX=1` | `-m "uat_matrix and stress"` | about 650 orders in one burst |

The matrix runs only when it is **selected explicitly**: an `-m` expression that names
`uat_matrix` positively (`-m "uat_matrix and smoke"`, `-m "live_orders and uat_matrix"`), or the
`tests/live_orders/uat_matrix` path (the directory, a file in it, or a node id). Otherwise its
items are deselected: `pytest tests/live_orders -m live_orders` runs the older suites only, and so
do `-m "not uat_matrix"` and an `-m` expression that cannot be parsed (fails closed). A plain
`-m stress` does not select it; use `-m "uat_matrix and stress"`. Without the matrix opt-in,
full and stress items skip with the reason; a stress item also skips unless `-m` selects
`stress`. The run is always one process: a live opt-in forces `-n 0` (stated below anyway). `compose.yaml` passes `TRADESMART_LIVE_ORDER_UAT_MATRIX` and
`TRADESMART_UAT_RUN_ID` into the container.

```
# smoke (the UAT variables of "UAT mode" above in the shell)
TRADESMART_ENV=uat TRADESMART_LIVE_TESTS=1 TRADESMART_LIVE_ORDER_TESTS=1 \
  TRADESMART_LIVE_ORDER_ACCOUNT="<uat-account-id>" \
  docker compose run --rm dev pytest tests/live_orders/uat_matrix -m "uat_matrix and smoke" -n 0 -v -rs
# full
TRADESMART_ENV=uat TRADESMART_LIVE_TESTS=1 TRADESMART_LIVE_ORDER_TESTS=1 \
  TRADESMART_LIVE_ORDER_ACCOUNT="<uat-account-id>" TRADESMART_LIVE_ORDER_UAT_MATRIX=1 \
  docker compose run --rm dev pytest tests/live_orders/uat_matrix -m "uat_matrix and not stress" -n 0 -v -rs
# stress
TRADESMART_ENV=uat TRADESMART_LIVE_TESTS=1 TRADESMART_LIVE_ORDER_TESTS=1 \
  TRADESMART_LIVE_ORDER_ACCOUNT="<uat-account-id>" TRADESMART_LIVE_ORDER_UAT_MATRIX=1 \
  docker compose run --rm dev pytest tests/live_orders/uat_matrix -m "uat_matrix and stress" -n 0 -v -rs
# one family, or one case
... pytest tests/live_orders/uat_matrix/test_modify.py -m "uat_matrix and not stress" -n 0 -v -rs
... pytest tests/live_orders/uat_matrix -m "uat_matrix and not stress" -k "nfo and sl-lmt" -n 0 -v -rs
```

Hours: on UAT the matrix runs **at any hour** (no market-hours refusal, no 15:30 fill cut-off,
GTT writes included; `TRADESMART_LIVE_ORDER_OUTSIDE_HOURS` is not needed). A case that a closed
market refuses or leaves unfilled is judged as on any other day: its refusal or non-fill is
recorded; note it and decide when to run it again. The UAT hours: the server is up from 06:20 IST
until the next beginning of day; the NSE simulation (cash and F&O 09:15-15:30 IST with a live
feed, currency 09:15-17:00 IST); the BSE and MCX test markets from 10:00 IST until the next
beginning of day, where prices move only when orders match there. The AMO family keeps its own
windows: only **outside** each segment's session (kept clear on weekdays: NSE, NFO and BFO
08:45-15:45 IST; MCX 08:30 to 00:10 IST the next morning, as MCX trades until 23:55 IST in
winter), so run it as a separate evening or early-morning run (`-m "uat_matrix and m_amo"`).
(The code keeps the market-hours rule for a session that is not UAT, which the matrix gate never
opens.)

Before a run, the UAT preflight (safety table above) needs the account's cash, turnover limit
and pending-order value limit at the minimum set in code or more; the session refuses before any order
otherwise.

**UAT behaviours** (seen on UAT; the matrix records them instead of failing, on UAT only; production
judging is unchanged). A modify outside the circuit band is acknowledged `Ok` but not applied, so
every modify step that moves a price stays inside the band of a fresh quote (-1 tick from the
lower circuit steps +1 tick instead, +1 tick at the upper circuit steps down; a far BUY stays well
below the last price). UAT may fill an order at any price: a resting order that fills (fully or
partly) is `record` with the note `uat_filled_far` (exchange, segment, side, product, order type,
limit price, last price, gap %), the fill goes once into the run's position ledger (a partly filled
order is still cancelled), and an instrument whose position reached the ceiling (2 lots) takes no
further order in the run ("position ceiling reached by UAT fills"; closing orders of the fill
families exempt); a GTT's order that fills is recorded the same way. A stop-limit order seen `OPEN`
where `TRIGGER_PENDING` was expected is `record` (note `uat_stop_open`; still cancelled once). The
refusals `BEYOND CIRCUIT LIMITS` and `STOP PRICE IS NOT REASONABLE` (UAT's own band, which may not
match its quote) are `record` (note `uat_band_refusal` with the band the message quotes) and never
count for the segment's circuit breaker. An unknown outcome is still CHECK THE ACCOUNT.
UAT also limits quote reads to 120 per minute: on UAT every quote read of the matrix waits on one
session pacer (at most 110 in any 60 s and 8 in any second); a `current minute` rate refusal waits
for the next minute (at most 60 s). State-changing calls are paced at 6 per second. A quantity
increase acknowledged `Ok` but not shown is `record` on UAT (with a note). A modify of a cancelled
order acknowledged `Ok` is judged from `order_history()` on every environment (an `Ok` reply only
acknowledges the request): a refusal entry with the order still `CANCELED`, or no new entry and
the order unchanged (`modify_after_cancel_ignored`), passes; a changed or no longer cancelled
order fails; an unreadable history is `record` (`modify_after_cancel_unverified`). `Invalid
Trading Symbol` (production SENSEX weekly options that UAT
does not know) is a skip of that case ("environment data") on UAT. A bracket order's trailing
price is at least 1.00 (UAT refuses less). Everywhere: a CNC SELL is sent only after a CNC BUY of
the same stock and quantity filled in the run, or with that sellable quantity in a fresh
`holdings()` read, else the case skips before sending; only an account-wide RMS limit
(`{Turnover limit}`, `Margin Shortfall`) skips a family's remaining cases, any other `RED:`
refusal is an environment limit of that case only; prices use the contract's tick.

### The run: ledger, resume, STOP

- **Run folder.** `.live-results/uat-matrix/<run-id>/` (local, git-ignored, never public): the
  run id is `MMDD` (IST) plus 4 hex digits; `ledger.jsonl` (a line written and flushed before and
  after every state-changing call), `positions.jsonl` (what the run's fills left open),
  `contracts.json` (the day's instruments) and one JSON record per case. No account id is written.
  Every order carries the tag `sdkuat-<run>-<sdk><key>-<n>` (`<sdk>` is one letter: `p` Python, `j`
  JavaScript; at most 30 characters, 22 by default; tags written before the SDK code, `sdkuat-<run>-<key>-<n>`,
  are still read by the matrix and the cleanup tool). `TRADESMART_LIVE_SDKS` (unset: `python`; `all`; or a
  comma list) selects the SDKs, one session each, run serially; only the eleven state-changing calls
  (the eight order and GTT calls and, since driver protocol 2, the three OCO calls) go through the SDK, every read stays on the Python client, and family M9 skips for other SDKs.
  Another SDK runs through a driver process (`specifications/live-driver.md`); the harness acts on
  each reply's wire body, never on the driver's own ids. Its teardown is **fail-closed** (owner,
  2026-11-16): after a driver has been stopped nothing more is sent for that run, through the driver
  or the Python client, and its open orders are listed for `uat_cleanup`; while it runs, a book row
  is cancelled only when it carries the ledger's tag for its id. The
  run's own tags (22 characters for Python, 23 for JavaScript, whose run id carries the SDK letter) round trip through
  `remarks`; UAT was verified to echo both lengths whole on 2026-11-17 (a one-order canary per SDK). A tag-length
  canary stays an optional first step for a new SDK or a changed tag scheme. No caller tags and no longer-tag probe).
- **Resume.** `TRADESMART_UAT_RUN_ID=<run id>` with the same command continues that run: cases
  with a verdict skip ("done in run ..."), a case an earlier process started without a verdict is
  never placed again. Only on the **same trading day** (IST); another day is refused before any
  API call.
- **STOP.** Create `.live-results/uat-matrix/STOP` (any content) to stop a long run safely: no new
  place or modify call, the current batch is cancelled and confirmed, the remaining items skip
  "stopped by the STOP file". Delete the file before the next run (a run that finds it stops at
  once).
- **Open-order invariant.** Before each batch every non-final order in the account must be one of
  this run's (or listed in `TRADESMART_LIVE_ORDER_UAT_IGNORE_OPEN`); any other open order stops the
  run (its own orders are cancelled; the other order is never touched).
- **Run lock.** A `run.lock` file in the run folder allows one process per run id (a resume takes
  it before any API call). A process killed hard leaves it behind: delete it by hand once the
  process is gone, or set `TRADESMART_UAT_RUN_LOCK_TAKEOVER=1` once it is 12 hours old (and the
  ledger has been quiet for 10 minutes).
- **Market hours:** none on UAT (any hour, see "Hours" above); the AMO family checks its own
  windows right before every place. For a session that is not UAT the code keeps the rule: market
  hours checked right before every place (MCX its own window), a refusal skips the case without a
  verdict.
- **UAT preflight.** The session reads `margins()` once after the account check and refuses
  before any order unless cash, turnover limit and pending-order value limit are each at the code minimum or
  more (safety table above); the values go into `run.json` (`uat_preflight`) and the ledger.
- **Market path.** The run sends at most one order that may fill at the market; it is claimed in
  the ledger (`market_claim`, released by `market_release` when it certainly did not fill) before
  it is sent, and a resume restores the claim.
- **End-of-session notice.** The terminal summary shows one notice per session, in this order: the
  CHECK THE ACCOUNT lines listing every leftover order and GTT of the session (not final, stuck,
  unknown outcome, never listed in the book, an unreadable book, a driver's tag mismatch or stop),
  then the open-AMO line, and last the `POSITIONS LEFT` line (the positions the run's fills left:
  the run's own position ledger, or the group's shared one for another SDK's run; a listing, never
  a CHECK THE ACCOUNT). It is also noted in the ledger (`session_notice`, which may hold the
  positions line alone; `positions_notice` holds that line). After Ctrl-C it may list orders that
  are already final.

### Safety rules of the matrix

- No per-run or per-day order cap on UAT. Kept: the SDK's own pacing (10 orders per second, 200
  per minute, the client defaults; never raised), a declared place-call count per case (a bug guard), at most 50 of
  the run's orders open at once, per-segment circuit breakers (one stuck order, one CHECK THE
  ACCOUNT, or three unexpected rejections in a row: the segment's remaining cases skip), the STOP
  file.
- **Order-call pacer.** TradeSmart's API order check (`ALGO_CHK`) counts every place, modify,
  cancel and exit against 10 per second (first smoke run, 2026-11-13), so every state-changing
  call of the matrix (a sliced place counts its slices; the GTT writes too) waits on one
  session-wide pacer: at most 6 in any rolling second (at 8 one `ALGO_CHK` refusal was still seen).
- **Places per minute.** Production refused an order once 200 had been received in the current minute ("Order Recieved 202 in a
  current minute exceeds Limit 200 for user", mock session 14 Nov 2026; the docs say 600; asked of
  TradeSmart), so every place of the run (each slice of a sliced order, every burst order) also
  waits on the run's pacer: at most 180 in any rolling 60 s, under the SDK's 200. Modify, cancel
  and exit count only in the per-second window. A run of about 700 orders spends at least about
  4 minutes placing. Offline dry runs keep the SDK's 200 and run on a simulated clock.
- **Option far prices** sit 4 % from the last price (`far_buy` / `far_sell` with `option=True`,
  the deep sliced option price too), inside the exchange's LPP band; other instruments keep 10 %
  (deep: 20 %). On UAT a far option that fills is `record` as before; in mock mode it is a failure.
- **Environment limits neutral to the breaker.** An order refused for the exchange's price band
  (`... out of the current LPP range.`, in the reply or the order book) or for TradeSmart's
  per-minute order count (`... in a current minute exceeds Limit ...`) finishes `environment limit
  (LPP band)` / `environment limit (order rate)` with the text kept; it neither adds to nor resets
  the segment's unexpected-rejection count and does not skip the family's other cases.
- **Quote reads** are not paced; one refused for rate (`exceeds Limit`) is read again after 0.5 s,
  1 s and 2 s (read-only). On UAT, whose quotes report `ls` 1 for F&O, the F&O / MCX lot comes
  from the script master (the mismatch is in the case record, `lot_size_mismatch`); a master row
  without a lot is still refused.
- **Never resend.** One place / modify / cancel / exit / sliced call per intent. An ambiguous
  outcome is resolved by reading the order book (a place by its tag); not found means CHECK THE
  ACCOUNT for that tag, its segment's breaker trips, and that order gets no further call.
- **One CancelOrder per order per session.** An order still open after its one cancel is listed
  as stuck and its segment trips; the session teardown does not cancel it again. The maintainers'
  cleanup tool (below) may send one more cancel per invocation.
- **Fills** go one at a time per instrument, at any hour on UAT (a BUY a closed market leaves
  unfilled is recorded). On UAT a closing SELL at the last
  price does not fill (listed with the live-verified facts in section 15 of the behaviour
  contract; known-issue key `closing-sell` in the code): the case is a non-strict xfail and the
  position stays in the run's position ledger, at most 2 lots per instrument; a manual or the
  end-of-day square-off clears it. An RMS `RED:` refusal (the UAT account's turnover limit) is an
  "environment limit": the family's remaining cases skip.
- **BSE and BFO** start with one canary order each; a canary stuck `PENDING` (a known BSE issue
  on UAT) skips the segment for the run. `MKT` and `SL-MKT` pass when refused with `ALGO_CHK`
  (the API order check; on UAT `SL-MKT` is refused the same way).
- **GTT family:** on UAT new GTT writes run at any hour, whatever
  `TRADESMART_LIVE_ORDER_OUTSIDE_HOURS` holds (a session that is not UAT refuses them outside
  market hours or with that variable set); the teardown cancels the run's pending GTTs whatever
  the hours (a cancel only removes risk).
- **AMO family:** every AMO is cancelled before the session ends. Open AMOs after an interrupted
  session are listed as `CHECK THE ACCOUNT` in the terminal summary: cancel them before the next
  session's open (09:00 IST) or with the maintainers' cleanup tool (below). AMO LIMIT orders sit
  12 % from the last price (at least 10 % after their modify); on an instrument with a ± 10 %
  circuit the AMO case, the BFO AMO canary included, is an expected skip: watch the first AMO run.
- **`test_z_algo_id.py` runs last** (file order) or on its own: an order with a test `algo_id`
  that sticks open trips its segment for the rest of the run.
- **First MCX run:** watch it. The far BUYs on GOLD, GOLDM and GOLDPETAL must rest (`OPEN`); the
  code does not compare the price unit with the quote unit (`prcftr` is recorded per order).
- **Burst and stress** measure the client at the cap: every burst place waits on the run's pacer
  (180 per minute) and the order pacer (6 per second), and the SDK's own limits (10/s, 200/min)
  must never be exceeded nor refuse an order (an SDK `RateLimitError` or an HTTP 429 fails the
  case); the case records the most places started in any second and minute (`max N/min`). The
  stress case (650 orders) passes when its places reached the run's 180 per minute; when the
  waves' settle and cancel pauses (and the ledger's flush to disk, about 3-12 ms per line on a
  Windows bind mount) kept them under it, it records "180/min pace not reached (max N/min)". An
  LPP-band or order-rate refusal in a burst stops it as an environment limit (above). Stress has
  not been run since this change.

### After the run

- After **Ctrl-C** the teardown cancels nothing (an interrupted call must not be followed by more
  calls), and a **hard kill** skips the teardown entirely: in both cases, and whenever the run
  lists a stuck or CHECK THE ACCOUNT order, run the maintainers' internal cleanup tool (not part
  of this repository) with the run id, dry run first. It reads the run's ledger, cancels each
  pending GTT and then each open order of the run once per invocation, lists the ones with an
  unknown outcome instead of cancelling them (unless explicitly confirmed), lists the positions
  the run left, and never places, modifies or exits an order. It refuses while the run may still
  be running (a ledger line of the run in the last 10 minutes and no teardown note after it; a
  stop is not the end, the teardown follows it) unless told the run is over (after a hard kill);
  a run that is starting or resuming is invisible to it until its first ledger line, so never
  start it while you start or resume a run. Its success means no order or GTT of the run is left
  open; leftover positions are only listed and are closed by hand or by the end-of-day
  square-off. Without the tool, cancel the run's open orders and GTTs by hand in the web
  platform (`ledger.jsonl` lists them by their tags `sdkuat-<run>-...`).
- **`uat_matrix/uat_expectations.json`** pins the answers of undocumented UAT behaviour by case id
  (`{"verdict": "accept" | "reject", "words": ...}`), written from the **first full run** so later
  runs catch drift. Never pin `accept` for a case whose verdict is a recorded fill: the
  partial-fill probe (`fills-...-partial`, `-partial-close`), the CO/BO `-fill` cases and the
  holdings SELL `hold-...-fill` (a fill depends on the market of the moment).
  `tests/unit/test_uat_matrix_contracts.py` refuses such a pin.

## Funded mode (a small funded production account)

`test_funded_live.py` sends **real orders with real money** on a production account that holds
little money and that its holder approved for it. Some orders are meant to fill. Cash and
index options only. Futures, MCX, `MARKET` / `SL-MKT` orders, selling an option to open, AMO,
cover or bracket orders, runs outside market hours and UAT mode are out of scope. Its tests skip
in every other mode, and in funded mode the production tests of `test_live_orders.py` skip, so
only `test_funded_live.py` sends orders there.

### Gates

Funded mode is on only when **all** of these hold. A failed check skips every funded test with
the reason, before any order is sent; it never falls back to other rules.

| Check | Rule |
|---|---|
| Opt-ins | `TRADESMART_LIVE_TESTS=1`, `TRADESMART_LIVE_ORDER_TESTS=1` **and** `TRADESMART_LIVE_ORDER_FUNDED=1` (exactly `1`; any other value refuses). |
| Account | `TRADESMART_LIVE_ORDER_ACCOUNT` equals the `profile()` user id and the client id (as above). |
| Production only | `TRADESMART_ENV` unset or `production`, and the REST host is exactly the production host of the SDK's default base URL (the same host parser as UAT mode). `TRADESMART_LIVE_ORDER_FUNDED` set to any value turns UAT mode off; funded mode and UAT mode are never both on. |
| Balance ceiling | `margins()` answers `Ok` with a usable `cash` at most the ceiling, and the positive cash / collateral fields of the zero-funds check together are at most the ceiling too. The ceiling is **₹25,000**; `TRADESMART_LIVE_ORDER_MAX_BALANCE` can only lower it (a whole number of rupees from 1 to 25000; anything else refuses). Checked when the session opens and again in each test before its first order. |
| Market hours | 09:20–15:15 IST, Monday to Friday, for all four segments. `TRADESMART_LIVE_ORDER_OUTSIDE_HOURS=1` is refused. |
| Serial | As above (one process, `-n 0`, never inside a parallel worker). |

Start with no open order in the account: an open order found there when a test is about to place
one ends the run with `CHECK THE ACCOUNT` (see below).

### Limits

| Limit | Value |
|---|---|
| Orders per run | **10** place calls |
| Orders per test | **2**: a BUY and the SELL that closes its fill. A BUY is sent only while the run and the test still have room for that SELL. |
| Value of one order | `max(price, trigger price) × quantity` (an option: premium × quantity) at most **₹2,000**. A closing SELL is exempt (it only reduces the position). |
| BUY value per run | At most **₹5,000**. Each BUY reserves its value before it is sent and gets it back only when it ends with no fill. A BUY that would pass the limit is not sent (the test skips). |
| Order shape | `LIMIT` or `SL-LMT` only, product `MIS`, quantity 1 share or 1 lot, a unique `sdklive-` tag. |
| SELL | Only to close a BUY filled in the same test, at most the filled quantity; any other SELL is refused before it is sent. |
| Option strike | Nearest expiry after today; the nearest out-of-the-money CE with a last price above 0 whose premium × lot size is at most **₹1,800** (90 % of the order limit, leaving room for the spread); at most 40 strikes above the future are quoted. None found: the segment skips. |

Every other rule above still applies: one open order at a time, one place call per order (never
resent; an ambiguous answer is looked up by tag), one cancel in `finally` while an order is open.
`MIS` means the broker squares off an intraday position left at the end of the day. That is a
safety net, not the plan: the tests close every fill themselves.

### Instruments

| Id | Exchange | Instrument | Quantity |
|---|---|---|---|
| `nse` | NSE | `ITC-EQ` | 1 share |
| `bse` | BSE | `ITC` (skipped with the reason when it is missing from the script master or has no last price) | 1 share |
| `nfoopt` | NFO | `NIFTY` index option (`OPTIDX`) CE, strike as above | 1 lot (script-master `LotSize`) |
| `bfoopt` | BFO | `SENSEX` index option (`OPTIDX`) CE, strike as above (skipped with the reason when the data is missing) | 1 lot |

### Tests: two stages

**Stage 1 (no fills).** The `test_stage1_*` tests, first in the file. They run in every funded
run and send **8 orders** when no segment skips:

| Test | Segments | Orders | What it checks |
|---|---|---|---|
| `test_stage1_far_limit_open_modified_cancelled[<seg>]` | all four | 1 each | A LIMIT BUY far below the market (cash −10 %, options −50 %, floored to the tick; raised to the lower circuit when below it, and skipped, nothing sent, unless the price is still at least 3 % below the last price), with an order-update stream subscribed first. It must be `OPEN`; one `modify_order()` (one tick), and `orders()` shows the new price; one cancel ends it `CANCELED`. `orders()`, `order_history()` and the stream must agree. |
| `test_stage1_ioc_far_never_rests[<seg>]` | nse, bse | 1 each | The same far LIMIT BUY with validity `IOC` ends `CANCELED` without the test cancelling it. |
| `test_stage1_stop_limit_not_hit[<seg>]` | nse, bse | 1 each | An `SL-LMT` BUY with the trigger 3 % and the limit 4 % above the last price is `TRIGGER_PENDING`; one `modify_order()` and `orders()` shows it; one cancel ends it `CANCELED`. |

A far LIMIT or SL-LMT order that is rejected or never open fails its test, and so does an IOC
order that does not end `CANCELED`. A stage-1 order that fills is sold back at once at the best
bid, and the test fails.

**Stage 2 (tiny fills).** `test_stage2_tiny_fill_closed[<seg>]`, all four segments, last in the
file. It runs only with `TRADESMART_LIVE_ORDER_FILLS=1`, when no stage-1 test of the same run
failed and the run was not stopped; otherwise it skips before any order. It sends its BUY only
before **14:30 IST** (checked at the start and again just before the BUY; from 14:30 on the test
skips and sends nothing), so a fill can still be sold back well before the intraday square-off.
Per segment, up to **2 orders** (8 in all): a LIMIT BUY at the best offer (the last price when
there is no depth), rounded up to the tick and kept within the circuits, given 60 s to fill. A
BUY the broker rejects fails the test (it is not a "did not fill" skip). Not filled: cancelled
once, recorded, and the test skips. Filled: one LIMIT SELL of exactly the filled quantity at the
best bid (else the last price), given 60 s. The `positions()` net quantity must change by
+filled after the BUY and be back where it began after the SELL (within 10 s), and `trades()`
must list both orders. If the close does not fill, the fill quantity is unknown or the net
quantity is not back, the open SELL is cancelled and the run stops with `CHECK THE ACCOUNT:
position left open`.

**Run the stages as two commands.** Stage 1 uses 8 of the 10 orders per run and stage 2 alone
uses up to 8, so they do not fit in one run:

1. Stage 1: the funded variables **without** `TRADESMART_LIVE_ORDER_FILLS`. The stage-2 tests
   skip and send nothing.
2. Stage 2, only after a clean stage-1 run (every stage-1 test passed or skipped for a data
   reason, none failed or errored, no `CHECK THE ACCOUNT`, and the account shows no open order or
   position from it): the same variables plus `TRADESMART_LIVE_ORDER_FILLS=1`, and `-k stage2`
   so only the stage-2 tests run.

The suite checks stage-1 failures only within one run; across the two commands that check is
yours. Never set `TRADESMART_LIVE_ORDER_FILLS=1` without `-k stage2`: stage 1 would use 8
orders, leaving room for one round trip only. Run stage 2 **at most once per day**, also after
"did not fill" skips: each run has its own limits, so every further run allows another ₹5,000
of BUY orders on the same day.

### CHECK THE ACCOUNT in funded mode

`CHECK THE ACCOUNT` means the suite could not confirm that the account is back where it began:
an order may still be open (it could not be cancelled, or its state could not be read), an order
was already open in the account, a fill could not be counted, or a position is left open (the
closing SELL did not fill, or `positions()` does not show the net quantity back). The run then
**stops**: every later funded test of the run fails at its start, before any API call.

What to do, at once:

1. Open the TradeSmart web platform. In **Orders**, cancel every open order, in particular those
   with an `sdklive-` tag or the order id in the message.
2. In **Positions**, look at the instruments of the run (ITC on NSE and BSE, the NIFTY and SENSEX
   options named in the message or the results). Close any open position by hand (sell the open
   quantity) and check that its net quantity is 0 again.
3. Start no new run until the account is clean. If you cannot close a position, the broker
   squares off `MIS` positions near the end of the day; that is the safety net, not the plan
   (brokers often charge for it).

### Cost

Stage 1 is not meant to fill, so it normally costs nothing. Stage 2 buys and sells at most 1
share of ITC on each exchange and 1 lot of each option, at most ₹2,000 per order and ₹5,000 of
BUY orders per run, and closes each fill within about a minute. Each filled order is charged as
usual: your plan's brokerage per executed order (up to 8), the statutory taxes and fees
(securities transaction tax, exchange transaction charges, regulator fee, stamp duty, GST), all
small on these amounts. The main cost is the spread: buying at the best offer and selling at the
best bid loses the spread times the quantity (for an option, times the lot size), plus any price
move during the minute the position is held. A position left open is exposed to the market until
it is closed.

### Run funded mode

From `sdk-python/`, in a fresh shell: `TRADESMART_ENV`, `TRADESMART_ENV_*`,
`TRADESMART_BASE_URL`, `TRADESMART_TOKEN_FILE`, `TRADESMART_LIVE_ORDER_OUTSIDE_HOURS` and
`TRADESMART_LIVE_ORDER_FILLS` unset. Log in to the approved account on production first, on
the day of the run (tokens expire daily at 06:00 IST); the saved login is the production one,
never an environment's `<env>-token.json`:

```
docker compose run --rm --service-ports login --client-id "<funded-account-id>"
```

Replace `<funded-account-id>` with its client id here and below; the id is never written in
this repository. Optionally lower the ceiling with
`TRADESMART_LIVE_ORDER_MAX_BALANCE` (for example `10000`); it can never be raised.

Stage 1 (bash):

```
TRADESMART_LIVE_TESTS=1 TRADESMART_LIVE_ORDER_TESTS=1 TRADESMART_LIVE_ORDER_FUNDED=1 \
  TRADESMART_LIVE_ORDER_ACCOUNT="<funded-account-id>" \
  docker compose run --rm dev pytest tests/live_orders/test_funded_live.py -m live_orders -v -rs
```

Stage 2, only after a clean stage 1 (bash):

```
TRADESMART_LIVE_TESTS=1 TRADESMART_LIVE_ORDER_TESTS=1 TRADESMART_LIVE_ORDER_FUNDED=1 \
  TRADESMART_LIVE_ORDER_ACCOUNT="<funded-account-id>" TRADESMART_LIVE_ORDER_FILLS=1 \
  docker compose run --rm dev pytest tests/live_orders/test_funded_live.py -m live_orders -k stage2 -v -rs
```

Always name `tests/live_orders/test_funded_live.py`, never the whole folder. Each test writes
one line to the results file (below) with `funded_mode: true`, the money reserved so far
(`money_committed`), the orders it sent, the stage-1 prices or the stage-2 BUY (`tiny_buy`),
the option chosen (`option`), any position left open (`left_open`) and whether the run was
stopped. `tests/integration/test_funded_live_offline_broker.py` dry-runs every test body against the offline
offline broker, including a whole-file run and a stage-2-only run, and runs the real fixtures and
report hook of `test_funded_live.py` in an inner pytest session (only the login session is
replaced by the offline broker's).

## Run

From `sdk-python/`, in your own shell, after `docker compose run --rm --service-ports login
--client-id <YOUR_CLIENT_ID>` (or with `TRADESMART_CLIENT_ID` + `TRADESMART_ACCESS_TOKEN` set). The variables
are passed into the container by `compose.yaml`; they are never written to a file.
Run only on the zero-funds test account: `TRADESMART_LIVE_ORDER_ACCOUNT` carries
its client id (replace `<approved-account-id>` below); the id is not written in this repository.

PowerShell (MCX tonight):

```
$env:TRADESMART_LIVE_TESTS = "1"; $env:TRADESMART_LIVE_ORDER_TESTS = "1"; $env:TRADESMART_LIVE_ORDER_ACCOUNT = "<approved-account-id>"
docker compose run --rm dev pytest tests/live_orders/test_live_orders.py --ignore=tests/live_orders/uat_matrix -m live_orders -k mcx -v -rs
```

bash (NSE and NFO equity tomorrow, one segment per run):

```
TRADESMART_LIVE_TESTS=1 TRADESMART_LIVE_ORDER_TESTS=1 TRADESMART_LIVE_ORDER_ACCOUNT="<approved-account-id>" \
  docker compose run --rm dev pytest tests/live_orders/test_live_orders.py --ignore=tests/live_orders/uat_matrix -m live_orders -k nse -v -rs
TRADESMART_LIVE_TESTS=1 TRADESMART_LIVE_ORDER_TESTS=1 TRADESMART_LIVE_ORDER_ACCOUNT="<approved-account-id>" \
  docker compose run --rm dev pytest tests/live_orders/test_live_orders.py --ignore=tests/live_orders/uat_matrix -m live_orders -k nfo -v -rs
```

Afterwards, clear the gate in PowerShell:
`Remove-Item Env:TRADESMART_LIVE_ORDER_TESTS, Env:TRADESMART_LIVE_ORDER_ACCOUNT`.

## Results

Each test appends one JSON line to `sdk-python/.live-results/live_orders.jsonl` (git-ignored),
also when it skips or fails: the plan (symbol, lot, price, tag), the quote (`lp`, `lc`, `ti`,
`ls`), the place outcome with the raw `stat` / `emsg` or response body, the SDK exception, the
order-book row (`status`, `rejreason`), the cancel outcome, the `order_history()` entries in the
order received (`status`, `rejreason`, `norentm`, `rpt`) and the modify answer. Keys that could
hold secrets or personal data (`uid`, `actid`, `pan`, any `*token*` except the instrument
`token`, `*secret*`, `Authorization`, ...) are dropped before writing. Every line carries
`environment` (the `TRADESMART_ENV` name, `production` when unset), each test line `uat_mode`; the
lifecycle tests add `lifecycle: true`, the stream statuses and whether the three views agree.

The raw bodies of `PlaceOrder`, `ModifyOrder`, `CancelOrder` and the GTT writes (`PlaceGTTOrder`,
`ModifyGTTOrder`, `CancelGTTOrder`) are captured by an httpx
response hook attached to the client's HTTP client (test-only; the hook only keeps a reference
and reads the body after the SDK has read it). A body is attributed to a call only if it was
recorded after that call started (the log position is noted before each place, modify and
cancel), so an earlier test's body is never reported as this one's. `raw_bodies_recorded` in
each line says whether the hook was attached.
