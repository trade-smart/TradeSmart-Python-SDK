# Test fixtures

Shared JSON fixtures for the SDKs. All values are synthetic: no real tokens,
user ids, names, emails or phone numbers. Every fixture file is listed in
exactly one of the tables below.

## Derived from official examples (values replaced)

| File | Notes |
|---|---|
| `errors.json` | Failure examples from the official docs. The `no_data`, `search_no_data` and `order_reject` texts are copied verbatim (they contain no secrets); other values are synthetic. |
| `user_details_success.json` | `UserDetails` success example: same structure as the docs; ids, name and email replaced with synthetic values. |
| `limits_success.json` | `Limits` success example: same structure and values as the docs example (no ids in it). |
| `gen_acs_tok_success.json` | `GenAcsTok` success example: same structure as the docs; ids, tokens and name replaced with synthetic values. |
| `search_scrip_success.json` | `SearchScrip` success example (3 of the docs' results: BSE, NSE, NFO future); values as in the official example (public instrument data, no ids). |
| `get_quotes_success.json` | `GetQuotes` success example (includes `lut`); same values as the docs example (public market data). |
| `security_info_success.json` | `GetSecurityInfo` success example (key `symname`); same values as the docs example. |
| `option_chain_success.json` | `GetOptionChain` success example trimmed to 4 contracts (CE/PE at 2280 and 2300); tokens for the two PE rows are synthetic. |
| `tpseries_success.json` | `TPSeries` success example: first 5 candles (newest first) of the official Postman example; public market data. |
| `eod_chart_success.json` | `EODChartData` success example: 3 elements kept as JSON strings (double-encoded) exactly as the API sends them; public market data. |
| `order_book_success.json` | `OrderBook` success example: all 13 rows of the full official Postman example (all `REJECTED`); `uid`/`actid`/`src_uid` -> `TEST123`, `ui_dev_code` -> `TEST123_03`, `ipaddr` -> `127.0.0.1`; other values unchanged. |
| `single_ord_hist_success.json` | `SingleOrdHist` success example: both rows of the official Postman example (REJECTED, PENDING); `uid`/`actid` -> `TEST123`, `pan` -> `XXXXX0000X`. |
| `place_order_success.json` | `PlaceOrder` success example (LMT): same structure as the docs; `norenordno` replaced with a synthetic number. |
| `order_margin_success.json` | `GetOrderMargin` success example: same structure and values as the docs example (no ids in it). |
| `place_gtt_success.json` | `PlaceGTTOrder` success example: `stat` is the exact word `Oi created` (not `Ok`) with a non-blank `al_id`; same structure as the GTT page of the docs (copy of 2026-11-12); alert id and time as in the docs example. |
| `modify_gtt_success.json` | `ModifyGTTOrder` success example: `stat` `Oi replaced` with `al_id`; same structure as the docs; alert id and time as in the docs example. |
| `cancel_gtt_success.json` | `CancelGTTOrder` success example: `stat` `OI deleted` (the word the test environment answers and the SDKs accept; the docs word `Oi delete success` is not a success, owner decision 2026-11-18) with `al_id`; structure and values as the docs example, only the word changed. |
| `pending_gtts_success.json` | `GetPendingGTTOrder` success example (an array, one object per pending GTT): the first row has the docs example's keys and values (`qty` a JSON number, as in the docs) with `actid` -> `TEST123`; the second row is synthetic (an `LTP_A_O` alert placing an `SL-LMT` SELL, with `trgprc`, `qty` a string, alert `validity` `DAY`). The GTT's own validity is the wire `validity` (DAY/GTT); `ret` is the validity of the order it places. The pending list also carries two synthetic OCO rows shaped as observed on 2026-11-18 (web terminal capture): a BUY OCO (`LMT_BOS_O`, `al_id` 26111800000001) with the stop-loss in `x` (leg 1, `place_order_params`) and a SELL OCO (`LMT_SOB_O`, 26111800000002) with the **target in `x`** (the docs orientation), so a reader must choose stop-loss and target by trigger value and side, not by slot. An OCO row has a top-level `d` (= `x`'s) and `remarks`, leg 1's fields copied to the top level (`qty` a JSON number), `oivariable` (two items, `var_name` x / y, `d` with two decimals), legs without `tsym`/`exch`/`ret`/`trgprc` (`qty` a JSON number, `prc` with two decimals, `ipaddr` as the placeholder `0.0.0.0`), only leg 1 with `s_prdt_ali`; `C`, `ti`, `brkname`, `cname` and `upd_tm` have placeholder values (their meaning is not documented). All ids synthetic (`TESTUID`). |
| `triggered_gtts_success.json` | `GetTrgOi` success example: the docs example row (`actid` -> `TEST123`) with `norentm` (present in the docs example, not in its field table) and the three notification flags. The triggered list also has two synthetic OCO rows with the row `stat` `CANCELLED` (a cancelled OCO is listed by `GetTrgOi`, as a cancelled GTT, F32 / F35 c): a BUY OCO, `al_id` 26111800000003, same leg shape as the pending OCO rows; and a SELL OCO (`LMT_SOB_O`, 26111800000004, also `CANCELLED`) with the **target in `x`** (the docs orientation), so both slot orders are covered in the pending and the triggered list. No fired OCO has been observed, so no fired OCO row exists. |
| `enabled_gtts_success.json` | `GetEnabledGTTs` success example: `ai_ts` holds the alert families (`ATP`, `LTP`); `request_time` here is `DDMMYYYYHHMMSS` (no separators), unlike the other endpoints; same values as the docs example. |

## Synthetic, no official example exists

| File | Notes |
|---|---|
| `place_oco_success.json` | `PlaceOCOOrder` success: `stat` `OI created` with `al_id` and `request_time`; the docs example's shape with the word the test environment answers (same as the docs word); alert id and time synthetic. |
| `modify_oco_success.json` | `ModifyOCOOrder` success: `stat` `OI replaced` with the same `al_id`; shape as the docs example, synthetic id and time. |
| `cancel_oco_success.json` | `CancelOCOOrder` success: `stat` `OI deleted` (the observed word; the docs say `Oi delete success`) with `al_id`; synthetic id and time. |
| `modify_oco_invalid.json` | `ModifyOCOOrder` (and `CancelOCOOrder`) answer for an alert that no longer exists, as observed on the test environment: `stat` `Invalid Oi` with an `al_id` and **no** `emsg` (so a reader that treats "`al_id` and no `emsg`" as success would misread it; the SDKs raise `OrderRejectedError`); synthetic id. |
| `holdings_success.json` | `Holdings` array with one record and one NSE `exch_tsym` entry, built only from documented fields with plausible string values. |
| `position_book_success.json` | `PositionBook` array with one NSE/MIS position, built only from documented fields with plausible string values. |
| `trade_book_success.json` | `TradeBook` array with two fills (NSE buy/sell), built only from documented fields with plausible string values (no official non-empty example; `fltm`/`exch_tm` formats are assumed `dd-mm-YYYY hh:MM:SS`). |
| `cancel_order_success.json` | `CancelOrder`/`ModifyOrder`/`ExitSNOOrder` acknowledgement: no official success example exists; built from documented fields (`dmsg` per the acknowledgement fields) with a synthetic message. |
| `cancel_order_ack_live_shape.json` | `CancelOrder` acknowledgement in the shape the live server sends (`stat: Ok` with the order id in `result`; observed 2026-11-10); time and order id synthetic. |

## WebSocket messages

| File | Notes |
|---|---|
| `ws_messages.json` | One WebSocket message per `t` (`ak, tk, tf, dk, df, om, pk, pm, uk, udk, uok, upk, hk`) from the WebSocket page of the docs, keyed by `t`. The docs give only `t` for `udk`, `uok` and `upk`, so those entries hold nothing else. `df` is the verbatim docs sample; the others are synthetic, built only from documented keys (uid/actid `TEST123`). The extra key `ak_upper_ok` is a synthetic connect ack with `"s": "OK"`: the live server sent this spelling (observed 2026-11-10) while the docs show `"Ok"`, so a client must compare the status case-insensitively. Keys other than the `t` names are variants, not message types. |
