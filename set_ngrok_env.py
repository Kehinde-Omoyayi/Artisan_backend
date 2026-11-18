"""Point backend/.env at whatever ngrok tunnels are running right now.

The tunnel hostname changes every time ngrok restarts, and three values in the
backend .env are absolute URLs that Flutterwave and the browser must reach:
SITE_URL (where the payer is sent after checkout) and the two callback URLs.
Everything else in .env is left exactly as it is.

    python set_ngrok_env.py --env C:\\path\\to\\backend\\.env

It reads the ngrok agent's own local API (http://127.0.0.1:4040/api/tunnels) and
works out which tunnel is the API (port 8000) and which is the web frontend.
If the agent has no local API (ngrok in Docker/WSL, `web_addr` disabled, a
cloud agent), pass the two addresses by hand instead:

    python set_ngrok_env.py --env .env ^
        --api-url https://aaaa.ngrok-free.app ^
        --web-url https://bbbb.ngrok-free.app

Add --dry-run to see the result without writing. Restart Django and the Celery
worker afterwards: .env is read once, at start-up.
"""

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request

VERSION = "7"

NGROK_HOSTS = ("127.0.0.1", "localhost")
NGROK_API_PORTS = (4040, 4041)  # ngrok's own default, then the next agent
NGROK_SCAN_ALL = range(4040, 4056)
DEFAULT_API_PORT = 8000

# key -> how to build the value from the two tunnel addresses
TEMPLATES = {
    "SITE_URL": lambda t: t["web"],
    "FLW_REDIRECT_URL": lambda t: f"{t['api']}/api/v1/payments/flutterwave/callback/",
    "FLW_TRANSFER_CALLBACK_URL": lambda t: f"{t['api']}/api/v1/payments/flutterwave/webhook/",
    "ALLOWED_HOSTS": lambda t: (
        "localhost,127.0.0.1,.ngrok-free.app,.ngrok-free.dev,.ngrok.app,.ngrok.dev,.ngrok.io"
    ),
    "CSRF_TRUSTED_ORIGINS": lambda t: (
        "http://localhost,http://127.0.0.1,"
        "https://*.ngrok-free.app,https://*.ngrok-free.dev,"
        "https://*.ngrok.app,https://*.ngrok.dev,https://*.ngrok.io"
    ),
}


def _port_of(addr: str) -> int | None:
    """'http://localhost:8000' -> 8000"""
    tail = str(addr or "").rstrip("/").rsplit(":", 1)
    if len(tail) != 2 or not tail[1].isdigit():
        return None
    return int(tail[1])


def _clean(url: str) -> str:
    return str(url or "").strip().rstrip("/")


def _placeholder(url: str) -> bool:
    """An example copied out of a doc is not an address."""
    from urllib.parse import urlparse

    text = str(url or "").strip().lower()
    if any(char in text for char in "<>{}"):
        return True
    host = urlparse(text).hostname or ""
    return any(
        token in host
        for token in ("api-tunnel", "web-tunnel", "your-", "example.com", "tunnel.example")
    )


def _looks_like_host(url: str) -> bool:
    from urllib.parse import urlparse

    host = (urlparse(str(url or "")).hostname or "").lower()
    if not host or "." not in host or " " in host:
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789.-")
    if not set(host) <= allowed:
        return False
    return all(label and not label.startswith("-") and not label.endswith("-") for label in host.split("."))


def discover_problems(ports=NGROK_API_PORTS) -> list:
    """One line per host:port we could not read, so the failure is explainable."""
    problems = []
    for host in NGROK_HOSTS:
        for port in ports:
            try:
                with urllib.request.urlopen(
                    f"http://{host}:{port}/api/tunnels", timeout=0.6
                ):
                    return problems  # something answered; nothing to report
            except urllib.error.HTTPError as exc:
                problems.append(f"http://{host}:{port} answered {exc.code}")
                return problems
            except (urllib.error.URLError, OSError):
                continue
    return problems


def _listening(host: str, port: int) -> bool:
    """Cheap check first: a closed port must not cost a full HTTP timeout."""
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def discover(ports=NGROK_API_PORTS) -> dict:
    tunnels: dict[int, str] = {}
    for host in NGROK_HOSTS:
        for port in ports:
            if not _listening(host, port):
                continue
            try:
                with urllib.request.urlopen(
                    f"http://{host}:{port}/api/tunnels", timeout=1.2
                ) as response:
                    payload = json.loads(response.read() or b"{}")
            except (urllib.error.URLError, OSError, ValueError):
                continue
            for tunnel in payload.get("tunnels") or []:
                url = _clean(tunnel.get("public_url"))
                addr_port = _port_of((tunnel.get("config") or {}).get("addr"))
                if url.startswith("https://") and addr_port:
                    tunnels.setdefault(addr_port, url)
    return tunnels


def pick(tunnels: dict, api_port: int, web_port: int | None = None) -> tuple[str, str]:
    """(api_url, web_url) from the discovered tunnels, or ('', reason)."""
    if web_port and web_port in tunnels:
        return tunnels.get(api_port, ""), tunnels[web_port]
    if api_port in tunnels:
        api_url = tunnels[api_port]
        others = [url for port, url in tunnels.items() if port != api_port]
        if len(others) == 1:
            return api_url, others[0]
        if not others:
            return api_url, ""
        return api_url, ""  # ambiguous: more than one other tunnel
    if len(tunnels) == 1:
        port, url = next(iter(tunnels.items()))
        if port == api_port:
            return url, ""  # only the API is exposed; the browser cannot reach the web app
        return "", url  # web only: single-tunnel mode
    return "", ""


SINGLE_TUNNEL = {
    "SITE_URL": lambda t: t["web"],
    "FLW_REDIRECT_URL": lambda t: f"{t['web']}/payments/callback/",
    "FLW_TRANSFER_CALLBACK_URL": lambda t: f"{t['web']}/payments/flutterwave/webhook/",
}


def build_values(api_url: str, web_url: str) -> dict:
    """One tunnel (web only) or two: both shapes are valid, the values differ."""
    if not api_url:
        found = {"web": web_url}
        return {key: make(found) for key, make in {**TEMPLATES, **SINGLE_TUNNEL}.items()}
    return {key: make({"api": api_url, "web": web_url}) for key, make in TEMPLATES.items()}


def rewrite(text: str, values: dict) -> tuple[str, list, list]:
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    seen, changed = set(), []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in values and key not in seen:
            seen.add(key)
            if lines[index] != f"{key}={values[key]}":
                lines[index] = f"{key}={values[key]}"
                changed.append(key)
    missing = [key for key in values if key not in seen]
    if missing:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Set by set_ngrok_env.py — do not hand-edit while a tunnel is up.")
        for key in missing:
            lines.append(f"{key}={values[key]}")
    return newline.join(lines).rstrip(newline) + newline, changed, missing


def resolve_env(given: str) -> str:
    """The .env to edit: what was passed, or the nearest one up the tree."""
    if given:
        path = os.path.abspath(os.path.expanduser(given))
        if os.path.isdir(path):
            path = os.path.join(path, ".env")
        return path
    here = os.path.abspath(os.getcwd())
    for _ in range(5):
        candidate = os.path.join(here, ".env")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return os.path.join(os.getcwd(), ".env")


def ask(label: str) -> str:
    if not sys.stdin or not sys.stdin.isatty():
        return ""
    try:
        return _clean(input(f"{label}: "))
    except EOFError:
        return ""


def main() -> int:
    print(f"set_ngrok_env.py {VERSION}")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="", help="path to the backend .env file (default: find .env from here)")
    parser.add_argument("--api-url", default="", help="public https URL of the API tunnel")
    parser.add_argument("--web-url", default="", help="public https URL of the web tunnel")
    parser.add_argument("--api-port", type=int, default=DEFAULT_API_PORT, help="local port the API tunnel forwards to")
    parser.add_argument("--web-port", type=int, default=0, help="local port the web tunnel forwards to, when it is not the only other one")
    parser.add_argument("--dry-run", action="store_true", help="print the values, change nothing")
    parser.add_argument("--scan-all", action="store_true", help="probe ports 4040-4055 instead of 4040-4041")
    parser.add_argument("--ask", action="store_true", help="prompt for the two addresses when discovery fails")
    args = parser.parse_args()

    api_url, web_url = _clean(args.api_url), _clean(args.web_url)

    ports = NGROK_SCAN_ALL if args.scan_all else NGROK_API_PORTS

    if not (api_url and web_url):
        tunnels = discover(ports)
        if tunnels:
            print("Tunnels found:")
            for port, url in sorted(tunnels.items()):
                print(f"  localhost:{port} -> {url}")
            found_api, found_web = pick(tunnels, args.api_port, args.web_port or None)
            api_url = api_url or found_api
            web_url = web_url or found_web
        else:
            print(f"No ngrok agent answered on http://127.0.0.1:{{{ports[0]}..{ports[-1]}}}/api/tunnels.")
            for line in discover_problems(ports):
                print("  " + line)
            print("Open http://127.0.0.1:4040 in a browser while ngrok runs: if it does not load,")
            print("the agent has no local web interface (Docker/WSL, web_addr off, or a cloud")
            print("agent) and the two addresses have to be pasted from the ngrok console.")
            if args.ask:
                api_url = api_url or ask("API tunnel  (ngrok http 8000)")
                web_url = web_url or ask("web tunnel  (ngrok http 8080)")

    if not web_url and api_url and len(tunnels) == 1:
        print(f"\nThe one tunnel points at the API (localhost:{args.api_port}), but the")
        print("browser has to reach the web app. Stop ngrok (Ctrl+C) and start it again as")
        print("\n    ngrok http 8080\n")
        print("then run this script once more.")
        return 2

    if not web_url:
        print("\nNeed the web address at least — the address on the Forwarding line")
        print("of the window whose target is localhost:8080.")
        return 2

    bad = [url for url in (api_url, web_url) if url and not url.startswith("https://")]
    if bad:
        print("These must be the https:// forwarding addresses, not http://localhost:")
        for url in bad:
            print("  " + url)
        return 2

    placeholders = [url for url in (api_url, web_url) if url and _placeholder(url)]
    if placeholders:
        print("These are still placeholders, not tunnel addresses:")
        for url in placeholders:
            print("  " + url)
        print("Copy the address ngrok prints after 'Forwarding' — it looks like")
        print("  https://1a2b-3c4d-5e6f.ngrok-free.app")
        return 2

    malformed = [url for url in (api_url, web_url) if url and not _looks_like_host(url)]
    if malformed:
        print("These do not look like host names:")
        for url in malformed:
            print("  " + url)
        return 2

    print("\nAPI tunnel : " + (api_url or "none — the web app will call it over localhost"))
    print("web tunnel : " + web_url)

    values = build_values(api_url, web_url)
    if not api_url:
        print(
            "\nSingle tunnel: the API is not exposed. The web app forwards the provider\n"
            "callback and webhook to it over localhost, so paying still completes."
        )
    print("\nValues for backend .env:")
    for key, value in values.items():
        print(f"  {key}={value}")

    env_path = resolve_env(args.env)
    print("\n.env file  : " + env_path)
    try:
        with open(env_path, "r", encoding="utf-8-sig") as handle:
            original = handle.read()
    except FileNotFoundError:
        print("\nNo .env there. Run this from the backend folder (the one with manage.py),")
        print("or pass the file:  --env \"C:\\full\\path\\to\\.env\"")
        return 2
    except OSError as exc:
        print(f"\nCould not read {env_path}: {exc}")
        return 2

    stale = [
        line.strip()
        for line in original.splitlines()
        if "://" in line and ("<" in line or ">" in line)
    ]
    if stale:
        print("\nThis file already contains placeholder URLs — they are being replaced:")
        for line in stale:
            print("  " + line)

    updated, changed, missing = rewrite(original, values)
    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    try:
        with open(env_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
    except OSError as exc:
        print(f"\nCould not write {env_path}: {exc} (is it open in an editor?)")
        return 2

    leftovers = [
        line.strip()
        for line in updated.splitlines()
        if "://" in line and ("<" in line or ">" in line)
    ]
    if leftovers:
        print("\nWarning: these lines still contain placeholders and will break the redirect:")
        for line in leftovers:
            print("  " + line)

    print(f"\nWrote {env_path}")
    if changed:
        print("  updated: " + ", ".join(changed))
    if missing:
        print("  added:   " + ", ".join(missing))
    if not changed and not missing:
        print("  already correct")
    print("\nNow restart Django and the Celery worker — .env is read at start-up.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped. Nothing was written.")
        sys.exit(130)
