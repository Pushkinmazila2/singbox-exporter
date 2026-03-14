# singbox-exporter
singbox-exporter

<img src="sing-box-1773515138940.png" width="800">
def

# singbox-exporter

Prometheus exporter for [sing-box](https://sing-box.sagernet.org/) via Clash-compatible API.

Collects metrics and structured connection logs from sing-box and exposes them for scraping.

---

## Features

- **Metrics** — traffic counters, active connections, memory, proxy status
- **Connection logs** — structured JSON per closed connection (stdout → Loki via Alloy/Promtail)
- **Zero data loss** — global traffic via SSE stream, per-flow bytes accumulated from closed connections
- **Universal** — works with any Prometheus-compatible scraper (Grafana Alloy, Prometheus, VictoriaMetrics)

---

## Requirements

- sing-box with Clash API enabled (`experimental.clash_api`)
- Docker + Docker Compose

### sing-box config

```json
{
  "experimental": {
    "clash_api": {
      "external_controller": "127.0.0.1:9090",
      "secret": "your_secret"
    }
  }
}
```

---

## Quick start

```bash
git clone https://github.com/Pushkinmazila2/singbox-exporter
cd singbox-exporter

# Edit docker-compose.yml — set CLASH_API_SECRET
docker compose up -d

# Verify
curl http://127.0.0.1:9101/metrics
```

---

## Configuration

All configuration is done via environment variables.

| Variable | Default | Description |
|---|---|---|
| `CLASH_API_URL` | `http://127.0.0.1:9090` | sing-box Clash API address |
| `CLASH_API_SECRET` | `` | Clash API secret (Bearer token) |
| `EXPORTER_PORT` | `9101` | Port to expose `/metrics` on |
| `CONN_POLL_INTERVAL` | `5` | Connections poll interval (seconds) |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`) |

---

## Metrics reference

### sing-box info

| Metric | Type | Description |
|---|---|---|
| `singbox_info` | Info | Version and build info (`version`, `meta`, `premium` labels) |
| `singbox_memory_bytes` | Gauge | Memory used by sing-box process |
| `singbox_scrape_success` | Gauge | `1` if last scrape succeeded, `0` otherwise (`endpoint` label) |

### Traffic

| Metric | Type | Labels | Description |
|---|---|---|---|
| `singbox_traffic_bytes_total` | Counter | `direction` | Global bytes (upload/download) from `/traffic` SSE stream |
| `singbox_flow_bytes_total` | Counter | `inbound_protocol`, `inbound_tag`, `outbound`, `rule_outbound`, `direction` | Bytes per flow, accumulated from closed connections |

### Connections

| Metric | Type | Labels | Description |
|---|---|---|---|
| `singbox_connections_active` | Gauge | `inbound_protocol`, `inbound_tag`, `outbound`, `rule_outbound` | Currently active connections |
| `singbox_proxy_up` | Gauge | `proxy`, `type` | Outbounds defined in sing-box config (always `1`) |

---

## Connection logs (Loki)

Each closed connection emits one JSON line to stdout:

```json
{
  "message": "Inbound/vless[vless-13423] (77.37.196.126) -> api.telegram.org:443 [TCP] -> Outbound[ServerNL] | Rule: rule_set => route(ServerNL) | ↑2.5KB ↓45.3KB",
  "ts": "2026-03-14T15:53:28Z",
  "inbound_protocol": "vless",
  "inbound_tag": "vless-13423",
  "source_ip": "77.37.196.126",
  "host": "api.telegram.org",
  "port": "443",
  "network": "tcp",
  "outbound": "ServerNL",
  "rule_raw": "rule_set=[geosite-ru-blocked ...] => route(ServerNL)",
  "rule_outbound": "ServerNL",
  "upload_bytes": 2560,
  "download_bytes": 46387
}
```

Collect with Grafana Alloy or Promtail — mount `/var/run/docker.sock` and scrape container stdout.

### Loki labels promoted by Alloy

| Label | Description |
|---|---|
| `outbound` | Outbound used for this connection |
| `rule_outbound` | Outbound extracted from routing rule |
| `network` | `tcp` or `udp` |
| `container` | Container name (`/singbox-exporter`) |

---

## Useful PromQL queries

```promql
# Global download rate (bytes/sec)
rate(singbox_traffic_bytes_total{direction="download"}[1m])

# Traffic by outbound (bytes/sec)
sum by (outbound) (rate(singbox_flow_bytes_total{direction="download"}[5m]))

# Active connections by outbound
sum by (outbound) (singbox_connections_active)

# Total bytes transferred per outbound (since exporter start)
sum by (outbound) (singbox_flow_bytes_total)
```

---

## Architecture

```
sing-box Clash API
  ├── GET /traffic  (SSE, 1/sec)   ──▶ singbox_traffic_bytes_total (Counter)
  ├── GET /connections (poll, 5s)  ──▶ singbox_flow_bytes_total (Counter)
  │                                    singbox_connections_active (Gauge)
  │                                    singbox_memory_bytes (Gauge)
  │                                    stdout JSON logs (→ Loki)
  ├── GET /proxies  (poll, 60s)    ──▶ singbox_proxy_up (Gauge)
  └── GET /version  (poll, 60s)    ──▶ singbox_info (Info)

:9101/metrics  ◀──  Prometheus / Grafana Alloy
stdout         ──▶  Grafana Alloy / Promtail  ──▶  Loki
```

---

## Grafana dashboard

A ready-made dashboard JSON is available in the repository.

Import via **Dashboards → New → Import** and select your Prometheus and Loki datasources.

Panels included:
- sing-box version, memory, active connections, upload/download rate, scrape health
- Global traffic timeseries (upload/download)
- Traffic by outbound timeseries
- Active connections by outbound and by rule
- Current connections snapshot (bargauge)
- Proxy/outbound status
- Live connection log from Loki

---

## License

MIT
