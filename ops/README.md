# ops: fleet configuration Fleet Pulse depends on

These files mirror what runs on the fleet, so the design can be read and checked from the repo. They are
excerpts, not the full files, and tokens are redacted. Where they live:

| File | Lives at | Purpose |
|---|---|---|
| `prometheus-job-apps.yml` | server-3 `~/monitoring/prometheus/prometheus.yml` | The `apps` probe job (31 apps, module per target) |
| `blackbox-modules.yml` | server-3 `~/monitoring/blackbox/` | `app_ok`, `app_gated`, `app_tailnet` |
| `prometheus-rules-watchdog.yml` | server-3 `~/monitoring/prometheus/rules/watchdog.yml` | Always-firing heartbeat |
| `prometheus-rules-containers.yml` | server-3 `~/monitoring/prometheus/rules/alerts.yml` | Crash loop, memory (working set), endpoint down |
| `alertmanager-routes.yml` | server-3 `~/monitoring/alertmanager/alertmanager.yml` | Heartbeat and Fleet Pulse receivers |
| `promtail-traefik-access.yml` | promtail on all five nodes | Traefik access logs into Loki |

Traefik on oracle also exposes metrics on its tailnet address (entrypoint `metrics`, :8082) with latency buckets
0.025-5 s, which the canary's p95 comparison uses. Prometheus keeps 35 days (cap 12 GB) so the 30-day SLO window is
always full.
