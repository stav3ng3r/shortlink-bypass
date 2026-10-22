#!/usr/bin/env python3
"""
shortlink-bypass - resolve shortxlinks.com ad-gateway links to the final target.

The resolver replays the shortxlinks / adlinkfly / WP-Safelink chain without
executing JavaScript or loading any ad:

  HTTP 3xx -> meta refresh -> location assignment -> interstitial forms
  -> adlinkfly `go` payload -> WordPress safelink `newwpsafelink` payload
  -> final shortener `/links/go` JSON -> destination

It never solves captchas and never runs page scripts; a JS challenge is reported
as blocked and the last reachable URL is returned.

Usage:
  python bypass.py https://shortxlinks.com/EXAMPLE
  python bypass.py -v shortxlinks.com/EXAMPLE
  python bypass.py --json https://shortxlinks.com/EXAMPLE
  python bypass.py --check https://shortxlinks.com/EXAMPLE

Requires: requests (pip install requests)
"""

from __future__ import annotations

import argparse
import base64
import html as htmlmod
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urljoin, urlparse

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("error: requests is required (pip install requests)")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.I | re.S)
INPUT_RE = re.compile(r"<input\b[^>]*>", re.I | re.S)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
META_REFRESH_RE = re.compile(
    r"<meta\b[^>]*http-equiv\s*=\s*[\"']?refresh[\"']?[^>]*>", re.I | re.S
)
INTERSTITIAL_RE = re.compile(
    r"please\s+wait|just\s+a\s+moment|redirecting|verifying"
    r"|checking your browser|safelink|too\s+early",
    re.I,
)
JS_REDIRECT_RES = (
    re.compile(r"(?:window\.)?location(?:\.href)?\s*=\s*([\"'])([^\"']+)\1", re.I | re.S),
    re.compile(r"(?:window\.)?location\.(?:replace|assign)\(\s*([\"'])([^\"']+)\1\s*\)", re.I | re.S),
    re.compile(r"(?:document|top)\.location(?:\.href)?\s*=\s*([\"'])([^\"']+)\1", re.I | re.S),
)


# --------------------------------------------------------------------------- helpers


def attr(tag: str, name: str) -> Optional[str]:
    """Read an HTML attribute value regardless of quote style / attribute order."""
    m = re.search(
        r"(?<![-\w])" + name + r"\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
        tag,
        re.I | re.S,
    )
    if not m:
        return None
    value = m.group(1) if m.group(1) is not None else m.group(2) if m.group(2) is not None else m.group(3)
    return htmlmod.unescape(value)


def b64_text(value: str) -> Optional[str]:
    """Decode standard or URL-safe base64 with missing padding."""
    value = re.sub(r"\s+", "", value)
    pad = "=" * (-len(value) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return decoder(value + pad).decode("utf-8", "replace")
        except Exception:
            continue
    return None


def normalize_url(url: str) -> str:
    url = url.strip().strip("<>\"'")
    if url and "://" not in url:
        url = "https://" + url
    return url


def shorten(url: str, width: int = 100) -> str:
    if url is None:
        return ""
    return url if len(url) <= width else url[: width - 3] + "..."


# --------------------------------------------------------------------------- ui

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
CYAN = "\x1b[36m"
MAGENTA = "\x1b[35m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
BLUE = "\x1b[34m"
GREY = "\x1b[90m"


def _supports_color(stream, mode: str) -> bool:
    if mode == "never":
        return False
    if mode == "always":
        return True
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    forced = os.environ.get("FORCE_COLOR") or os.environ.get("CLICOLOR_FORCE")
    if forced and forced != "0":
        return True
    return bool(getattr(stream, "isatty", lambda: False)())


def _enable_windows_vt() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def _encode_ok(text: str) -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


@dataclass(frozen=True)
class Glyphs:
    arrow: str
    branch: str
    last: str
    pipe: str
    ok: str
    fail: str
    wait: str
    full: str
    empty: str
    rule: str
    tl: str
    tr: str
    bl: str
    br: str


_UNICODE_GLYPHS = Glyphs("→", "├", "└", "│", "✔", "✖", "⏳", "█", "░", "─", "┌", "┐", "└", "┘")
_ASCII_GLYPHS = Glyphs("->", "|-", "\\-", "|", "OK", "X", "..", "#", "-", "-", "+", "+", "+", "+")


def _glyphs() -> Glyphs:
    return _UNICODE_GLYPHS if _encode_ok("→├└│✔✖⏳█░─") else _ASCII_GLYPHS


@dataclass
class Form:
    action: str
    method: str
    fields: Dict[str, str] = field(default_factory=dict)
    fid: str = ""


def parse_forms(page: str, base: str) -> List[Form]:
    forms: List[Form] = []
    for match in FORM_RE.finditer(page):
        open_tag = "<form" + match.group(1) + ">"
        action = attr(open_tag, "action") or base
        method = (attr(open_tag, "method") or "get").lower()
        fid = attr(open_tag, "id") or ""
        fields: Dict[str, str] = {}
        for inp in INPUT_RE.findall(match.group(2)):
            name = attr(inp, "name")
            if not name:
                continue
            if (attr(inp, "type") or "").lower() in ("submit", "button", "image", "reset", "file"):
                continue
            fields[name] = attr(inp, "value") or ""
        forms.append(Form(urljoin(base, action), method, fields, fid))
    return forms


def meta_refresh(page: str) -> Optional[str]:
    for tag in META_REFRESH_RE.findall(page):
        content = attr(tag, "content")
        if not content:
            continue
        m = re.search(r"url\s*=\s*([\"']?)(.+?)\1\s*$", content, re.I | re.S)
        if m:
            return m.group(2).strip()
    return None


def js_redirect(page: str) -> Optional[str]:
    for pattern in JS_REDIRECT_RES:
        for m in pattern.finditer(page):
            url = m.group(2).replace("\\/", "/").replace("\\u0026", "&")
            if url.startswith(("http://", "https://")):
                return url
    return None


def interstitial_anchor(page: str, base: str) -> Optional[str]:
    labels = {"continue", "proceed", "get link", "click here", "download", "go to link", "skip ad"}
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", page, re.I | re.S):
        href = attr("<a" + m.group(1) + ">", "href")
        if not href or href.startswith(("javascript:", "#", "mailto:", "tel:")):
            continue
        text = htmlmod.unescape(re.sub(r"<[^>]+>", "", m.group(2)))
        text = re.sub(r"\s+", " ", text).strip().lower()
        if text in labels:
            return urljoin(base, href)
    return None


# --------------------------------------------------------------------------- resolver


@dataclass
class Hop:
    method: str
    url: str
    status: Optional[int]
    final_url: str
    redirects: List[Tuple[int, Optional[str]]] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "method": self.method,
            "url": self.url,
            "status": self.status,
            "landed": self.final_url,
            "redirects": [{"status": s, "location": loc} for s, loc in self.redirects],
            "note": self.note,
        }


class Reporter:
    def __init__(
        self, enabled: bool = True, color_mode: str = "auto", verbose: bool = False
    ) -> None:
        self.enabled = enabled
        self.verbose = verbose
        self.stream = sys.stdout
        self.tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.color = _supports_color(self.stream, color_mode)
        if self.color and self.tty and os.name == "nt":
            self.color = _enable_windows_vt()
        self.g = _glyphs()
        self.width = max(40, min(shutil.get_terminal_size((100, 24)).columns, 160))

    def paint(self, text: str, *codes: str) -> str:
        if not self.color or not codes:
            return text
        return "".join(codes) + text + RESET

    def banner(self) -> None:
        if not self.enabled:
            return
        inner = 44
        title = "shortlink-bypass"
        tagline = "shortxlinks / adlinkfly resolver"
        attribution = "by stav3ng3r"
        top = self.paint(self.g.tl + self.g.rule * (inner + 2) + self.g.tr, DIM)
        bottom = self.paint(self.g.bl + self.g.rule * (inner + 2) + self.g.br, DIM)
        rendered_attribution = self.paint("by ", DIM) + self.paint("stav3ng3r", CYAN, BOLD)

        def row(plain: str, rendered: str) -> str:
            pad = " " * max(0, inner - len(plain))
            return "%s %s%s %s" % (
                self.paint(self.g.pipe, DIM),
                rendered,
                pad,
                self.paint(self.g.pipe, DIM),
            )

        print()
        print(top)
        print(row(title, self.paint(title, BOLD, CYAN)))
        print(row(tagline, self.paint(tagline, DIM)))
        print(row(attribution, rendered_attribution))
        print(bottom)

    def header(self, url: str) -> None:
        if not self.enabled:
            return
        print()
        print(self.paint("input", DIM) + "  " + shorten(url, max(30, self.width - 7)))
        print()

    def hop(self, hop: Hop, index: int) -> None:
        if not self.enabled or not self.verbose:
            return
        method_color = CYAN if hop.method == "GET" else MAGENTA if hop.method == "POST" else BLUE
        method = self.paint("%-4s" % hop.method, method_color)
        prefix_plain = "[%d] %s " % (index, hop.method.ljust(4))
        print(prefix_plain.replace(hop.method.ljust(4), method) + shorten(hop.url, max(30, self.width - len(prefix_plain))))

        has_status = hop.status is not None
        has_note = bool(hop.note)
        show_redirects = self.verbose and bool(hop.redirects)

        if has_status:
            branch = self.g.branch if (has_note or show_redirects) else self.g.last
            color = GREEN if 200 <= hop.status < 300 else YELLOW if 300 <= hop.status < 400 else RED
            line = "    %s %s" % (self.paint(branch, DIM), self.paint(str(hop.status), color, BOLD))
            if hop.final_url and hop.final_url != hop.url:
                line += " %s %s" % (
                    self.paint(self.g.arrow, DIM),
                    shorten(hop.final_url, max(24, self.width - 14)),
                )
            if hop.redirects:
                line += self.paint(
                    "  (%d redirect%s)" % (len(hop.redirects), "" if len(hop.redirects) == 1 else "s"),
                    DIM,
                )
            print(line)

        if show_redirects:
            indent = "    %s   " % self.paint(self.g.pipe, DIM)
            for status, location in hop.redirects:
                print(
                    "%s%s %s"
                    % (
                        indent,
                        self.paint(str(status), DIM),
                        shorten(location or "", max(24, self.width - 12)),
                    )
                )

        if has_note:
            note = hop.note
            if self.g.arrow != "->":
                note = note.replace(" -> ", " %s " % self.g.arrow)
            print("    %s %s" % (self.paint(self.g.last, DIM), self.paint(note, DIM)))

    def wait(self, seconds: float) -> None:
        if not self.enabled:
            time.sleep(seconds)
            return
        seconds = max(0.0, float(seconds))
        if not self.tty:
            print("    %s waiting %ds for server timer" % (self.g.wait, round(seconds)), file=sys.stderr)
            time.sleep(seconds)
            return

        label = "    %s %s" % (self.paint(self.g.wait, YELLOW), self.paint("server timer", DIM))
        bar_width = min(20, max(10, self.width - 44))
        start = time.monotonic()
        clear = "\x1b[K" if self.color else ""
        try:
            while True:
                remaining = seconds - (time.monotonic() - start)
                if remaining <= 0:
                    break
                fraction = 1.0 - (remaining / seconds if seconds else 1.0)
                filled = int(fraction * bar_width)
                bar = self.paint(self.g.full * filled, CYAN) + self.paint(
                    self.g.empty * (bar_width - filled), GREY
                )
                self.stream.write(("\r%s  [%s] %4.1fs" % (label, bar, remaining)) + clear)
                self.stream.flush()
                time.sleep(0.1)
        finally:
            self.stream.write("\r" + " " * 120 + "\r")
            self.stream.flush()

    def result(
        self,
        final: Optional[str],
        error: Optional[str],
        last: Optional[str],
        hops_count: int,
    ) -> None:
        if not self.enabled:
            return
        print()
        count = "%d hop%s" % (hops_count, "" if hops_count == 1 else "s")
        if final and error is None:
            print(self.paint("%s %s bypassed" % (self.g.ok, count), GREEN, BOLD))
            print(self.paint(final, GREEN, BOLD))
        elif final:
            print(
                self.paint("%s blocked after %s" % (self.g.fail, count), YELLOW, BOLD)
                + "  "
                + self.paint(error or "", YELLOW)
            )
            print(self.paint("last  ", DIM) + final)
        else:
            print(
                self.paint("%s failed after %s" % (self.g.fail, count), RED, BOLD)
                + "  "
                + self.paint(error or "no destination found", RED)
            )
            if last:
                print(self.paint("last  ", DIM) + last)

    def check(self, status: Optional[int], url: str, error: Optional[str] = None) -> None:
        if not self.enabled:
            return
        if status is None:
            print(
                "  "
                + self.paint("check", DIM)
                + "  "
                + self.paint("unreachable", RED)
                + "  "
                + (error or "")
            )
            return
        color = GREEN if 200 <= status < 300 else YELLOW if 300 <= status < 400 else RED
        print(
            "  "
            + self.paint("check", DIM)
            + "  "
            + self.paint("HTTP %d" % status, color, BOLD)
            + "  "
            + shorten(url, max(20, self.width - 20))
        )


class Resolver:
    def __init__(
        self,
        timeout: float = 30.0,
        max_hops: int = 15,
        max_wait: float = 90.0,
        insecure: bool = False,
        verbose: bool = False,
    ) -> None:
        self.timeout = timeout
        self.max_hops = max_hops
        self.max_wait = max_wait
        self.verbose = verbose
        self.last_delay = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        self.session.max_redirects = 12
        self.session.verify = not insecure

    # -- transport ---------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        data: Optional[Dict[str, str]] = None,
        referer: Optional[str] = None,
        ajax: bool = False,
    ) -> requests.Response:
        headers: Dict[str, str] = {}
        if referer:
            headers["Referer"] = referer
        if ajax:
            parsed = urlparse(url)
            headers["X-Requested-With"] = "XMLHttpRequest"
            headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
            headers["Origin"] = "%s://%s" % (parsed.scheme, parsed.netloc)
        return self.session.request(
            method,
            url,
            data=data,
            headers=headers or None,
            timeout=self.timeout,
            allow_redirects=True,
        )

    # -- page classification ----------------------------------------------

    def classify(self, response: requests.Response) -> Optional[dict]:
        page = response.text or ""
        title = ""
        m = TITLE_RE.search(page)
        if m:
            title = htmlmod.unescape(re.sub(r"\s+", " ", m.group(1))).strip()

        content_type = (response.headers.get("content-type") or "").lower()
        head = page.lstrip()[:1]
        if "json" in content_type or head in ("{", "["):
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                if payload.get("status") in ("error", "fail") and not any(
                    k in payload for k in ("url", "link", "destination", "target")
                ):
                    return {"kind": "blocked", "reason": str(payload.get("message") or "server error")}
                for key in ("url", "link", "destination", "target", "redirect"):
                    value = payload.get(key)
                    if isinstance(value, str) and value.startswith("http"):
                        return {"kind": "final", "url": value, "note": "json." + key}

        if re.search(r"cf-chl-|cdn-cgi/challenge-platform|Just a moment\.\.\.", page, re.I):
            return {"kind": "blocked", "reason": "Cloudflare / JS challenge"}

        forms = parse_forms(page, response.url)
        for form in forms:
            if "adcopy_challenge" in form.fields and "adcopy_response" in form.fields:
                return {"kind": "blocked", "reason": "captcha required (adcopy)"}

        if re.search(r"Too\s*Early", title or page[:2000], re.I):
            return {
                "kind": "retry",
                "delay": self.last_delay or 25.0,
                "note": "server timer (Too Early)",
            }

        # WordPress safelink payload: linkr / second_safelink_url / newsafelink.
        for form in forms:
            if "newwpsafelink" not in form.fields:
                continue
            decoded = b64_text(form.fields["newwpsafelink"])
            if not decoded:
                continue
            try:
                payload = json.loads(decoded)
            except ValueError:
                continue
            try:
                delay = float(payload.get("delay") or 0)
            except (TypeError, ValueError):
                delay = 0.0
            if delay > 0:
                self.last_delay = max(self.last_delay, delay)
            target = payload.get("linkr")
            for key in ("second_safelink_url", "newsafelink"):
                if not target and isinstance(payload.get(key), str):
                    candidate = payload[key].strip()
                    if candidate.startswith("http"):
                        target = candidate
            note = "safelink payload"
            if delay:
                note += " (timer %gs)" % delay
            if target:
                return {"kind": "get", "url": urljoin(response.url, target), "note": note}
            return {"kind": "post", "url": form.action, "fields": form.fields, "note": note + " form"}

        # Final shortener form (CakePHP): ad_form_data -> /links/go JSON.
        for form in forms:
            if (
                "ad_form_data" in form.fields
                or form.fid == "go-link"
                or form.action.rstrip("/").endswith("/links/go")
            ):
                return {
                    "kind": "post",
                    "url": form.action,
                    "fields": form.fields,
                    "ajax": True,
                    "note": "destination form",
                }

        # Generic gate form: hidden `go` = base64(next URL) or opaque token.
        for form in forms:
            if "go" not in form.fields:
                continue
            decoded = b64_text(form.fields["go"]) or ""
            note = "gate form (go)"
            if decoded.startswith("http"):
                note = "gate form (go -> %s)" % shorten(decoded, 70)
            if form.method == "get":
                separator = "&" if "?" in form.action else "?"
                return {
                    "kind": "get",
                    "url": form.action + separator + urlencode(form.fields),
                    "note": note,
                }
            return {"kind": "post", "url": form.action, "fields": form.fields, "note": note}

        refresh = meta_refresh(page)
        if refresh:
            return {"kind": "get", "url": urljoin(response.url, refresh), "note": "meta refresh"}

        assignment = js_redirect(page)
        if assignment:
            return {"kind": "get", "url": urljoin(response.url, assignment), "note": "JS redirect"}

        if INTERSTITIAL_RE.search(title) or INTERSTITIAL_RE.search(page[:8000]):
            anchor = interstitial_anchor(page, response.url)
            if anchor:
                return {"kind": "get", "url": anchor, "note": "continue link"}

        return None

    # -- main loop ---------------------------------------------------------

    def resolve(
        self,
        url: str,
        on_hop: Optional[Callable[[Hop, int], None]] = None,
        wait_fn: Optional[Callable[[float], None]] = None,
    ) -> Tuple[Optional[str], List[Hop], Optional[str]]:
        hops: List[Hop] = []
        method, target, data = "GET", url, None
        referer: Optional[str] = None
        ajax = False
        visited = set()
        retries = 0

        for _ in range(self.max_hops):
            key = (method, target, json.dumps(data, sort_keys=True) if data else "")
            if key in visited:
                hop = Hop(method, target, None, "", [], "loop detected")
                hops.append(hop)
                if on_hop:
                    on_hop(hop, len(hops))
                return None, hops, "redirect loop"
            visited.add(key)

            try:
                response = self.request(method, target, data=data, referer=referer, ajax=ajax)
            except requests.RequestException as exc:
                hop = Hop(method, target, None, "", [], "error: %s" % exc)
                hops.append(hop)
                if on_hop:
                    on_hop(hop, len(hops))
                return None, hops, "request failed: %s" % exc

            redirects = [(h.status_code, h.headers.get("Location")) for h in response.history]
            step = self.classify(response)
            hop = Hop(
                method,
                target,
                response.status_code,
                response.url,
                redirects,
                step.get("note", "") if step else "",
            )
            hops.append(hop)
            if on_hop:
                on_hop(hop, len(hops))

            if step is None:
                return response.url, hops, None

            kind = step["kind"]
            if kind == "final":
                return step["url"], hops, None
            if kind == "blocked":
                return response.url, hops, step["reason"]
            if kind == "retry":
                retries += 1
                if retries > 2:
                    return None, hops, "server timer never cleared"
                wait = min(max(float(step["delay"]) + 2.0, 5.0), self.max_wait)
                if wait_fn:
                    wait_fn(wait)
                else:
                    time.sleep(wait)
                visited.discard(key)  # same URL is re-requested after the timer
                method, target, data, ajax = "GET", response.url, None, False
                referer = None
                continue
            if kind == "get":
                method, target, data, ajax = "GET", step["url"], None, False
                referer = response.url
                continue
            if kind == "post":
                method, target, data = "POST", step["url"], step["fields"]
                ajax = bool(step.get("ajax"))
                referer = response.url
                continue

        return None, hops, "too many hops (limit %d)" % self.max_hops


# --------------------------------------------------------------------------- output


def check_url(url: str, timeout: float) -> dict:
    try:
        response = requests.head(
            url, allow_redirects=True, timeout=timeout, headers={"User-Agent": UA}
        )
        if response.status_code >= 400:
            response.close()
            response = requests.get(
                url, allow_redirects=True, timeout=timeout, headers={"User-Agent": UA}, stream=True
            )
            response.close()
        return {"status": response.status_code, "url": response.url}
    except requests.RequestException as exc:
        return {"status": None, "error": str(exc)}


# --------------------------------------------------------------------------- cli


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bypass.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Resolve shortxlinks.com ad-gateway links to the final destination without ads or JS.",
        epilog=(
            "Handles the shortxlinks.com chain: HTTP 3xx, meta refresh, location assignments,\n"
            "adlinkfly `go` forms, WordPress safelink `newwpsafelink` payloads, and /links/go JSON.\n"
            "Other providers work only if they reuse the same forms; captchas are never solved."
        ),
    )
    parser.add_argument("urls", nargs="+", metavar="URL", help="short URL(s) to resolve")
    parser.add_argument("-v", "--verbose", action="store_true", help="show every redirect hop")
    parser.add_argument("--json", action="store_true", dest="as_json", help="machine-readable output")
    parser.add_argument("--check", action="store_true", help="request the final URL and report its status")
    parser.add_argument("--timeout", type=float, default=30.0, metavar="SEC")
    parser.add_argument("--max-hops", type=int, default=15, metavar="N")
    parser.add_argument("--max-wait", type=float, default=90.0, metavar="SEC")
    parser.add_argument("--insecure", action="store_true", help="skip TLS verification")
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="colorize output: auto, always, never (default: auto)",
    )
    args = parser.parse_args(argv)

    reporter = Reporter(enabled=not args.as_json, color_mode=args.color, verbose=args.verbose)
    if not args.as_json:
        reporter.banner()
    results = []
    failed = False

    for position, raw in enumerate(args.urls):
        url = normalize_url(raw)
        if not url.startswith(("http://", "https://")):
            print("error: not an http(s) URL: %s" % raw, file=sys.stderr)
            failed = True
            continue

        if not args.as_json and args.verbose:
            reporter.header(url)

        resolver = Resolver(
            timeout=args.timeout,
            max_hops=args.max_hops,
            max_wait=args.max_wait,
            insecure=args.insecure,
            verbose=args.verbose,
        )
        final, hops, error = resolver.resolve(
            url,
            on_hop=None if args.as_json else reporter.hop,
            wait_fn=None if args.as_json else reporter.wait,
        )

        result = {
            "input": url,
            "final": final,
            "ok": bool(final) and error is None,
            "error": error,
            "hops": [hop.as_dict() for hop in hops],
        }
        if final and args.check:
            result["check"] = check_url(final, args.timeout)

        results.append(result)
        if error:
            failed = True

        if not args.as_json:
            last = (hops[-1].final_url or hops[-1].url) if hops else None
            reporter.result(final, error, last, len(hops))
            if "check" in result:
                checked = result["check"]
                reporter.check(
                    checked.get("status"),
                    checked.get("url", final or ""),
                    checked.get("error"),
                )

    if args.as_json:
        print(json.dumps(results if len(results) > 1 else results[0], indent=2))

    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
