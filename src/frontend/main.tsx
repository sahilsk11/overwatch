import React from "react";
import ReactDOM from "react-dom/client";
import {
  Activity,
  AlertTriangle,
  Check,
  ChevronRight,
  Circle,
  Clock3,
  GitPullRequest,
  Loader2,
  RefreshCw,
  Search,
  TerminalSquare,
  X,
} from "lucide-react";
import "./styles.css";

type Bucket = "active" | "done" | "all";
type HealthStatus = "checking" | "ok" | "error";

type PullRequestState =
  | "open"
  | "active"
  | "queued"
  | "running"
  | "success"
  | "failure"
  | "error"
  | "merged"
  | "closed"
  | "done"
  | string;

interface PullRequestSummary {
  id: string | number;
  url: string;
  owner: string;
  repo: string;
  number: number;
  status: PullRequestState;
  provider: string;
  model?: string | null;
  harness?: string | null;
  autofix?: boolean;
  merge_on_bot_approval?: boolean;
  max_turns?: number;
  turns_used?: number;
  latest_ci_state?: string | null;
  latest_head_sha?: string | null;
  latest_summary?: string | null;
  latest_checked_at?: string | null;
  worker_status?: string | null;
  active_attempt_id?: number | null;
  active_attempt_started_at?: string | null;
  active_attempt_elapsed_seconds?: number | null;
  active_attempt_status?: string | null;
  last_attempt_status?: string | null;
  last_attempt_completed_at?: string | null;
  last_provider_command?: string | null;
  last_provider_output?: string | null;
  last_error?: string | null;
}

interface PullRequestDetail extends PullRequestSummary {
  created_at?: string | null;
  updated_at?: string | null;
}

interface CiHistoryEvent {
  id: string | number;
  head_sha: string;
  state?: string;
  summary: string;
  details?: unknown;
  created_at?: string | null;
}

interface ResolutionAttempt {
  id?: string | number;
  watch_turn_id?: string | number | null;
  turn_number?: number | null;
  provider?: string | null;
  model?: string | null;
  harness?: string | null;
  head_sha?: string | null;
  status?: string | null;
  provider_command?: string | null;
  provider_output?: string | null;
  created_at?: string | null;
  completed_at?: string | null;
  error?: string | null;
}

interface PrEvent {
  id?: string | number;
  type?: string;
  level?: "info" | "warning" | "error" | string;
  message?: string;
  created_at?: string | null;
  payload?: unknown;
}

interface PrEventsPayload {
  ci_history?: CiHistoryEvent[];
  resolution_attempts?: ResolutionAttempt[];
}

interface Loadable<T> {
  data: T;
  loading: boolean;
  error: string | null;
}

const bucketLabels: Record<Bucket, string> = {
  active: "Tracked PRs",
  done: "Done / Merged / Closed",
  all: "All",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function asArray<T>(value: unknown, fallback: T[] = []): T[] {
  return Array.isArray(value) ? (value as T[]) : fallback;
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...init?.headers,
    },
    ...init,
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `${response.status} ${response.statusText}`);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

async function fetchPrs(bucket: Bucket): Promise<PullRequestSummary[]> {
  const payload = await requestJson<unknown>(`/api/prs?bucket=${bucket}`);
  if (Array.isArray(payload)) return payload as PullRequestSummary[];
  if (isRecord(payload)) {
    return asArray<PullRequestSummary>(payload.prs ?? payload.items ?? payload.results);
  }
  return [];
}

async function fetchPrDetail(id: string | number): Promise<PullRequestDetail> {
  return requestJson<PullRequestDetail>(`/api/prs/${id}`);
}

async function fetchPrEvents(id: string | number): Promise<PrEvent[]> {
  const payload = await requestJson<unknown>(`/api/prs/${id}/events`);
  if (Array.isArray(payload)) return payload as PrEvent[];
  if (isRecord(payload)) {
    const events = asArray<PrEvent>(payload.events ?? payload.items);
    if (events.length) return events;
    const typed = payload as PrEventsPayload;
    return [
      ...asArray<CiHistoryEvent>(typed.ci_history).map((event) => ({
        id: `ci-${event.id}`,
        type: "ci",
        level: event.state,
        message: event.summary,
        created_at: event.created_at,
        payload: event,
      })),
      ...asArray<ResolutionAttempt>(typed.resolution_attempts).map((attempt) => ({
        id: `attempt-${attempt.id}`,
        type: "agent",
        level: attempt.status ?? undefined,
        message: [
          attempt.provider || "provider",
          attempt.turn_number ? `turn ${attempt.turn_number}` : null,
          attempt.model,
          attempt.head_sha ? `@ ${attempt.head_sha.slice(0, 8)}` : null,
          attempt.status,
          attempt.error,
        ]
          .filter(Boolean)
          .join(" "),
        created_at: attempt.completed_at ?? attempt.created_at,
        payload: attempt,
      })),
    ];
  }
  return [];
}

function formatDate(value?: string | null): string {
  if (!value) return "never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function prLabel(pr: PullRequestSummary): string {
  if (pr.owner && pr.repo && pr.number) return `${pr.owner}/${pr.repo} #${pr.number}`;
  if (pr.repo && pr.number) return `${pr.repo} #${pr.number}`;
  return pr.url.replace(/^https?:\/\/github\.com\//, "");
}

function statusTone(state?: string | null): "ok" | "warn" | "bad" | "neutral" {
  const normalized = (state ?? "").toLowerCase();
  if (["success", "merged", "done", "green", "ok", "passed"].includes(normalized)) return "ok";
  if (["failure", "failed", "error", "closed", "red", "stopped"].includes(normalized)) return "bad";
  if (["pending", "running", "queued", "active", "open", "paused", "needs-human"].includes(normalized)) {
    return "warn";
  }
  return "neutral";
}

function StatusPill({ value }: { value?: string | null }) {
  const label = value || "unknown";
  return <span className={`status status-${statusTone(label)}`}>{label}</span>;
}

function EmptyState({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="empty">
      <Circle aria-hidden="true" />
      <h3>{title}</h3>
      <p>{detail}</p>
    </div>
  );
}

function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="notice notice-error">
      <AlertTriangle aria-hidden="true" />
      <span>{message}</span>
      <button type="button" className="ghost small" onClick={onRetry}>
        Retry
      </button>
    </div>
  );
}

function LoadingRows() {
  return (
    <div className="loading-list" aria-label="Loading pull requests">
      {Array.from({ length: 5 }, (_, index) => (
        <div className="skeleton-row" key={index} />
      ))}
    </div>
  );
}

function App() {
  const [bucket, setBucket] = React.useState<Bucket>("active");
  const [prs, setPrs] = React.useState<Loadable<PullRequestSummary[]>>({
    data: [],
    loading: true,
    error: null,
  });
  const [selectedId, setSelectedId] = React.useState<string | number | null>(null);
  const [detail, setDetail] = React.useState<Loadable<PullRequestDetail | null>>({
    data: null,
    loading: false,
    error: null,
  });
  const [events, setEvents] = React.useState<Loadable<PrEvent[]>>({
    data: [],
    loading: false,
    error: null,
  });
  const [health, setHealth] = React.useState<HealthStatus>("checking");
  const [query, setQuery] = React.useState("");

  const loadPrs = React.useCallback(async () => {
    setPrs((current) => ({ ...current, loading: true, error: null }));
    try {
      const next = await fetchPrs(bucket);
      setPrs({ data: next, loading: false, error: null });
      setSelectedId((current) => current ?? next[0]?.id ?? null);
    } catch (error) {
      setPrs({ data: [], loading: false, error: errorMessage(error) });
    }
  }, [bucket]);

  const loadHealth = React.useCallback(async () => {
    setHealth("checking");
    try {
      await requestJson<unknown>("/api/health");
      setHealth("ok");
    } catch {
      setHealth("error");
    }
  }, []);

  const loadDetail = React.useCallback(async (id: string | number) => {
    setDetail((current) => ({ ...current, loading: true, error: null }));
    setEvents((current) => ({ ...current, loading: true, error: null }));
    try {
      const [nextDetail, nextEvents] = await Promise.all([fetchPrDetail(id), fetchPrEvents(id)]);
      setDetail({ data: nextDetail, loading: false, error: null });
      setEvents({ data: nextEvents, loading: false, error: null });
    } catch (error) {
      setDetail({ data: null, loading: false, error: errorMessage(error) });
      setEvents({ data: [], loading: false, error: errorMessage(error) });
    }
  }, []);

  React.useEffect(() => {
    void loadHealth();
  }, [loadHealth]);

  React.useEffect(() => {
    setSelectedId(null);
    void loadPrs();
  }, [bucket, loadPrs]);

  React.useEffect(() => {
    if (selectedId !== null) {
      void loadDetail(selectedId);
    } else {
      setDetail({ data: null, loading: false, error: null });
      setEvents({ data: [], loading: false, error: null });
    }
  }, [loadDetail, selectedId]);

  const filteredPrs = React.useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return prs.data;
    return prs.data.filter((pr) =>
      [prLabel(pr), pr.url, pr.status, pr.latest_ci_state, pr.latest_summary]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(needle)),
    );
  }, [prs.data, query]);

  return (
    <main className="shell">
      <header className="topbar">
        <div>
          <div className="eyebrow">Overwatch Control</div>
          <h1>Pull Request Operations</h1>
        </div>
        <div className={`health health-${health}`}>
          {health === "checking" ? <Loader2 className="spin" /> : health === "ok" ? <Check /> : <X />}
          <span>API {health}</span>
        </div>
      </header>

      <section className="tabs" aria-label="Pull request buckets">
        {(Object.keys(bucketLabels) as Bucket[]).map((nextBucket) => (
          <button
            type="button"
            key={nextBucket}
            className={bucket === nextBucket ? "tab active" : "tab"}
            onClick={() => setBucket(nextBucket)}
          >
            {bucketLabels[nextBucket]}
          </button>
        ))}
      </section>

      <section className="layout">
        <aside className="left-panel">
          <div className="list-toolbar">
            <div className="search">
              <Search aria-hidden="true" />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Filter PRs"
              />
            </div>
            <button type="button" className="icon-button" onClick={loadPrs} aria-label="Refresh list">
              <RefreshCw aria-hidden="true" />
            </button>
          </div>

          {prs.error ? <ErrorState message={prs.error} onRetry={loadPrs} /> : null}
          {prs.loading ? <LoadingRows /> : null}
          {!prs.loading && !prs.error && filteredPrs.length === 0 ? (
            <EmptyState title="No pull requests" detail="Tracked PRs will appear here after one is added." />
          ) : null}
          <div className="pr-list">
            {filteredPrs.map((pr) => (
              <button
                type="button"
                key={pr.id}
                className={selectedId === pr.id ? "pr-row selected" : "pr-row"}
                onClick={() => setSelectedId(pr.id)}
              >
                <span className="pr-main">
                  <span className="pr-title">
                    <GitPullRequest aria-hidden="true" />
                    {prLabel(pr)}
                  </span>
                  <span className="pr-subtitle">{pr.latest_summary || pr.url}</span>
                </span>
                <span className="pr-meta">
                  <StatusPill value={pr.latest_ci_state ?? pr.status} />
                  <ChevronRight aria-hidden="true" />
                </span>
              </button>
            ))}
          </div>
        </aside>

        <DetailPanel
          detail={detail}
          events={events}
          selectedId={selectedId}
          onRetry={() => selectedId !== null && loadDetail(selectedId)}
        />
      </section>
    </main>
  );
}

function DetailPanel({
  detail,
  events,
  selectedId,
  onRetry,
}: {
  detail: Loadable<PullRequestDetail | null>;
  events: Loadable<PrEvent[]>;
  selectedId: string | number | null;
  onRetry: () => void;
}) {
  if (selectedId === null) {
    return (
      <section className="detail-panel">
        <EmptyState title="Select a PR" detail="Status, event, and agent logs will load in this pane." />
      </section>
    );
  }

  if (detail.loading) {
    return (
      <section className="detail-panel">
        <div className="detail-loading">
          <Loader2 className="spin" />
          <span>Loading PR telemetry</span>
        </div>
      </section>
    );
  }

  if (detail.error) {
    return (
      <section className="detail-panel">
        <ErrorState message={detail.error} onRetry={onRetry} />
      </section>
    );
  }

  const pr = detail.data;
  if (!pr) {
    return (
      <section className="detail-panel">
        <EmptyState title="No detail" detail="The API returned no detail for this pull request." />
      </section>
    );
  }

  return (
    <section className="detail-panel">
      <div className="detail-header">
        <div>
          <div className="eyebrow">PR Detail</div>
          <h2>{prLabel(pr)}</h2>
          <a href={pr.url} target="_blank" rel="noreferrer">
            {pr.url}
          </a>
        </div>
      </div>

      <div className="metric-grid">
        <Metric label="State" value={<StatusPill value={pr.status} />} />
        <Metric label="Worker" value={<StatusPill value={pr.worker_status} />} />
        <Metric label="CI" value={<StatusPill value={pr.latest_ci_state} />} />
        <Metric label="Turns" value={`${pr.turns_used ?? 0}/${pr.max_turns ?? 0}`} />
        <Metric label="Active" value={activeAttemptLabel(pr)} />
        <Metric label="Provider" value={pr.provider || "unset"} />
      </div>

      <div className="metric-grid secondary">
        <Metric label="Last Check" value={formatDate(pr.latest_checked_at ?? pr.updated_at)} />
        <Metric label="Last Attempt" value={pr.last_attempt_status || "none"} />
        <Metric label="Elapsed" value={elapsedLabel(pr.active_attempt_elapsed_seconds)} />
        <Metric label="Last Error" value={pr.last_error || "none"} />
      </div>

      {pr.latest_summary ? (
        <section className="section-band">
          <div className="panel-title">
            <Activity aria-hidden="true" />
            <span>Status Summary</span>
          </div>
          <p className="summary-text">{pr.latest_summary}</p>
        </section>
      ) : null}

      <section className="section-band">
        <div className="panel-title">
          <Check aria-hidden="true" />
          <span>Head</span>
        </div>
        {pr.latest_head_sha ? (
          <div className="check-row">
            <span>{pr.latest_head_sha}</span>
            <StatusPill value={pr.latest_ci_state} />
          </div>
        ) : (
          <EmptyState title="No check yet" detail="No CI snapshot has been recorded." />
        )}
      </section>

      <section className="section-band split">
        <div>
          <div className="panel-title">
            <Clock3 aria-hidden="true" />
            <span>Events</span>
          </div>
          {events.loading ? (
            <div className="detail-loading compact">
              <Loader2 className="spin" />
              <span>Loading events</span>
            </div>
          ) : events.error ? (
            <p className="error-text">{events.error}</p>
          ) : events.data.length ? (
            <div className="event-list">
              {events.data.map((event, index) => (
                <div className="event-row" key={event.id ?? index}>
                  <span className={`event-dot event-${statusTone(event.level)}`} />
                  <div>
                    <strong>{event.type || event.level || "event"}</strong>
                    <p>{event.message || JSON.stringify(event.payload ?? {})}</p>
                    <time>{formatDate(event.created_at)}</time>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <EmptyState title="No events" detail="Agent activity will appear here after it is recorded." />
          )}
        </div>

        <div>
          <div className="panel-title">
            <TerminalSquare aria-hidden="true" />
            <span>Logs</span>
          </div>
          <pre className="log-box">{collectLogs(pr, events.data)}</pre>
        </div>
      </section>
    </section>
  );
}

function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function activeAttemptLabel(pr: PullRequestDetail): string {
  if (!pr.active_attempt_id) return "idle";
  return `attempt ${pr.active_attempt_id}`;
}

function elapsedLabel(seconds?: number | null): string {
  if (seconds === null || seconds === undefined) return "-";
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function collectLogs(pr: PullRequestDetail, events: PrEvent[]): string {
  const providerLogs = events
    .map((event) => (isRecord(event.payload) ? (event.payload as ResolutionAttempt) : null))
    .filter(Boolean)
    .flatMap((attempt) => [
      attempt?.turn_number ? `turn ${attempt.turn_number}` : null,
      attempt?.provider_command ? `$ ${attempt.provider_command}` : null,
      attempt?.provider_output ?? null,
      attempt?.error ? `error: ${attempt.error}` : null,
    ]);
  const logs = [
    pr.latest_summary,
    pr.latest_head_sha ? `head ${pr.latest_head_sha}` : null,
    pr.last_provider_command ? `$ ${pr.last_provider_command}` : null,
    pr.last_provider_output,
    pr.last_error ? `error: ${pr.last_error}` : null,
    ...providerLogs,
  ].filter(Boolean);

  return logs.length ? logs.join("\n\n") : "No agent logs recorded.";
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Unexpected API error";
}

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
