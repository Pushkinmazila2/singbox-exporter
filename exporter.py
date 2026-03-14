#!/usr/bin/env python3
"""
singbox-exporter — Prometheus exporter for sing-box via Clash API

Sources:
  GET /version      → singbox_info
  GET /traffic SSE  → singbox_traffic_bytes_total (global, per-second stream)
  GET /connections  → per-connection tracking, closed connection accumulation
  GET /proxies      → singbox_proxy_up
"""

import os
import re
import json
import time
import logging
import threading
import requests
from prometheus_client import (
    start_http_server,
    Counter,
    Gauge,
    Info,
    REGISTRY,
    PROCESS_COLLECTOR,
    PLATFORM_COLLECTOR,
    GC_COLLECTOR,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("singbox-exporter")

# ── Connection logger (JSON → stdout → Alloy → Loki) ──────────────────────────
# Separate logger that writes one JSON line per closed connection.
# Alloy picks this up from container stdout and ships to Loki.
_conn_log = logging.getLogger("singbox.connections")
_conn_log.propagate = False  # don't leak into root logger

_conn_handler = logging.StreamHandler()
_conn_handler.setFormatter(logging.Formatter("%(message)s"))
_conn_log.addHandler(_conn_handler)
_conn_log.setLevel(logging.INFO)

# ── Config ─────────────────────────────────────────────────────────────────────
CLASH_API_URL      = os.getenv("CLASH_API_URL", "http://127.0.0.1:9090")
CLASH_API_SECRET   = os.getenv("CLASH_API_SECRET", "")
EXPORTER_PORT      = int(os.getenv("EXPORTER_PORT", "9101"))
CONN_POLL_INTERVAL = int(os.getenv("CONN_POLL_INTERVAL", "5"))  # seconds

# ── Remove noisy default collectors ───────────────────────────────────────────
REGISTRY.unregister(PROCESS_COLLECTOR)
REGISTRY.unregister(PLATFORM_COLLECTOR)
REGISTRY.unregister(GC_COLLECTOR)

# ── Metrics ────────────────────────────────────────────────────────────────────

singbox_info = Info(
    "singbox",
    "sing-box version and build info",
)

# Global traffic — fed by /traffic SSE stream (every second, never loses bytes)
singbox_traffic_bytes = Counter(
    "singbox_traffic_bytes_total",
    "Total bytes through sing-box (from /traffic SSE stream)",
    ["direction"],  # upload | download
)

# Per-flow traffic — accumulated from closed connections
singbox_flow_bytes = Counter(
    "singbox_flow_bytes_total",
    "Bytes transferred per flow (accumulated from closed connections)",
    ["inbound_protocol", "inbound_tag", "outbound", "rule_outbound", "direction"],
)

# Active connections snapshot
singbox_connections_active = Gauge(
    "singbox_connections_active",
    "Currently active connections",
    ["inbound_protocol", "inbound_tag", "outbound", "rule_outbound"],
)

# Memory
singbox_memory_bytes = Gauge(
    "singbox_memory_bytes",
    "Memory used by sing-box (bytes)",
)

# Proxies / outbounds
singbox_proxy_up = Gauge(
    "singbox_proxy_up",
    "Outbound/proxy defined in sing-box config (always 1, info in labels)",
    ["proxy", "type"],
)

# Per-endpoint scrape health
singbox_scrape_success = Gauge(
    "singbox_scrape_success",
    "1 if last scrape of this endpoint succeeded, 0 otherwise",
    ["endpoint"],  # traffic | connections | proxies | version
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def make_session() -> requests.Session:
    s = requests.Session()
    if CLASH_API_SECRET:
        s.headers.update({"Authorization": f"Bearer {CLASH_API_SECRET}"})
    return s


def parse_inbound(metadata_type: str) -> tuple[str, str]:
    """'vless/vless-13423' → ('vless', 'vless-13423')"""
    if "/" in metadata_type:
        proto, tag = metadata_type.split("/", 1)
        return proto, tag
    return metadata_type, metadata_type


def parse_rule(rule_str: str) -> str:
    """
    Extract outbound name from rule string.

    'final'
      → 'final'
    'rule_set=[geosite-ru-blocked ...] => route(ServerNL)'
      → 'ServerNL'
    'protocol=dns port=53 => hijack-dns'
      → 'hijack-dns'
    """
    m = re.search(r"=>\s*(?:route\(([^)]+)\)|(\S+))\s*$", rule_str)
    if m:
        return m.group(1) or m.group(2)
    return rule_str


def format_bytes(n: int) -> str:
    """1234567 → '1.18MB'"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.2f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.2f}TB"


def log_connection(conn: dict, upload: int, download: int) -> None:
    """
    Emit one JSON log line when a connection closes.
    JSON contains all fields for Loki label extraction
    plus a human-readable 'message' field for Grafana Explore.

    Example message:
      Inbound/vless[vless-13423] (77.37.196.126) -> api.telegram.org:443 [TCP]
      -> Outbound[ServerNL] | Rule: rule_set => route(ServerNL) | ↑1.2KB ↓45.3KB
    """
    meta          = conn.get("metadata", {})
    proto, tag    = parse_inbound(meta.get("type", "unknown"))
    source_ip     = meta.get("sourceIP", "")
    host          = meta.get("host") or meta.get("destinationIP", "")
    port          = meta.get("destinationPort", "")
    network       = meta.get("network", "").upper()
    outbound      = conn.get("chains", ["unknown"])[0]
    rule_raw      = conn.get("rule", "unknown")
    rule_outbound = parse_rule(rule_raw)
    start         = conn.get("start", "")

    dest = f"{host}:{port}" if host else port

    # Truncate very long rule strings for readability but keep outbound
    rule_display = rule_raw if len(rule_raw) <= 80 else rule_raw[:77] + "..."

    message = (
        f"Inbound/{proto}[{tag}] ({source_ip}) -> {dest} [{network}]"
        f" -> Outbound[{outbound}] | Rule: {rule_display}"
        f" | ↑{format_bytes(upload)} ↓{format_bytes(download)}"
    )

    record = {
        "message":          message,
        "ts":               start,
        "inbound_protocol": proto,
        "inbound_tag":      tag,
        "source_ip":        source_ip,
        "host":             host,
        "port":             port,
        "network":          network.lower(),
        "outbound":         outbound,
        "rule_raw":         rule_raw,
        "rule_outbound":    rule_outbound,
        "upload_bytes":     upload,
        "download_bytes":   download,
    }
    _conn_log.info(json.dumps(record, ensure_ascii=False))


def flow_key(conn: dict) -> tuple[str, str, str, str]:
    proto, tag = parse_inbound(conn.get("metadata", {}).get("type", "unknown"))
    outbound = conn.get("chains", ["unknown"])[0]
    rule_outbound = parse_rule(conn.get("rule", "unknown"))
    return proto, tag, outbound, rule_outbound


# ── Thread 1: /traffic SSE stream ─────────────────────────────────────────────

def traffic_stream_thread(session: requests.Session) -> None:
    """
    Streams /traffic endpoint (sing-box pushes JSON every second):
      {"up": N, "down": N}

    Increments global Counters — no bytes are ever lost regardless of
    how often Prometheus scrapes.
    Reconnects automatically on any error.
    """
    url = f"{CLASH_API_URL.rstrip('/')}/traffic"
    backoff = 1

    while True:
        try:
            log.info("Connecting to /traffic stream...")
            with session.get(url, stream=True, timeout=(5, None)) as resp:
                resp.raise_for_status()
                backoff = 1
                singbox_scrape_success.labels(endpoint="traffic").set(1)
                log.info("/traffic stream connected")

                for raw_line in resp.iter_lines():
                    if not raw_line:
                        continue
                    try:
                        data = json.loads(raw_line)
                        up   = data.get("up", 0)
                        down = data.get("down", 0)
                        if up > 0:
                            singbox_traffic_bytes.labels(direction="upload").inc(up)
                        if down > 0:
                            singbox_traffic_bytes.labels(direction="download").inc(down)
                    except Exception as e:
                        log.warning("/traffic parse error: %s | raw: %r", e, raw_line)

        except Exception as exc:
            singbox_scrape_success.labels(endpoint="traffic").set(0)
            log.error("/traffic error: %s — reconnect in %ds", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ── Thread 2: /connections poller ─────────────────────────────────────────────

class ConnectionTracker:
    """
    Polls /connections every CONN_POLL_INTERVAL seconds.

    Strategy:
    - Keep a dict of all known connection ids → {upload, download, flow_key}
    - Each poll: find ids that disappeared since last poll → those are closed
    - Closed connections: add their final byte counts to flow Counters
    - Active connections: update Gauge with current snapshot
    - Memory: update Gauge

    This way every byte is counted even for connections shorter than poll interval,
    as long as they appeared at least once in a poll response.
    """

    def __init__(self, session: requests.Session):
        self.session = session
        self._seen: dict[str, dict] = {}  # id → {upload, download, key}
        self._lock = threading.Lock()

    def poll(self) -> None:
        url = f"{CLASH_API_URL.rstrip('/')}/connections"
        try:
            r = self.session.get(url, timeout=5)
            r.raise_for_status()
            data = r.json()

            singbox_memory_bytes.set(data.get("memory", 0))

            current_ids: set[str] = set()
            agg_active: dict[tuple, int] = {}

            for conn in data.get("connections", []):
                cid  = conn["id"]
                key  = flow_key(conn)
                up   = conn.get("upload", 0)
                dl   = conn.get("download", 0)

                current_ids.add(cid)
                agg_active[key] = agg_active.get(key, 0) + 1

                with self._lock:
                    # Store full conn object so we can log it on close
                    self._seen[cid] = {"upload": up, "download": dl, "key": key, "conn": conn}

            # Find closed connections, flush bytes to Counters, emit log line
            with self._lock:
                closed_ids = set(self._seen.keys()) - current_ids
                for cid in closed_ids:
                    prev = self._seen.pop(cid)
                    ip, it, ob, ro = prev["key"]
                    up = prev["upload"]
                    dl = prev["download"]

                    # Emit JSON log → stdout → Alloy → Loki
                    log_connection(prev["conn"], up, dl)

                    if up > 0:
                        singbox_flow_bytes.labels(
                            inbound_protocol=ip, inbound_tag=it,
                            outbound=ob, rule_outbound=ro,
                            direction="upload",
                        ).inc(up)
                    if dl > 0:
                        singbox_flow_bytes.labels(
                            inbound_protocol=ip, inbound_tag=it,
                            outbound=ob, rule_outbound=ro,
                            direction="download",
                        ).inc(dl)

            # Refresh active Gauge
            singbox_connections_active.clear()
            for key, count in agg_active.items():
                ip, it, ob, ro = key
                singbox_connections_active.labels(
                    inbound_protocol=ip, inbound_tag=it,
                    outbound=ob, rule_outbound=ro,
                ).set(count)

            singbox_scrape_success.labels(endpoint="connections").set(1)
            log.debug("poll: %d active, %d closed this cycle",
                      len(current_ids), len(closed_ids))

        except Exception as exc:
            singbox_scrape_success.labels(endpoint="connections").set(0)
            log.error("/connections error: %s", exc)

    def run(self) -> None:
        while True:
            self.poll()
            time.sleep(CONN_POLL_INTERVAL)


# ── Thread 3: /version + /proxies (slow poller) ────────────────────────────────

def meta_poll_thread(session: requests.Session) -> None:
    """Polls /version and /proxies every 60s — these change rarely."""
    base = CLASH_API_URL.rstrip("/")

    while True:
        # /version
        try:
            r = session.get(f"{base}/version", timeout=5)
            r.raise_for_status()
            ver = r.json()
            singbox_info.info({
                "version": ver.get("version", "unknown"),
                "meta":    str(ver.get("meta", False)).lower(),
                "premium": str(ver.get("premium", False)).lower(),
            })
            singbox_scrape_success.labels(endpoint="version").set(1)
        except Exception as exc:
            singbox_scrape_success.labels(endpoint="version").set(0)
            log.error("/version error: %s", exc)

        # /proxies
        try:
            r = session.get(f"{base}/proxies", timeout=5)
            r.raise_for_status()
            proxies = r.json().get("proxies", {})
            singbox_proxy_up.clear()
            for name, info in proxies.items():
                singbox_proxy_up.labels(
                    proxy=name,
                    type=info.get("type", "unknown"),
                ).set(1)
            singbox_scrape_success.labels(endpoint="proxies").set(1)
        except Exception as exc:
            singbox_scrape_success.labels(endpoint="proxies").set(0)
            log.error("/proxies error: %s", exc)

        time.sleep(60)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("singbox-exporter starting on :%d", EXPORTER_PORT)
    log.info("Clash API: %s", CLASH_API_URL)
    log.info("Connection poll interval: %ds", CONN_POLL_INTERVAL)

    session = make_session()

    threads = [
        threading.Thread(target=traffic_stream_thread, args=(session,),
                         daemon=True, name="traffic-stream"),
        threading.Thread(target=ConnectionTracker(session).run,
                         daemon=True, name="conn-poller"),
        threading.Thread(target=meta_poll_thread, args=(session,),
                         daemon=True, name="meta-poller"),
    ]
    for t in threads:
        t.start()

    start_http_server(EXPORTER_PORT)
    log.info("Metrics at http://0.0.0.0:%d/metrics", EXPORTER_PORT)

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()