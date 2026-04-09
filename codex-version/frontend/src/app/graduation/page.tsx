"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { useAuth } from "@/lib/auth";
import { apiFetch } from "@/lib/api";

type GraduationStage =
  | "paper_candidate"
  | "paper_running"
  | "paper_review"
  | "live_candidate"
  | "live_running"
  | "paused"
  | "archived";

type GraduationCandidate = {
  id: string;
  promotion_id: string;
  report_id: string;
  created_at: string;
  updated_at: string;
  created_by?: string | null;
  stage: GraduationStage;
  notes?: string | null;
  strategy_id: string;
  strategy_name: string;
  strategy_path: string;
  instruments: string[];
  config: Record<string, unknown>;
  selection: {
    kind?: string;
    result_index?: number | null;
    window_index?: number | null;
    metrics?: Record<string, number>;
  };
  paper_trading: boolean;
  live_url: string;
  portfolio_url: string;
};

const STAGE_ORDER: GraduationStage[] = [
  "paper_candidate",
  "paper_running",
  "paper_review",
  "live_candidate",
  "live_running",
  "paused",
  "archived",
];

const STAGE_META: Record<
  GraduationStage,
  { label: string; tone: string; description: string }
> = {
  paper_candidate: {
    label: "Paper Candidate",
    tone: "border-cyan-300/25 bg-cyan-400/10 text-cyan-50",
    description: "Freshly promoted from research and waiting for paper deployment.",
  },
  paper_running: {
    label: "Paper Running",
    tone: "border-amber-300/25 bg-amber-400/10 text-amber-50",
    description: "Currently deployed in paper trading for operational validation.",
  },
  paper_review: {
    label: "Paper Review",
    tone: "border-violet-300/25 bg-violet-400/10 text-violet-50",
    description: "Paper run complete and under operator review.",
  },
  live_candidate: {
    label: "Live Candidate",
    tone: "border-emerald-300/25 bg-emerald-400/10 text-emerald-50",
    description: "Approved for live capital, pending final deployment.",
  },
  live_running: {
    label: "Live Running",
    tone: "border-emerald-300/30 bg-emerald-500/15 text-emerald-50",
    description: "Actively trading with the live runtime.",
  },
  paused: {
    label: "Paused",
    tone: "border-white/10 bg-white/5 text-zinc-100",
    description: "Intentionally paused while preserving audit state.",
  },
  archived: {
    label: "Archived",
    tone: "border-zinc-400/20 bg-zinc-400/10 text-zinc-200",
    description: "No longer active in the graduation loop.",
  },
};

export default function GraduationPage() {
  const searchParams = useSearchParams();
  const queryCandidateId = searchParams.get("candidate_id");
  const { token } = useAuth();
  const [candidates, setCandidates] = useState<GraduationCandidate[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<GraduationCandidate | null>(null);
  const [stageDraft, setStageDraft] = useState<GraduationStage>("paper_candidate");
  const [notesDraft, setNotesDraft] = useState("");
  const [statusMessage, setStatusMessage] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!token) return;

    async function loadCandidates() {
      try {
        const response = await apiFetch<GraduationCandidate[]>("/api/v1/graduation/candidates", token);
        setCandidates(response);
        setSelectedId((current) => {
          if (queryCandidateId && response.some((candidate) => candidate.id === queryCandidateId)) {
            return queryCandidateId;
          }
          if (current && response.some((candidate) => candidate.id === current)) {
            return current;
          }
          return response[0]?.id ?? "";
        });
        setError("");
      } catch (fetchError) {
        const message = fetchError instanceof Error ? fetchError.message : "Failed to load graduation candidates";
        setError(message);
      }
    }

    void loadCandidates();
  }, [queryCandidateId, token]);

  useEffect(() => {
    if (!token || !selectedId) {
      setDetail(null);
      return;
    }

    async function loadDetail() {
      try {
        const response = await apiFetch<GraduationCandidate>(`/api/v1/graduation/candidates/${selectedId}`, token);
        setDetail(response);
        setStageDraft(response.stage);
        setNotesDraft(response.notes ?? "");
      } catch (fetchError) {
        const message = fetchError instanceof Error ? fetchError.message : "Failed to load graduation detail";
        setError(message);
      }
    }

    void loadDetail();
  }, [selectedId, token]);

  const stageGroups = useMemo(
    () =>
      STAGE_ORDER.map((stage) => ({
        stage,
        meta: STAGE_META[stage],
        items: candidates.filter((candidate) => candidate.stage === stage),
      })),
    [candidates],
  );
  const stageCounts = useMemo(
    () =>
      STAGE_ORDER.reduce(
        (acc, stage) => {
          acc[stage] = candidates.filter((candidate) => candidate.stage === stage).length;
          return acc;
        },
        {} as Record<GraduationStage, number>,
      ),
    [candidates],
  );

  async function applyStageUpdate() {
    if (!token || !detail) return;
    try {
      setSaving(true);
      const response = await apiFetch<GraduationCandidate>(
        `/api/v1/graduation/candidates/${detail.id}/stage`,
        token,
        {
          method: "POST",
          body: JSON.stringify({
            stage: stageDraft,
            notes: notesDraft || null,
          }),
        },
      );
      setDetail(response);
      setCandidates((current) => current.map((row) => (row.id === response.id ? response : row)));
      setStatusMessage(`Candidate moved to ${STAGE_META[response.stage].label}.`);
      setError("");
    } catch (fetchError) {
      const message = fetchError instanceof Error ? fetchError.message : "Failed to update graduation stage";
      setError(message);
    } finally {
      setSaving(false);
    }
  }

  const paperFlowCount = stageCounts.paper_candidate + stageCounts.paper_running + stageCounts.paper_review;
  const liveFlowCount = stageCounts.live_candidate + stageCounts.live_running;

  return (
    <div className="space-y-6">
      <section className="grid gap-4 xl:grid-cols-[1.3fr_1fr]">
        <div className="rounded-[1.75rem] border border-white/10 bg-[radial-gradient(circle_at_top_left,rgba(56,189,248,0.18),transparent_45%),linear-gradient(180deg,rgba(8,12,18,0.94),rgba(10,14,21,0.84))] p-6">
          <p className="text-[11px] uppercase tracking-[0.3em] text-sky-200/80">Graduation Workflow</p>
          <h2 className="mt-3 text-3xl font-semibold text-white md:text-4xl">Turn research winners into paper and live sleeves with a visible state machine.</h2>
          <p className="mt-4 max-w-3xl text-sm leading-7 text-zinc-300">
            The research console creates candidates, this workspace governs their promotion, and live trading consumes
            the same API-first record. Nothing jumps straight from a backtest into capital without showing up here.
          </p>
          <div className="mt-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <StageSignal label="Total candidates" value={String(candidates.length)} tone="cyan" />
            <StageSignal label="Paper flow" value={String(paperFlowCount)} tone="amber" />
            <StageSignal label="Live flow" value={String(liveFlowCount)} tone="emerald" />
            <StageSignal label="Paused / archived" value={String(stageCounts.paused + stageCounts.archived)} tone="neutral" />
          </div>
        </div>

        <section className="rounded-[1.75rem] border border-white/10 bg-[linear-gradient(180deg,rgba(11,16,24,0.92),rgba(8,12,18,0.82))] p-5">
          <p className="text-[11px] uppercase tracking-[0.28em] text-zinc-500">Operator Guidance</p>
          <div className="mt-4 space-y-4 text-sm leading-7 text-zinc-300">
            <p>1. Promote from Research after a convincing sweep or walk-forward run.</p>
            <p>2. Move the candidate into paper deployment and let the live desk prove execution and reconciliation.</p>
            <p>3. Promote to live only after paper metrics and operational behavior both look clean.</p>
          </div>
        </section>
      </section>

      {statusMessage ? (
        <div className="rounded-[1.5rem] border border-emerald-300/30 bg-emerald-500/10 p-4 text-sm text-emerald-100">
          {statusMessage}
        </div>
      ) : null}
      {error ? (
        <div className="rounded-[1.5rem] border border-rose-300/30 bg-rose-500/10 p-4 text-sm text-rose-100">{error}</div>
      ) : null}

      <section className="grid gap-4 xl:grid-cols-3">
        {stageGroups.map((group) => (
          <section
            key={group.stage}
            className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5"
          >
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="text-[11px] uppercase tracking-[0.24em] text-zinc-500">{group.meta.label}</p>
                <p className="mt-2 text-sm text-zinc-400">{group.meta.description}</p>
              </div>
              <span className={`rounded-full border px-3 py-1 text-xs ${group.meta.tone}`}>{group.items.length}</span>
            </div>
            <div className="mt-4 space-y-3">
              {group.items.length === 0 ? (
                <div className="rounded-[1.2rem] border border-dashed border-white/10 bg-black/20 px-4 py-6 text-sm text-zinc-500">
                  No candidates in this stage.
                </div>
              ) : null}
              {group.items.map((candidate) => {
                const metrics = candidate.selection.metrics ?? {};
                const selected = candidate.id === selectedId;
                return (
                  <button
                    key={candidate.id}
                    type="button"
                    onClick={() => setSelectedId(candidate.id)}
                    className={`w-full rounded-[1.2rem] border p-4 text-left transition ${
                      selected
                        ? "border-cyan-300/40 bg-cyan-500/10"
                        : "border-white/10 bg-white/[0.03] hover:bg-white/5"
                    }`}
                  >
                    <p className="text-sm font-semibold text-white">{candidate.strategy_name}</p>
                    <p className="mt-1 text-xs text-zinc-500">{candidate.instruments.join(", ")}</p>
                    <div className="mt-4 grid gap-2 sm:grid-cols-3">
                      <MiniMetric label="Sharpe" value={formatMetric(metrics.sharpe)} />
                      <MiniMetric label="Return" value={formatPercent(metrics.total_return)} />
                      <MiniMetric label="Win Rate" value={formatPercent(metrics.win_rate)} />
                    </div>
                  </button>
                );
              })}
            </div>
          </section>
        ))}
      </section>

      <section className="grid gap-6 xl:grid-cols-[0.95fr_1.05fr]">
        <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h3 className="text-lg font-semibold text-white">Candidate Detail</h3>
              <p className="mt-1 text-sm text-zinc-400">
                Review configuration, notes, and the exact promotion source before changing the stage.
              </p>
            </div>
            {detail ? (
              <span className={`rounded-full border px-3 py-1 text-xs ${STAGE_META[detail.stage].tone}`}>
                {STAGE_META[detail.stage].label}
              </span>
            ) : null}
          </div>

          {detail ? (
            <div className="mt-5 space-y-5">
              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                <DetailMetric label="Strategy" value={detail.strategy_name} />
                <DetailMetric label="Mode" value={detail.paper_trading ? "Paper-first" : "Live-capable"} />
                <DetailMetric label="Selection" value={detail.selection.kind ?? "manual"} />
                <DetailMetric label="Updated" value={new Date(detail.updated_at).toLocaleString()} />
              </div>

              <div className="grid gap-4 md:grid-cols-2">
                <label className="space-y-2 text-sm text-zinc-300">
                  <span>Stage</span>
                  <select
                    value={stageDraft}
                    onChange={(event) => setStageDraft(event.target.value as GraduationStage)}
                    className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
                  >
                    {STAGE_ORDER.map((stage) => (
                      <option key={stage} value={stage}>
                        {STAGE_META[stage].label}
                      </option>
                    ))}
                  </select>
                </label>
                <div className="space-y-2 text-sm text-zinc-300">
                  <span>Quick links</span>
                  <div className="flex flex-wrap gap-2">
                    <Link href={detail.live_url} className="rounded-2xl border border-cyan-300/30 px-3 py-2 text-cyan-100">
                      Open Live Desk
                    </Link>
                    <Link href={detail.portfolio_url} className="rounded-2xl border border-violet-300/30 px-3 py-2 text-violet-100">
                      Open Portfolio Lab
                    </Link>
                  </div>
                </div>
              </div>

              <label className="space-y-2 text-sm text-zinc-300">
                <span>Operator notes</span>
                <textarea
                  value={notesDraft}
                  onChange={(event) => setNotesDraft(event.target.value)}
                  rows={5}
                  className="w-full rounded-[1.25rem] border border-white/10 bg-black/30 px-3 py-3 text-sm text-white"
                  placeholder="Record why this candidate is moving stages, what to watch in paper, or why it was paused."
                />
              </label>

              <div className="flex flex-wrap items-center justify-between gap-3">
                <p className="text-xs text-zinc-500">
                  Strategy path: <span className="font-mono">{detail.strategy_path}</span>
                </p>
                <button
                  type="button"
                  onClick={() => void applyStageUpdate()}
                  disabled={saving}
                  className="rounded-2xl border border-emerald-300/40 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-100 disabled:opacity-60"
                >
                  {saving ? "Saving..." : "Apply Stage Update"}
                </button>
              </div>
            </div>
          ) : (
            <div className="mt-4 rounded-[1.25rem] border border-dashed border-white/10 bg-black/20 px-4 py-10 text-sm text-zinc-500">
              Select a graduation candidate to inspect and manage it.
            </div>
          )}
        </section>

        <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h3 className="text-lg font-semibold text-white">Promotion Payload</h3>
              <p className="mt-1 text-sm text-zinc-400">Exact strategy config and selection metadata passed downstream.</p>
            </div>
          </div>

          {detail ? (
            <div className="mt-5 space-y-4">
              <div className="grid gap-3 sm:grid-cols-3">
                <MiniMetric label="Report" value={detail.report_id} wide />
                <MiniMetric label="Promotion" value={detail.promotion_id} wide />
                <MiniMetric label="Instruments" value={detail.instruments.join(", ")} wide />
              </div>
              <div className="grid gap-3 sm:grid-cols-3">
                <MiniMetric label="Sharpe" value={formatMetric(detail.selection.metrics?.sharpe)} />
                <MiniMetric label="Sortino" value={formatMetric(detail.selection.metrics?.sortino)} />
                <MiniMetric label="Return" value={formatPercent(detail.selection.metrics?.total_return)} />
              </div>
              <pre className="overflow-x-auto rounded-[1.25rem] border border-white/10 bg-black/30 p-4 text-xs text-zinc-200">
                {JSON.stringify(detail.config, null, 2)}
              </pre>
            </div>
          ) : (
            <div className="mt-4 rounded-[1.25rem] border border-dashed border-white/10 bg-black/20 px-4 py-10 text-sm text-zinc-500">
              Candidate config appears here once you select a row from the board.
            </div>
          )}
        </section>
      </section>
    </div>
  );
}

function StageSignal({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: "cyan" | "amber" | "emerald" | "neutral";
}) {
  const toneClasses =
    tone === "cyan"
      ? "border-cyan-300/20 bg-cyan-400/10 text-cyan-50"
      : tone === "amber"
        ? "border-amber-300/20 bg-amber-400/10 text-amber-50"
        : tone === "emerald"
          ? "border-emerald-300/20 bg-emerald-400/10 text-emerald-50"
          : "border-white/10 bg-white/5 text-zinc-50";
  return (
    <div className={`rounded-2xl border px-4 py-4 ${toneClasses}`}>
      <p className="text-[11px] uppercase tracking-[0.24em] text-white/60">{label}</p>
      <p className="mt-3 text-xl font-semibold">{value}</p>
    </div>
  );
}

function DetailMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl border border-white/10 bg-white/[0.03] px-4 py-4">
      <p className="text-[11px] uppercase tracking-[0.22em] text-zinc-500">{label}</p>
      <p className="mt-3 text-sm font-semibold text-white">{value}</p>
    </div>
  );
}

function MiniMetric({ label, value, wide = false }: { label: string; value: string; wide?: boolean }) {
  return (
    <div className={`rounded-xl border border-white/10 bg-black/20 px-3 py-3 ${wide ? "sm:col-span-1" : ""}`}>
      <p className="text-[10px] uppercase tracking-[0.22em] text-zinc-500">{label}</p>
      <p className="mt-2 break-all text-sm text-zinc-100">{value}</p>
    </div>
  );
}

function formatMetric(value: number | undefined): string {
  return value === undefined ? "N/A" : value.toFixed(2);
}

function formatPercent(value: number | undefined): string {
  return value === undefined ? "N/A" : `${(value * 100).toFixed(2)}%`;
}
