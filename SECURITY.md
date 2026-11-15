# Security policy

## Reporting a vulnerability

Please report security problems privately. Do not open a public issue or pull request.

- Use GitHub private vulnerability reporting: open the **Security** tab of
  [trade-smart/TradeSmart-Python-SDK](https://github.com/trade-smart/TradeSmart-Python-SDK) and choose
  **Report a vulnerability**.
- Or email TradeSmart at `support@tradesmartonline.in`.

Never send credentials by any channel.

We will acknowledge your report, investigate, and coordinate a fix and disclosure with you.

## Supported versions

| Version | Supported |
| ------- | --------- |
| 2.x     | Yes       |
| < 2.0   | No        |

## What not to include

Never send real credentials in a report, issue, or log excerpt. This includes access tokens, secret
keys, app keys, checksums, auth codes, and passwords. Redact them and describe the problem with
synthetic values instead.

## If your App Key or Secret Key leaks

You can replace them yourself, straight away:

1. Remove the app on the API Dashboard (<https://web.tradesmartonline.in/api>).
2. Create a new one. It gets a new App ID (App Key) and Secret Key.
3. Update your environment variables or `.env` file with the new keys.
4. Log in again.

Contact `support@tradesmartonline.in` if you need help. More on the API Dashboard:
[API access and login](docs/api-access.md).
