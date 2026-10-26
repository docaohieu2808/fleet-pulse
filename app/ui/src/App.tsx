// Fleet Pulse dashboard. Visual design from a Lovable draft; wired to the Fleet Pulse API.
import { useCallback, useEffect, useState, type ReactNode } from "react";
import {
  AlertTriangle, CheckCircle2, ChevronRight, Clock, Database, GitCommitHorizontal,
  Info, Rocket, ShieldCheck, Ban, CircleSlash, X, Flame, Hourglass, Server, StickyNote, WifiOff, ExternalLink, Eye,
} from "lucide-react";
import { FleetPulseLogo } from "@/components/FleetPulseLogo";
import {
  SUMMARY_URL, incidentUrl, ackUrl, sampleSummary, sampleIncident,
  type Summary, type Slo, type Deploy, type Incident, type IncidentDetail, type IncidentEvent, type Service,
} from "@/data/fleet-pulse";

const DEV = import.meta.env.DEV;

/* ---------- time ---------- */
const parse = (s: string) => new Date(s.replace(/([+-]\d\d)(\d\d)$/, "$1:$2"));
const hm = (d: Date) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
function fmt(s: string) {
  const d = parse(s);
  const now = new Date();
  const day = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(now) - day(d)) / 86400000);
  if (diff === 0) return hm(d);
  if (diff === 1) return `Yesterday ${hm(d)}`;
  return d.toLocaleDateString([], { day: "numeric", month: "short" });
}
function ago(s: string, ref = Date.now()) {
  const m = Math.max(0, Math.round((ref - parse(s).getTime()) / 60000));
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  return h < 48 ? `${h} h ${m % 60} min` : `${Math.floor(h / 24)} d`;
}
const pct = (r: number, d = 1) => `${(r * 100).toFixed(d)}%`;
const hours = (h: number) => (h < 10 ? h.toFixed(1) : Math.round(h).toString());

/* ---------- data ---------- */
type Source = "live" | "sample" | "offline";

function useSummary() {
  const [data, setData] = useState<Summary | null>(null);
  const [source, setSource] = useState<Source>("live");
  const load = useCallback(async () => {
    try {
      const r = await fetch(SUMMARY_URL, { headers: { Accept: "application/json" } });
      if (!r.ok || !r.headers.get("content-type")?.includes("json")) throw new Error();
      setData(await r.json());
      setSource("live");
    } catch {
      // The dev server falls back to sample data; production keeps the last real numbers and says it is offline.
      if (DEV) {
        setData(sampleSummary);
        setSource("sample");
      } else {
        setSource("offline");
      }
    }
  }, []);
  useEffect(() => {
    load();
    let id: number | undefined;
    const start = () => { clearInterval(id); id = window.setInterval(load, 60000); };
    const onVis = () => {
      if (document.visibilityState === "visible") { load(); start(); } else clearInterval(id);
    };
    if (document.visibilityState === "visible") start();
    document.addEventListener("visibilitychange", onVis);
    return () => { clearInterval(id); document.removeEventListener("visibilitychange", onVis); };
  }, [load]);
  return { data, source, reload: load };
}

async function fetchIncident(id: number, sample: boolean): Promise<IncidentDetail> {
  if (!sample) {
    const r = await fetch(incidentUrl(id));
    if (r.ok && r.headers.get("content-type")?.includes("json")) return await r.json();
    throw new Error(`incident ${id}: ${r.status}`);
  }
  return sampleIncident(id);
}

/* ---------- page ---------- */
export default function FleetPulse() {
  const { data, source, reload } = useSummary();
  const [openId, setOpenId] = useState<number | null>(null);

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-20 border-b bg-background/85 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-5xl items-center gap-3 px-4 sm:px-6">
          <div className="flex size-7 items-center justify-center rounded-md bg-primary text-primary-foreground shadow-e1">
            <FleetPulseLogo className="size-4" />
          </div>
          <span className="font-semibold tracking-tight">Fleet Pulse</span>
          {source === "sample" && (
            <span className="rounded-full border bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
              sample data
            </span>
          )}
          {source === "offline" && (
            <span className="inline-flex items-center gap-1 rounded-full bg-warn-soft px-2 py-0.5 text-[11px] font-medium text-warn">
              <WifiOff className="size-3" />API unreachable
            </span>
          )}
          <span className="ml-auto flex items-center gap-1.5 text-xs text-muted-foreground tnum">
            <Clock className="size-3.5" />
            {data ? `Updated ${fmt(data.generated_at)}` : ""}
          </span>
        </div>
      </header>

      <main className="mx-auto max-w-5xl space-y-10 px-4 py-6 sm:px-6 sm:py-10">
        {data ? (
          <>
            {data.prometheus?.error && (
              <p className="flex items-center gap-2 rounded-lg bg-warn-soft px-4 py-3 text-sm text-warn">
                <WifiOff className="size-4 shrink-0" />
                Prometheus is not answering ({data.prometheus.error}). App status and SLOs below are the last known
                values{data.prometheus.age_s !== null ? `, ${Math.round(data.prometheus.age_s / 60)} min old` : ""}.
              </p>
            )}
            <NowSection incidents={data.open_incidents} services={data.services} onOpen={setOpenId} />
            <ServicesSection services={data.services} />
            <SloSection slos={data.slos} />
            <DeploySection deploys={data.deploys} />
            <PastSection incidents={data.past_incidents} onOpen={setOpenId} />
            <footer className="pb-6 text-center text-xs text-muted-foreground">
              Refreshes every 60 s while this tab is open.
            </footer>
          </>
        ) : (
          <p className="surface p-6 text-sm text-muted-foreground">
            {source === "offline" ? "Cannot reach the Fleet Pulse API." : "Loading…"}
          </p>
        )}
      </main>

      <IncidentSheet id={openId} sample={source === "sample"} onClose={() => setOpenId(null)} onChanged={reload} />
    </div>
  );
}

function SectionHead({ title, hint, right }: { title: string; hint?: string; right?: ReactNode }) {
  return (
    <div className="mb-3 flex items-end justify-between gap-4">
      <div>
        <h2 className="text-sm font-semibold tracking-tight">{title}</h2>
        {hint && <p className="mt-0.5 text-xs text-muted-foreground">{hint}</p>}
      </div>
      {right}
    </div>
  );
}

/* ---------- 1. now ---------- */
function NowSection({ incidents, services, onOpen }: { incidents: Incident[]; services: Service[] | null; onOpen: (id: number) => void }) {
  const notAnswering = (services ?? []).filter((s) => s.status !== "up");
  if (incidents.length === 0 && notAnswering.length > 0) {
    // No incident does not mean nothing is wrong: warnings and unpaged apps never open one.
    return (
      <section className="surface-raised flex items-center gap-4 p-6 sm:p-8">
        <div className="flex size-12 shrink-0 items-center justify-center rounded-full bg-warn-soft text-warn">
          <AlertTriangle className="size-6" />
        </div>
        <div>
          <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">
            {notAnswering.length} app{notAnswering.length > 1 ? "s" : ""} not answering
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            No open incident: {notAnswering.map((s) => s.name).join(", ")}. See All services below.
          </p>
        </div>
      </section>
    );
  }
  if (incidents.length === 0 && services === null) {
    return (
      <section className="surface-raised flex items-center gap-4 p-6 sm:p-8">
        <div className="flex size-12 shrink-0 items-center justify-center rounded-full bg-muted text-muted-foreground">
          <Hourglass className="size-6" />
        </div>
        <div>
          <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">No open incidents</h1>
          <p className="mt-1 text-sm text-muted-foreground">App status is unknown right now, so this is not an all-clear.</p>
        </div>
      </section>
    );
  }
  if (incidents.length === 0) {
    return (
      <section className="surface-raised flex items-center gap-4 p-6 sm:p-8">
        <div className="flex size-12 shrink-0 items-center justify-center rounded-full bg-ok-soft text-ok">
          <CheckCircle2 className="size-6" />
        </div>
        <div>
          <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">All clear</h1>
          <p className="mt-1 text-sm text-muted-foreground">Nothing is broken right now. No open incidents.</p>
        </div>
      </section>
    );
  }
  const unexplained = incidents.filter((i) => !i.acked_at).length;
  return (
    <section className="surface-raised overflow-hidden">
      <div className="flex items-center gap-3 border-b px-5 py-4 sm:px-6">
        {unexplained > 0 ? (
          <span className="relative flex size-2.5">
            <span className="absolute inline-flex size-full animate-ping rounded-full bg-bad opacity-60" />
            <span className="relative inline-flex size-2.5 rounded-full bg-bad" />
          </span>
        ) : (
          <span className="inline-flex size-2.5 rounded-full bg-muted-foreground/50" />
        )}
        <div>
          <h1 className="text-lg font-semibold tracking-tight sm:text-xl">
            {incidents.length} open incident{incidents.length > 1 ? "s" : ""}
          </h1>
          {unexplained === 0 && (
            <p className="text-xs text-muted-foreground">Cause known for all of them; they close on their own when the alert clears.</p>
          )}
        </div>
      </div>
      <ul className="divide-y">
        {incidents.map((i) => {
          const [svc, alert] = i.title.includes(": ") ? i.title.split(": ") : ["", i.title];
          return (
            <li key={i.id}>
              <button onClick={() => onOpen(i.id)} className="flex w-full items-center gap-4 px-5 py-4 text-left transition-colors hover:bg-muted/60 sm:px-6">
                {i.acked_at
                  ? <Eye className="size-5 shrink-0 text-muted-foreground" />
                  : <AlertTriangle className={`size-5 shrink-0 ${i.severity === "critical" ? "text-bad" : "text-warn"}`} />}
                <div className="min-w-0 flex-1">
                  <div className={`truncate font-medium ${i.acked_at ? "text-muted-foreground" : ""}`}>{alert}</div>
                  {i.ack_note && <div className="mt-0.5 truncate text-xs text-muted-foreground">Cause known: {i.ack_note}</div>}
                  <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                    {svc && <span>{svc}</span>}
                    {i.host && <span className="flex items-center gap-1"><Server className="size-3" />{i.host}</span>}
                    <span className="capitalize">{i.severity}</span>
                  </div>
                </div>
                <div className="text-right text-xs tnum">
                  <div className="font-medium">{fmt(i.opened_at)}</div>
                  <div className="text-muted-foreground">{`open ${ago(i.opened_at)}`}</div>
                </div>
                <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/* ---------- 1b. every app, from the blackbox apps job ---------- */
const GROUP_NAMES: Record<string, string> = {
  products: "Products", daily: "Daily tools", devops: "DevOps & CI/CD",
  observability: "Observability", storage: "Storage & secrets", engines: "Chess engines",
};
const dot: Record<Service["status"], string> = { up: "bg-ok", down: "bg-bad", unknown: "bg-muted-foreground/40" };

function ServicesSection({ services }: { services: Service[] | null }) {
  if (services === null) {
    return (
      <section>
        <SectionHead title="All services" />
        <p className="surface p-4 text-sm text-muted-foreground">
          Prometheus cannot be reached, so per-app status is unavailable.
        </p>
      </section>
    );
  }
  const problems = services.filter((s) => s.status !== "up");
  const up = services.length - problems.length;
  const groups = Object.keys(GROUP_NAMES).filter((g) => services.some((s) => s.group === g));
  return (
    <section>
      <SectionHead
        title="All services"
        hint={`${up} of ${services.length} up${problems.length ? `, ${problems.length} need a look` : ""} · probed every minute from server-3`}
      />
      {problems.length > 0 && (
        <ul className="surface mb-3 divide-y overflow-hidden">
          {problems.map((s) => (
            <li key={s.id} className="flex items-center gap-3 px-4 py-3">
              <span className={`size-2 shrink-0 rounded-full ${dot[s.status]}`} />
              <a href={s.url} target="_blank" rel="noreferrer" className="min-w-0 flex-1 truncate text-sm font-medium hover:underline">{s.name}</a>
              <span className="text-xs text-muted-foreground tnum">
                {s.status === "down" ? (s.last_code ? `HTTP ${s.last_code}` : "unreachable") : "no recent data"} · {s.host}
              </span>
            </li>
          ))}
        </ul>
      )}
      <div className="surface p-4">
        <div className="grid gap-x-6 gap-y-4 sm:grid-cols-2 lg:grid-cols-3">
          {groups.map((g) => (
            <div key={g}>
              <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">{GROUP_NAMES[g]}</div>
              <ul className="space-y-1">
                {services.filter((s) => s.group === g).map((s) => (
                  <li key={s.id}>
                    <a href={s.url} target="_blank" rel="noreferrer" className="group flex items-center gap-2 rounded-md py-0.5 text-sm hover:text-primary">
                      <span className={`size-1.5 shrink-0 rounded-full ${dot[s.status]}`} />
                      <span className="min-w-0 flex-1 truncate">{s.name}</span>
                      {s.status === "up" && s.last_latency_ms !== null && (
                        <span className="text-xs text-muted-foreground tnum">{s.last_latency_ms} ms</span>
                      )}
                      <ExternalLink className="size-3 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

/* ---------- 2. SLOs ---------- */
type Status = "healthy" | "burning" | "exhausted" | "nodata";
// Under a day of data the budget is mostly noise: four minutes down in the first hour reads as -300%.
const MIN_HOURS_TO_JUDGE = 24;
// Share of the 30-day allowance still unspent. Probe minutes are real downtime, so they count from the first hour;
// request counts are projected from the traffic so far and need a day before they mean much.
function leftShare(s: Slo): number | null {
  if (s.budget_allowed == null || s.budget_used == null || s.budget_allowed <= 0) return null;
  return 1 - s.budget_used / s.budget_allowed;
}
function sloStatus(s: Slo): Status {
  const left = leftShare(s);
  if (left === null || s.burn_rate === null) return "nodata";
  if (s.unit !== "minutes" && (s.data_hours === null || s.data_hours < MIN_HOURS_TO_JUDGE)) return "nodata";
  if (left <= 0) return "exhausted";
  if (s.burn_rate > 1 || left < 0.25) return "burning";
  return "healthy";
}
const count = (n: number) => Math.round(n).toLocaleString("en-US");
// The burn rate in words: what happens if the last hour's pace keeps going.
function pace(s: Slo): string {
  const left = leftShare(s);
  if (s.burn_rate === null || left === null) return "—";
  if (s.burn_rate === 0) return "No failures in the last hour";
  if (s.burn_rate < 1) return "Failing a little, within the allowance";
  if (left <= 0) return "Allowance already used up";
  const days = (left * 30) / s.burn_rate;
  return `At this pace, used up in ~${days < 2 ? `${Math.max(1, Math.round(days * 24))} h` : `${Math.round(days)} days`}`;
}
const statusMeta: Record<Status, { label: string; cls: string; bar: string; Icon: typeof CheckCircle2 }> = {
  healthy: { label: "Healthy", cls: "bg-ok-soft text-ok", bar: "bg-ok", Icon: CheckCircle2 },
  burning: { label: "Burning", cls: "bg-warn-soft text-warn", bar: "bg-warn", Icon: Flame },
  exhausted: { label: "Exhausted", cls: "bg-bad-soft text-bad", bar: "bg-bad", Icon: AlertTriangle },
  nodata: { label: "Not enough data", cls: "bg-muted text-muted-foreground", bar: "bg-muted-foreground/30", Icon: Hourglass },
};

function SloSection({ slos }: { slos: Slo[] | null }) {
  if (slos === null) {
    return (
      <section>
        <SectionHead title="Service promises" />
        <p className="surface p-4 text-sm text-muted-foreground">SLO data unavailable: Prometheus has not answered yet.</p>
      </section>
    );
  }
  const counts = slos.reduce<Record<Status, number>>((a, s) => ({ ...a, [sloStatus(s)]: a[sloStatus(s)] + 1 }), { healthy: 0, burning: 0, exhausted: 0, nodata: 0 });
  const parts = [
    `${counts.healthy} healthy`,
    counts.burning ? `${counts.burning} burning` : "",
    counts.exhausted ? `${counts.exhausted} exhausted` : "",
    counts.nodata ? `${counts.nodata} still collecting data` : "",
  ].filter(Boolean);
  return (
    <section>
      <SectionHead title="Service promises" hint={parts.join(", ")} />
      <div className="mb-4 flex gap-2.5 rounded-lg border bg-muted/50 px-4 py-3 text-xs leading-relaxed text-muted-foreground">
        <Info className="mt-0.5 size-3.5 shrink-0" />
        <p>
          Each promise allows a little failure over 30 days (the <span className="font-medium text-foreground">error
          budget</span>): 99.5% leaves 216 minutes of downtime, 98% leaves 2 in 100 requests. The number is what is
          still allowed. The line under it (<span className="font-medium text-foreground">burn rate</span>) says how fast
          the last hour spent it; spending it in about 2 days pages you on Telegram.
        </p>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        {slos.map((s) => <SloCard key={`${s.app}-${s.slo}`} s={s} />)}
      </div>
    </section>
  );
}

function SloCard({ s }: { s: Slo }) {
  const st = sloStatus(s);
  const m = statusMeta[st];
  const provisional = s.data_hours === null || s.data_hours < 720;
  const left = leftShare(s);
  const fill = left === null ? 0 : Math.max(0, Math.min(1, left));
  const minutes = s.unit === "minutes";
  const remaining = left === null || s.budget_allowed == null ? null : s.budget_allowed * left;
  const sample = s.description?.includes("A sample SLO");
  return (
    <div className="surface p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 truncate font-medium">
            {s.app}
            {sample && <span className="rounded-full border px-1.5 py-px text-[10px] font-medium text-muted-foreground">sample</span>}
          </div>
          <div className="mt-0.5 text-xs text-muted-foreground">
            {s.slo} · {s.host} · target <span className="tnum">{pct(s.objective, s.objective >= 0.99 ? 1 : 0)}</span>
          </div>
        </div>
        <span className={`inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ${m.cls}`}>
          <m.Icon className="size-3" />{m.label}
        </span>
      </div>

      <div className="mt-4 tnum">
        {remaining === null || s.budget_allowed == null ? (
          <span className="text-2xl font-semibold tracking-tight">—</span>
        ) : (
          <>
            <span className="text-2xl font-semibold tracking-tight">{count(Math.max(0, remaining))}</span>
            <span className="ml-1 text-sm text-muted-foreground">/ {count(s.budget_allowed)} {minutes ? "min" : "requests"}</span>
            <div className="mt-0.5 text-xs text-muted-foreground">
              {remaining < 0
                ? `over the allowance by ${count(-remaining)} ${minutes ? "min" : "requests"}`
                : minutes ? "of downtime still allowed this 30 days" : "may still fail this 30 days (at the current traffic)"}
            </div>
          </>
        )}
      </div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-track">
        <div className={`h-full rounded-full ${m.bar} transition-all`} style={{ width: `${fill * 100}%` }} />
      </div>
      <p className="mt-2 text-xs tnum">
        <span className="text-foreground">{pace(s)}</span>
        {s.burn_rate !== null && s.burn_rate > 0 && <span className="text-muted-foreground"> · burn {s.burn_rate.toFixed(1)}×</span>}
      </p>

      {s.description && <p className="mt-3 text-xs text-muted-foreground">{s.description}</p>}
      {provisional && (
        <p className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground">
          <Database className="size-3 shrink-0" />
          <span className="tnum">
            {s.data_hours === null
              ? "Data coverage unknown."
              : `Measured for ${hours(s.data_hours)} h of the 720 h period so far${minutes ? "" : "; the allowance is projected from that traffic"}.`}
          </span>
        </p>
      )}
    </div>
  );
}

/* ---------- 3. deploys ---------- */
const deployMeta: Record<Deploy["result"], { label: string; note: string; cls: string; Icon: typeof Rocket }> = {
  promoted: { label: "Promoted", note: "New version is live", cls: "bg-primary/10 text-primary", Icon: Rocket },
  rolled_back: { label: "Caught by canary", note: "Rolled back automatically, production unchanged", cls: "bg-ok-soft text-ok", Icon: ShieldCheck },
  refused: { label: "Refused", note: "Not attempted", cls: "bg-muted text-muted-foreground", Icon: Ban },
  aborted: { label: "Not judged", note: "Canary removed before a verdict, production unchanged", cls: "bg-warn-soft text-warn", Icon: CircleSlash },
  promote_failed: { label: "Promote failed", note: "Canary passed but the switch failed; see the reason", cls: "bg-bad-soft text-bad", Icon: AlertTriangle },
};

function DeploySection({ deploys }: { deploys: Deploy[] }) {
  const caught = deploys.filter((d) => d.result === "rolled_back").length;
  return (
    <section>
      <SectionHead
        title="Recent deploys"
        hint={caught ? `The safety check stopped ${caught} release${caught > 1 ? "s" : ""} before they reached users.` : "Every release goes through a canary check first."}
      />
      {deploys.length === 0 ? (
        <p className="surface p-4 text-sm text-muted-foreground">No deploys yet.</p>
      ) : (
        <ol className="surface divide-y overflow-hidden">
          {deploys.map((d) => {
            const m = deployMeta[d.result] ?? deployMeta.aborted;
            return (
              <li key={d.id} className="flex gap-3 px-4 py-3.5 sm:gap-4">
                <div className={`mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-md ${m.cls}`}>
                  <m.Icon className="size-4" />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                    <span className="font-medium">{d.app}</span>
                    <span className={`text-xs font-medium ${m.cls.split(" ").find((c) => c.startsWith("text-"))}`}>{m.label}</span>
                  </div>
                  {d.reason && <p className="mt-0.5 break-words text-sm text-muted-foreground">{d.reason}</p>}
                  <p className="mt-1 flex flex-wrap items-center gap-x-3 text-xs text-muted-foreground">
                    <span>{m.note}</span>
                    {d.rev && <span className="flex items-center gap-1 font-mono"><GitCommitHorizontal className="size-3" />{d.rev}</span>}
                  </p>
                </div>
                <div className="shrink-0 text-xs text-muted-foreground tnum">{fmt(d.at)}</div>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

/* ---------- 4. past ---------- */
function PastSection({ incidents, onOpen }: { incidents: Incident[]; onOpen: (id: number) => void }) {
  return (
    <section>
      <SectionHead title="Past incidents" />
      {incidents.length === 0 ? (
        <p className="surface p-4 text-sm text-muted-foreground">No past incidents.</p>
      ) : (
        <ul className="surface divide-y overflow-hidden">
          {incidents.map((i) => (
            <li key={i.id}>
              <button onClick={() => onOpen(i.id)} className="flex w-full items-center gap-3 px-4 py-3.5 text-left transition-colors hover:bg-muted/60">
                <CheckCircle2 className="size-4 shrink-0 text-muted-foreground" />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-medium">{i.title}</div>
                  <div className="mt-0.5 text-xs text-muted-foreground tnum">
                    #{i.id}{i.host ? ` · ${i.host}` : ""}{i.closed_at ? ` · lasted ${ago(i.opened_at, parse(i.closed_at).getTime())}` : ""}
                  </div>
                </div>
                <span className="text-xs text-muted-foreground tnum">{fmt(i.opened_at)}</span>
                <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/* ---------- sheet ---------- */
const eventIcon: Record<IncidentEvent["kind"], { Icon: typeof Rocket; cls: string }> = {
  firing: { Icon: AlertTriangle, cls: "text-bad bg-bad-soft" },
  resolved: { Icon: CheckCircle2, cls: "text-ok bg-ok-soft" },
  deploy: { Icon: Rocket, cls: "text-primary bg-primary/10" },
  note: { Icon: StickyNote, cls: "text-muted-foreground bg-muted" },
};

function IncidentSheet({ id, sample, onClose, onChanged }: {
  id: number | null; sample: boolean; onClose: () => void; onChanged: () => void;
}) {
  const [detail, setDetail] = useState<IncidentDetail | null>(null);
  const [err, setErr] = useState(false);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    if (id === null) return;
    let current = true; // a slow answer for an earlier incident must not overwrite this one
    setErr(false);
    fetchIncident(id, sample).then((d) => current && setDetail(d)).catch(() => current && setErr(true));
    return () => { current = false; };
  }, [id, sample, version]);
  useEffect(() => setDetail(null), [id]);
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  if (id === null) return null;
  const inc = detail?.incident;
  return (
    <div className="fixed inset-0 z-50" role="dialog" aria-modal="true">
      <div className="absolute inset-0 bg-foreground/20 animate-in fade-in" onClick={onClose} />
      <aside className="absolute inset-x-0 bottom-0 flex max-h-[88vh] flex-col rounded-t-xl border bg-raised shadow-e3 animate-in slide-in-from-bottom sm:inset-y-0 sm:left-auto sm:right-0 sm:max-h-none sm:w-[440px] sm:rounded-none sm:rounded-l-xl sm:slide-in-from-right">
        <div className="flex items-start gap-3 border-b px-5 py-4">
          <div className="min-w-0 flex-1">
            <div className="text-xs text-muted-foreground tnum">Incident #{id}</div>
            <h3 className="mt-0.5 font-semibold tracking-tight">{inc?.title ?? (err ? "Unavailable" : "Loading…")}</h3>
            {inc && (
              <p className="mt-1 text-xs text-muted-foreground tnum">
                {inc.host ?? "n/a"} · opened {fmt(inc.opened_at)} · {inc.closed_at ? `closed ${fmt(inc.closed_at)}` : "still open"}
              </p>
            )}
          </div>
          <button onClick={onClose} aria-label="Close" className="rounded-md p-1.5 text-muted-foreground hover:bg-muted">
            <X className="size-4" />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto px-5 py-5">
          {err && <p className="text-sm text-muted-foreground">Could not load this incident.</p>}
          {detail && (
            <>
              {!detail.incident.closed_at && !sample && (
                <AckForm id={detail.incident.id} note={detail.incident.ack_note ?? ""}
                  onDone={() => { setVersion((v) => v + 1); onChanged(); }} />
              )}
              <h4 className="mb-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">Timeline</h4>
              <ol className="relative space-y-4 before:absolute before:left-[13px] before:top-2 before:bottom-2 before:w-px before:bg-border">
                {detail.events.map((e, i) => {
                  const m = eventIcon[e.kind] ?? eventIcon.note;
                  return (
                    <li key={i} className="relative flex gap-3">
                      <div className={`z-10 flex size-7 shrink-0 items-center justify-center rounded-full ring-4 ring-raised ${m.cls}`}>
                        <m.Icon className="size-3.5" />
                      </div>
                      <div className="min-w-0 pt-1">
                        <div className="text-xs text-muted-foreground tnum">{fmt(e.at)}</div>
                        <div className="break-words text-sm">{e.text}</div>
                      </div>
                    </li>
                  );
                })}
              </ol>
              <h4 className="mb-1 mt-8 text-xs font-semibold uppercase tracking-wider text-muted-foreground">Postmortem</h4>
              {detail.postmortem ? (
                <div className="prose-pm text-sm"><Markdown src={detail.postmortem} /></div>
              ) : (
                <p className="mt-2 text-sm text-muted-foreground">Written automatically when the incident closes.</p>
              )}
            </>
          )}
        </div>
      </aside>
    </div>
  );
}

/* ---------- known cause ---------- */
// Writes need the API token. It is asked for once and kept in this browser only.
const TOKEN_KEY = "fleet-pulse-token";
const readToken = () => { try { return localStorage.getItem(TOKEN_KEY) ?? ""; } catch { return ""; } };
const saveToken = (t: string) => { try { if (t) localStorage.setItem(TOKEN_KEY, t); else localStorage.removeItem(TOKEN_KEY); } catch { /* private window */ } };

function AckForm({ id, note, onDone }: { id: number; note: string; onDone: () => void }) {
  const [text, setText] = useState(note);
  const [token, setToken] = useState(readToken);
  const [askToken, setAskToken] = useState(() => readToken() === "");
  const [state, setState] = useState<"idle" | "saving" | "error">("idle");
  const [msg, setMsg] = useState("");
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setState("saving");
    try {
      const r = await fetch(ackUrl(id), {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
        body: JSON.stringify({ note: text }),
      });
      if (r.status === 401) { saveToken(""); setAskToken(true); throw new Error("The token was not accepted."); }
      if (!r.ok) throw new Error((await r.text()) || `Error ${r.status}`);
      saveToken(token);
      setAskToken(false);
      setState("idle");
      onDone();
    } catch (x) {
      setState("error");
      setMsg(x instanceof Error ? x.message : "Could not save.");
    }
  }
  return (
    <form onSubmit={submit} className="mb-8 rounded-lg border bg-muted/40 p-3">
      <label htmlFor="ack-note" className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        {note ? "Cause known" : "Know the cause?"}
      </label>
      <p className="mt-1 text-xs text-muted-foreground">
        Write it down: the incident turns grey on the page and the note goes into the postmortem. A new alert makes it red again.
      </p>
      <textarea id="ack-note" value={text} onChange={(e) => setText(e.target.value)} maxLength={500} rows={2} required
        placeholder="e.g. planned drill, nothing failing since 18:01"
        className="mt-2 w-full resize-y rounded-md border bg-raised px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary/40" />
      {askToken && (
        <input type="password" value={token} onChange={(e) => setToken(e.target.value)} required autoComplete="off"
          placeholder="API token (FLEET_PULSE_TOKEN), asked once"
          className="mt-2 w-full rounded-md border bg-raised px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary/40" />
      )}
      <div className="mt-2 flex items-center gap-3">
        <button type="submit" disabled={state === "saving" || !text.trim()}
          className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50">
          {state === "saving" ? "Saving…" : note ? "Update note" : "Mark cause as known"}
        </button>
        {state === "error" && <span className="text-xs text-bad">{msg}</span>}
      </div>
    </form>
  );
}

/* ---------- tiny markdown: headings, lists, paragraphs, 4-space code lines ---------- */
function inline(t: string): ReactNode[] {
  return t.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).filter(Boolean).map((p, i) =>
    p.startsWith("**") ? <strong key={i}>{p.slice(2, -2)}</strong>
      : p.startsWith("`") ? <code key={i}>{p.slice(1, -1)}</code> : p,
  );
}
function Markdown({ src }: { src: string }) {
  const out: ReactNode[] = [];
  let list: string[] = [];
  let code: string[] = [];
  const flush = () => {
    if (list.length) out.push(<ul key={out.length}>{list.map((l, i) => <li key={i}>{inline(l)}</li>)}</ul>);
    if (code.length) out.push(<pre key={out.length}>{code.join("\n")}</pre>);
    list = [];
    code = [];
  };
  for (const line of src.split("\n")) {
    if (line.startsWith("    ")) { if (list.length) flush(); code.push(line.slice(4)); continue; }
    const l = line.trim();
    if (/^[-*] /.test(l)) { if (code.length) flush(); list.push(l.slice(2)); continue; }
    flush();
    if (!l) continue;
    const h = l.match(/^#{1,6} (.*)/);
    out.push(h ? <h2 key={out.length}>{inline(h[1] ?? "")}</h2> : <p key={out.length}>{inline(l)}</p>);
  }
  flush();
  return <>{out}</>;
}
