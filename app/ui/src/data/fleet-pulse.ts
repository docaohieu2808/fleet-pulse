// Fleet Pulse: API types, endpoints, and sample data used only by the dev server when /api is unreachable.

export type Severity = "critical" | "warning" | "info";

export interface Incident {
  id: number;
  title: string;
  severity: Severity;
  host: string | null;
  opened_at: string;
  closed_at?: string | null;
  acked_at?: string | null;   // the owner wrote down the cause while it was open
  ack_note?: string | null;
}

export interface Slo {
  app: string;
  slo: string;
  host: string;
  objective: number;
  budget_remaining: number | null;
  burn_rate: number | null;
  error_ratio_period?: number | null;
  data_hours: number | null;
  description?: string;
  unit?: "minutes" | "requests";   // minutes of downtime (probes) or failed requests
  budget_allowed?: number | null;  // what the 30-day period allows, in `unit`
  budget_used?: number | null;     // spent so far, in `unit`
}

export type DeployResult = "promoted" | "rolled_back" | "refused" | "aborted" | "promote_failed";

export interface Deploy {
  id: number;
  at: string;
  app: string;
  result: DeployResult;
  image?: string | null;
  rev?: string | null;
  reason: string | null;
}

export interface Service {
  id: string;
  name: string;
  url: string;
  group: string;
  host: string;
  status: "up" | "down" | "unknown";
  alerting?: boolean;
  last_code: number | null;
  last_latency_ms: number | null;
}

export interface Summary {
  generated_at: string;
  prometheus?: { error: string | null; age_s: number | null };
  open_incidents: Incident[];
  services: Service[] | null;
  slos: Slo[] | null;
  deploys: Deploy[];
  past_incidents: Incident[];
}

export interface IncidentEvent {
  at: string;
  kind: "firing" | "resolved" | "deploy" | "note";
  text: string;
}

export interface IncidentDetail {
  incident: Incident;
  events: IncidentEvent[];
  postmortem: string | null;
}

export const SUMMARY_URL = "/api/summary";
export const incidentUrl = (id: number) => `/api/incidents/${id}`;
export const ackUrl = (id: number) => `/api/incidents/${id}/ack`;

const avail = (app: string, host: string, budget: number, hours: number): Slo => ({
  app, slo: "availability", host, objective: 0.995, budget_remaining: budget, burn_rate: 0,
  error_ratio_period: budget < 0 ? 0.0199 : 0, data_hours: hours,
  description: "99.5% of probes succeed over 30 days.",
  unit: "minutes", budget_allowed: 216, budget_used: (1 - budget) * 216,
});

export const sampleSummary: Summary = {
  generated_at: "2026-10-24T18:12:00+0700",
  open_incidents: [
    { id: 2, title: "server-1: CriticalDiskSpace", severity: "critical", host: "server-1", opened_at: "2026-10-24T17:43:05+0700" },
    { id: 5, title: "job-radar: SLOJobRadarAvailability", severity: "warning", host: "oracle", opened_at: "2026-10-24T18:02:02+0700",
      acked_at: "2026-10-24T23:40:00+0700", ack_note: "Drill and an early canary bug; nothing failing since 18:01." },
  ],
  services: [
    { id: "portfolio", name: "Portfolio", url: "https://docaohieu.com", group: "products", host: "cloudflare", status: "up", last_code: 200, last_latency_ms: 53 },
    { id: "jobradar", name: "Job Radar", url: "https://jobs.docaohieu.com", group: "daily", host: "oracle", status: "down", last_code: 0, last_latency_ms: 0 },
    { id: "grafana", name: "Grafana", url: "https://grafana.docaohieu.com", group: "observability", host: "server-3", status: "up", last_code: 302, last_latency_ms: 120 },
  ],
  slos: [
    avail("covumon", "loadbalancer", 1.0, 173),
    {
      app: "covumon", slo: "latency", host: "loadbalancer", objective: 0.98, budget_remaining: 0.566,
      burn_rate: 0, error_ratio_period: 0.0087, data_hours: 173,
      description: "98% of requests finish within 1.2s over 30 days.",
      unit: "requests", budget_allowed: 1480, budget_used: 642,
    },
    avail("job-radar", "oracle", -2.74, 1.2),
  ],
  deploys: [
    { id: 9, at: "2026-10-24T17:54:23+0700", app: "job-radar", result: "promoted", rev: "7854df4", reason: "canary 191 req, 5xx 0.00%, p95 266ms vs stable 235ms" },
    { id: 6, at: "2026-10-24T17:24:09+0700", app: "job-radar", result: "rolled_back", rev: null, reason: "canary 5xx 30.30% > allowed 1.00% (stable 0.00%)" },
  ],
  past_incidents: [
    { id: 3, title: "job-radar: EndpointDown", severity: "critical", host: "oracle", opened_at: "2026-10-24T17:58:16+0700", closed_at: "2026-10-24T18:00:16+0700" },
  ],
};

export function sampleIncident(id: number): IncidentDetail {
  const inc = [...sampleSummary.open_incidents, ...sampleSummary.past_incidents].find((i) => i.id === id);
  if (!inc) throw new Error("Incident not found");
  const events: IncidentEvent[] = [{ at: inc.opened_at, kind: "firing", text: inc.title }];
  if (inc.closed_at) events.push({ at: inc.closed_at, kind: "resolved", text: "Resolved" });
  return { incident: inc, events, postmortem: null };
}
