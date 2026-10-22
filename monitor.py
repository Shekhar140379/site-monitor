#!/usr/bin/env python3
"""
Website availability monitor - runs on GitHub Actions (no local machine / server needed).

Checks every site in sites.json for:
  - DNS resolution failure
  - Connection refused / timeout
  - SSL certificate problems (expired, wrong domain, untrusted, self-signed)
  - SSL certificate expiring soon (warning)
  - Redirect to non-secure http://
  - HTTP errors (4xx / 5xx)
  - Missing expected text on the page (optional "keyword")
  - Very slow response (warning)

Alerts go to Telegram, email and/or Slack - whichever secrets are configured.
Sends: DOWN alert, reminder while still down, RECOVERED alert, and a daily "all good" heartbeat
so you know the monitor itself is alive.

Standard library only - no pip install needed.
"""

import json
import os
import smtplib
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

HERE = os.path.dirname(os.path.abspath(__file__))
SITES_FILE = os.path.join(HERE, "sites.json")
STATE_FILE = os.path.join(HERE, "state.json")

IST = timezone(timedelta(hours=5, minutes=30))

# Tunables (can be overridden with environment variables)
TIMEOUT = int(os.getenv("CHECK_TIMEOUT", "20"))                 # seconds per request
RETRY_DELAY = int(os.getenv("RETRY_DELAY", "30"))               # wait before confirming a failure
SSL_WARN_DAYS = int(os.getenv("SSL_WARN_DAYS", "14"))           # warn when cert expires within N days
SLOW_SECONDS = float(os.getenv("SLOW_SECONDS", "10"))           # warn when page slower than this
REMINDER_MINUTES = int(os.getenv("REMINDER_MINUTES", "30"))     # repeat DOWN alert every N minutes
HEARTBEAT_HOUR_IST = int(os.getenv("HEARTBEAT_HOUR_IST", "9"))  # daily "all OK" message at/after this hour

USER_AGENT = "Mozilla/5.0 (compatible; SiteUptimeMonitor/1.0)"

# Certificate authorities that browsers have distrusted/blocked. The monitor's own trust store
# (Ubuntu on GitHub) may lag behind browsers, so flag these explicitly.
DISTRUSTED_ISSUERS = ["emsign", "emudhra"]


# --------------------------------------------------------------------------- checks

def check_site(site):
    """Return dict: status in {"UP","WARN","DOWN"}, reason, details."""
    url = site["url"]
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    warnings = []
    details = {}

    # 1. DNS
    try:
        socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        return {"status": "DOWN", "reason": f"DNS lookup failed for {host} ({e})", "details": details}

    # 2. SSL certificate (only for https)
    if parsed.scheme == "https":
        ctx = ssl.create_default_context()
        try:
            with socket.create_connection((host, port), timeout=TIMEOUT) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as tls:
                    cert = tls.getpeercert()
            expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), tz=timezone.utc)
            days_left = (expires - datetime.now(timezone.utc)).days
            details["ssl_expires"] = expires.strftime("%d %b %Y")
            details["ssl_days_left"] = days_left
            issuer = {k: v for rdn in cert.get("issuer", ()) for k, v in rdn}
            issuer_name = issuer.get("commonName") or issuer.get("organizationName") or "unknown"
            details["ssl_issuer"] = issuer_name
            issuer_text = " ".join(issuer.values()).lower()
            if any(bad in issuer_text for bad in DISTRUSTED_ISSUERS):
                warnings.append(
                    f"SSL certificate is issued by '{issuer_name}' (eMudhra/emSign). In Oct 2026 browsers "
                    f"began blocking compromised eMudhra intermediates and Chrome is removing the emSign "
                    f"ECC Root CA-G3 - visitors may see 'Not Secure' errors even though this check passes. "
                    f"Replace the certificate with one from a different authority.")
            if days_left <= SSL_WARN_DAYS:
                warnings.append(f"SSL certificate expires in {days_left} day(s) on {details['ssl_expires']}")
        except ssl.SSLCertVerificationError as e:
            msg = getattr(e, "verify_message", None) or str(e)
            return {"status": "DOWN",
                    "reason": f"SSL certificate error - browsers will show 'Not Secure': {msg}",
                    "details": details}
        except ssl.SSLError as e:
            return {"status": "DOWN", "reason": f"SSL/TLS handshake failed: {e}", "details": details}
        except socket.timeout:
            return {"status": "DOWN", "reason": f"Connection timed out after {TIMEOUT}s", "details": details}
        except ConnectionRefusedError:
            return {"status": "DOWN", "reason": f"Connection refused on port {port}", "details": details}
        except OSError as e:
            return {"status": "DOWN", "reason": f"Cannot connect: {e}", "details": details}

    # 3. HTTP request (follows redirects)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl.create_default_context()) as resp:
            body = resp.read(2_000_000).decode("utf-8", errors="ignore")
            code = resp.status
            final_url = resp.geturl()
    except urllib.error.HTTPError as e:
        return {"status": "DOWN", "reason": f"Server returned HTTP {e.code} {e.reason}", "details": details}
    except urllib.error.URLError as e:
        r = e.reason
        if isinstance(r, ssl.SSLCertVerificationError):
            msg = getattr(r, "verify_message", None) or str(r)
            return {"status": "DOWN", "reason": f"SSL certificate error after redirect: {msg}", "details": details}
        if isinstance(r, socket.timeout) or "timed out" in str(r):
            return {"status": "DOWN", "reason": f"Page did not respond within {TIMEOUT}s", "details": details}
        return {"status": "DOWN", "reason": f"Cannot open page: {r}", "details": details}
    except (socket.timeout, TimeoutError):
        return {"status": "DOWN", "reason": f"Page did not respond within {TIMEOUT}s", "details": details}
    except Exception as e:  # noqa: BLE001
        return {"status": "DOWN", "reason": f"Unexpected error: {type(e).__name__}: {e}", "details": details}

    elapsed = time.monotonic() - start
    details["http_status"] = code
    details["response_seconds"] = round(elapsed, 2)

    if parsed.scheme == "https" and final_url.startswith("http://"):
        return {"status": "DOWN",
                "reason": f"Site redirects to NON-SECURE address: {final_url}",
                "details": details}

    keyword = site.get("keyword")
    if keyword and keyword.lower() not in body.lower():
        return {"status": "DOWN",
                "reason": f"Page loaded (HTTP {code}) but expected text '{keyword}' is missing - "
                          f"possible error page, blank page or hacked content",
                "details": details}

    if elapsed > SLOW_SECONDS:
        warnings.append(f"Very slow: page took {elapsed:.1f}s to load")

    if warnings:
        return {"status": "WARN", "reason": "; ".join(warnings), "details": details}
    return {"status": "UP", "reason": "OK", "details": details}


def check_with_retry(site):
    """A failure must repeat after RETRY_DELAY seconds before we call it DOWN (avoids false alarms)."""
    result = check_site(site)
    if result["status"] == "DOWN":
        print(f"  first check failed ({result['reason']}), re-checking in {RETRY_DELAY}s...")
        time.sleep(RETRY_DELAY)
        result = check_site(site)
    return result


# --------------------------------------------------------------------------- alerts

def send_telegram(text):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    for chat_id in [c.strip() for c in chat.split(",") if c.strip()]:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text,
                                       "disable_web_page_preview": "true"}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=15)
        except Exception as e:  # noqa: BLE001
            print(f"  Telegram send failed: {e}")


def send_slack(text):
    hook = os.getenv("SLACK_WEBHOOK_URL")
    if not hook:
        return
    req = urllib.request.Request(hook, data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:  # noqa: BLE001
        print(f"  Slack send failed: {e}")


def _env(name, default=""):
    return (os.getenv(name) or default).strip()


def send_email(subject, text):
    host, user, to = _env("SMTP_HOST"), _env("SMTP_USER"), _env("ALERT_EMAIL_TO")
    pwd = _env("SMTP_PASS").replace(" ", "")  # Gmail app passwords are often pasted with spaces
    missing = [n for n, v in [("SMTP_HOST", host), ("SMTP_USER", user),
                              ("SMTP_PASS", pwd), ("ALERT_EMAIL_TO", to)] if not v]
    if missing:
        if len(missing) < 4:
            print(f"  Email skipped - missing secret(s): {', '.join(missing)}")
        return
    try:
        port = int(_env("SMTP_PORT", "587"))
    except ValueError:
        port = 587
    import re
    # accept commas, semicolons, spaces or new lines between addresses
    recipients = [a for a in re.split(r"[,;\s]+", to) if "@" in a]
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = _env("SMTP_FROM", user)
    msg["To"] = ", ".join(recipients)

    # Try the configured port first, then the other common one (587 STARTTLS <-> 465 SSL)
    attempts = [port] + [p for p in (587, 465) if p != port]
    for p in attempts:
        try:
            if p == 465:
                server = smtplib.SMTP_SSL(host, p, timeout=20, context=ssl.create_default_context())
            else:
                server = smtplib.SMTP(host, p, timeout=20)
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            server.login(user, pwd)
            refused = server.sendmail(msg["From"], recipients, msg.as_string())
            server.quit()
            print(f"  Email sent via {host}:{p} to {len(recipients) - len(refused)} of "
                  f"{len(recipients)} recipient(s)")
            for addr, (code, why) in refused.items():
                print(f"  Email REFUSED for {addr}: {code} {why!r}")
            return
        except smtplib.SMTPRecipientsRefused as e:
            for addr, (code, why) in e.recipients.items():
                print(f"  Email REFUSED for {addr}: {code} {why!r}")
            return
        except smtplib.SMTPAuthenticationError as e:
            print(f"  Email send failed ({host}:{p}): login rejected - check SMTP_USER / SMTP_PASS "
                  f"(Gmail needs an App Password). Server said: {e.smtp_code} {e.smtp_error!r}")
            return
        except Exception as e:  # noqa: BLE001
            print(f"  Email send failed ({host}:{p}): {type(e).__name__}: {e}")


def alert(subject, text):
    print(f"  ALERT -> {subject}")
    send_telegram(f"{subject}\n\n{text}")
    send_slack(f"*{subject}*\n{text}")
    send_email(subject, text)


# --------------------------------------------------------------------------- state

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def fmt_duration(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h}h {m}m" if h else f"{m}m"


def now_ist_str():
    return datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")


# --------------------------------------------------------------------------- main

def main():
    sites = load_json(SITES_FILE, [])
    if not sites:
        print("No sites configured in sites.json")
        sys.exit(1)

    state = load_json(STATE_FILE, {"sites": {}, "last_heartbeat": ""})
    state.setdefault("sites", {})
    now = time.time()
    results = {}

    for site in sites:
        name, url = site["name"], site["url"]
        print(f"Checking {name} ({url})")
        res = check_with_retry(site)
        results[name] = res
        print(f"  -> {res['status']}: {res['reason']} {res['details']}")

        prev = state["sites"].get(name, {"status": "UP", "since": now, "last_alert": 0, "reason": ""})
        prev_status = prev.get("status", "UP")
        cur = res["status"]

        if cur == "DOWN":
            if prev_status != "DOWN":
                alert(f"🔴 DOWN: {name}",
                      f"{url}\n\nProblem: {res['reason']}\nDetected: {now_ist_str()}\n\n"
                      f"Checked from GitHub cloud servers (outside your network), re-confirmed after "
                      f"{RETRY_DELAY}s.")
                state["sites"][name] = {"status": "DOWN", "since": now, "last_alert": now, "reason": res["reason"]}
            else:
                down_for = now - prev.get("since", now)
                if now - prev.get("last_alert", 0) >= REMINDER_MINUTES * 60 or res["reason"] != prev.get("reason"):
                    alert(f"🔴 STILL DOWN ({fmt_duration(down_for)}): {name}",
                          f"{url}\n\nProblem: {res['reason']}\nDown since: "
                          f"{datetime.fromtimestamp(prev.get('since', now), IST).strftime('%d %b %Y, %I:%M %p IST')}")
                    prev["last_alert"] = now
                    prev["reason"] = res["reason"]
                state["sites"][name] = prev

        else:  # UP or WARN
            if prev_status == "DOWN":
                down_for = now - prev.get("since", now)
                alert(f"✅ RECOVERED: {name}",
                      f"{url} is reachable again.\nTotal downtime: about {fmt_duration(down_for)}\n"
                      f"Recovered: {now_ist_str()}"
                      + (f"\n\nNote: {res['reason']}" if cur == "WARN" else ""))
                state["sites"][name] = {"status": cur, "since": now, "last_alert": now, "reason": res["reason"]}
            elif cur == "WARN":
                # warn when it first appears, then once a day while it persists
                if prev_status != "WARN" or res["reason"] != prev.get("reason") \
                        or now - prev.get("last_alert", 0) >= 24 * 3600:
                    alert(f"⚠️ WARNING: {name}", f"{url}\n\n{res['reason']}\nChecked: {now_ist_str()}")
                    state["sites"][name] = {"status": "WARN", "since": prev.get("since", now) if prev_status == "WARN" else now,
                                            "last_alert": now, "reason": res["reason"]}
            else:  # UP
                if prev_status != "UP" or name not in state["sites"]:
                    state["sites"][name] = {"status": "UP", "since": now, "last_alert": 0, "reason": "OK"}

    # daily heartbeat so you know the monitor is alive (and it keeps the repo active)
    ist_now = datetime.now(IST)
    today = ist_now.strftime("%Y-%m-%d")
    if ist_now.hour >= HEARTBEAT_HOUR_IST and state.get("last_heartbeat") != today:
        lines = []
        for site in sites:
            r = results[site["name"]]
            icon = {"UP": "✅", "WARN": "⚠️", "DOWN": "🔴"}[r["status"]]
            extra = []
            if "ssl_days_left" in r["details"]:
                extra.append(f"SSL valid {r['details']['ssl_days_left']}d")
            if "ssl_issuer" in r["details"]:
                extra.append(f"issued by {r['details']['ssl_issuer']}")
            if "response_seconds" in r["details"]:
                extra.append(f"{r['details']['response_seconds']}s")
            lines.append(f"{icon} {site['name']} - {r['status']}" + (f" ({', '.join(extra)})" if extra else ""))
        alert("📊 Daily website status", "\n".join(lines) + f"\n\nMonitor is running. {now_ist_str()}")
        state["last_heartbeat"] = today

    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)

    # Exit non-zero if anything is down, so the GitHub Actions run also shows red
    if any(r["status"] == "DOWN" for r in results.values()):
        sys.exit(2)


if __name__ == "__main__":
    main()
