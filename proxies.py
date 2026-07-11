"""Optional rotating proxy support for the CloakBrowser driver.

Loads proxies.txt if present (format: host:port:username:password per line).
Absent file -> empty list -> direct connections. The proxy is applied only to
the browser context, never to a server API call.
"""

import os

PROXIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxies.txt")


def parse_proxy(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = line.split(":")
    if len(parts) != 4:
        return None
    host, port, user, pwd = parts
    return {"server": f"http://{host}:{port}", "username": user, "password": pwd}


def load_proxies(path=PROXIES_FILE):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            px = parse_proxy(line)
            if px:
                out.append(px)
    return out


def proxy_for_index(index, proxies):
    if not proxies:
        return None
    return proxies[index % len(proxies)]
