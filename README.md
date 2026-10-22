# shortlink-bypass

Resolve **shortxlinks.com** ad-gateway links to their real destination — no ads, no JavaScript, no browser.

`shortlink-bypass` replays the specific chain used by shortxlinks.com: adlinkfly gateways → WordPress safelink hops → a timed token → the CakePHP `/links/go` JSON response. It returns the destination without loading an ad or executing page scripts.

> **Scope:** this is not a universal shortener resolver. Plain `3xx` links work for any URL, and the same handlers may match other adlinkfly / WP-Safelink deployments, but JavaScript-only, captcha-gated, or provider-specific shorteners are out of scope. For bit.ly/tinyurl-style links, `curl -L` is enough.

```console
$ python bypass.py https://shortxlinks.com/EXAMPLE
Input: https://shortxlinks.com/EXAMPLE
Resolving ...
    waiting 27s ...
...
FINAL: https://download.example/file.zip
```

## Why

The shortxlinks.com chain wraps a link in several ad gateways. Opening one in a browser means scripts, pop-unders, and redirect loops before you reach the file you asked for. Its hops are deterministic though: each one is a redirect, an auto-submitted form, a base64 payload, or a timed token. This tool drives that state machine directly and skips everything else.

## Features

- Follows 3xx redirects, `<meta http-equiv="refresh">`, and literal `location` assignments (works on any provider that uses them).
- Replays adlinkfly-style `go` forms and WordPress safelink `newwpsafelink` payloads (`linkr`, `second_safelink_url`, `newsafelink`) as used by shortxlinks.
- Submits the shortxlinks CakePHP destination form (`ad_form_data`, `_csrfToken`, `_Token[...]`) and reads the final `url` from its JSON response.
- Handles the server-side timer (`Too Early`) by waiting the advertised delay, then retrying.
- Detects Cloudflare / JS challenges and captchas and stops with a clear error instead of looping.
- `--json` for scripting, `--check` to probe the destination, `-v` for the full hop log.

## Install

Requires Python 3.8+ and [`requests`](https://pypi.org/project/requests/).

```console
git clone https://github.com/stav3ng3r/shortlink-bypass.git
cd shortlink-bypass
python -m pip install -r requirements.txt
```

## Usage

```console
python bypass.py https://shortxlinks.com/EXAMPLE
```

`EXAMPLE` stands in for the short code.

Verbose hop log:

```console
python bypass.py -v https://shortxlinks.com/EXAMPLE
```

```console
Input: https://shortxlinks.com/EXAMPLE
Resolving ...
    waiting 27s ...

[1] GET  https://shortxlinks.com/EXAMPLE
     200 -> https://gateway-a.example/?adlinkfly=EXAMPLE?...  (1 redirect)
     gate form (go -> https://shortxlinks.com/EXAMPLE?...)
[2] POST https://gateway-a.example/
     200
     safelink payload (timer 25s)
[3] GET  https://gateway-a.example?safelink_redirect=...
     200 -> https://gateway-b.example/?wpsafelink=...  (1 redirect)
     gate form (go -> https://shortxlinks.com/EXAMPLE?...)
[4] POST https://gateway-b.example/
     200
     safelink payload (timer 25s)
[5] GET  https://gateway-b.example?safelink_redirect=...
     200 -> https://shortxlinks.com/EXAMPLE?...  (1 redirect)
     server timer (Too Early)
[6] GET  https://shortxlinks.com/EXAMPLE?...
     200
     destination form
[7] POST https://shortxlinks.com/links/go
     200
     json.url

FINAL: https://download.example/file.zip
```

Example hop log: hostnames and tokens are generalized.

### Multiple links

```console
python bypass.py --json link1 link2 link3
```

Links are resolved sequentially. Each safe-link chain may include one server-enforced wait (~25 s), so batch runs take roughly `n × 30 s`.

## Options

| Flag | Default | Description |
| --- | --- | --- |
| `-v, --verbose` | off | Print every redirect in each hop. |
| `--json` | off | Emit machine-readable JSON (array when multiple URLs are given). |
| `--check` | off | Request the final URL and report its HTTP status. |
| `--timeout SEC` | `30` | Per-request timeout. |
| `--max-hops N` | `15` | Maximum requests before giving up. |
| `--max-wait SEC` | `90` | Cap for server-timer waits. |
| `--insecure` | off | Skip TLS certificate verification. |

Exit codes: `0` destination found, `2` blocked or failed.

### JSON output

```json
{
  "input": "https://shortxlinks.com/EXAMPLE",
  "final": "https://download.example/file.zip",
  "ok": true,
  "error": null,
  "hops": [
    {
      "method": "GET",
      "url": "https://shortxlinks.com/EXAMPLE",
      "status": 200,
      "landed": "https://gateway-a.example/?adlinkfly=EXAMPLE?...",
      "redirects": [{"status": 302, "location": "https://gateway-a.example/?adlinkfly=..."}],
      "note": "gate form (go -> https://shortxlinks.com/EXAMPLE?...)"
    }
  ]
}
```

## How it works

```text
                +---------------------------+
  input URL --> | GET / follow 3xx          | <---------------------+
                +-------------+-------------+                       |
                              |                                     |
                              v                                     |
                +---------------------------+                       |
                | classify the response     |                       |
                +-------------+-------------+                       |
                              |                                     |
        +---------------------+---------------------+               |
        |                     |                     |               |
        v                     v                     v               |
  +-----------+       +----------------+      +-------------+       |
  | gate form |       | safelink form  |      | dest. form  |       |
  |   POST    |       |   GET linkr    |      |    POST     |       |
  +-----+-----+       +--------+-------+      +------+------+       |
        |                      |                     |              |
        +----------------------+---------------------+              |
                               |                                    |
                               v                                    |
                  +--------------------------+                      |
                  | JSON contains "url"?     |--- no ---------------+
                  +------------+-------------+
                               | yes
                               v
                     +-------------------+
                     |     final URL     |
                     +-------------------+
```

`Too Early` short-circuits the loop: wait `delay + 2 s`, then retry the same URL (max 3 attempts).

1. **Fetch.** The input URL is requested with a browser User-Agent. `requests` follows 3xx and archives each hop.
2. **Classify.** The response is checked in order: JSON destination, Cloudflare/captcha block, `Too Early` timer, safelink payload, destination form, gate form, meta refresh, JS assignment, continue link.
3. **Replay.** Gate forms are posted with *all* hidden fields — including CSRF and CakePHP security tokens — because the server validates them. Base64 `go` values are decoded only for the log; the opaque token is what gets submitted.
4. **Wait.** `Too Early` means the token is younger than the advertised `delay`. The tool sleeps `delay + 2 s` (capped by `--max-wait`) and retries the same URL up to three times.
5. **Return.** The destination comes from the shortener's own JSON (`json.url`) or from the last redirect when the chain is purely HTTP.

No page script is ever executed, so no ad network is contacted by the tool itself.

## Supported chains

| Provider pattern | Status |
| --- | --- |
| shortxlinks.com (adlinkfly → WP-Safelink → timed token → `/links/go`) | Supported; validated October 2026 |
| Other adlinkfly / WP-Safelink deployments using identical forms and payload keys | Best-effort; not validated |
| Plain `3xx` shorteners (bit.ly, tinyurl, …) | Followed by the HTTP client; no special handling needed |
| JavaScript-only, captcha-gated, or custom API shorteners | Not supported; the tool stops and reports where |

Gateways rotate hostnames; the resolver matches behavior, not domains.

## Limitations

- **Timers are real.** The destination form is not served until the token ages past its delay (~25 s). There is no client-side shortcut; one wait per chain is expected.
- **Captchas are not solved.** adcopy / reCAPTCHA gates are detected and reported as blocked.
- **JS challenges are not solved.** Cloudflare interstitials need a real browser; the tool returns the last reachable URL and a warning.
- **HTTP chains are followed to the end.** When a shortener uses only 3xx, the destination is fetched as part of resolving it. Safe-link chains return the URL from JSON without touching the target.
- **Provider-specific.** It is built for the shortxlinks.com chain, not as a general shortener client. Other adlinkfly / WP-Safelink sites may work if they use identical forms and keys; anything requiring JavaScript, a custom API, or a captcha will not.

## Disclaimer

Use it on links you are allowed to access. The tool does not solve captchas, log in, or bypass paywalls — it stops and reports whenever a chain requires human verification. It exists to avoid malvertising and adware, not to defraud publishers.
