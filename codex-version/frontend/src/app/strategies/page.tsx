"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { StrategyCard } from "@/components/strategies/strategy-card";
import { useAuth } from "@/lib/auth";
import { apiFetch } from "@/lib/api";

type Strategy = {
  id: string;
  name: string;
  description?: string | null;
  strategy_class: string;
  file_path?: string;
};

type StrategyTemplate = {
  id: string;
  label: string;
  description: string;
  default_config: Record<string, unknown>;
};

export default function StrategiesPage() {
  const { token } = useAuth();
  const [items, setItems] = useState<Strategy[]>([]);
  const [templates, setTemplates] = useState<StrategyTemplate[]>([]);
  const [selectedTemplateId, setSelectedTemplateId] = useState("mean_reversion_zscore");
  const [moduleName, setModuleName] = useState("user.my_new_strategy");
  const [description, setDescription] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [creating, setCreating] = useState(false);
  const [syncing, setSyncing] = useState(false);

  const loadRegistry = useCallback(async () => {
    if (!token) return;
    try {
      const [strategies, templateList] = await Promise.all([
        apiFetch<Strategy[]>("/api/v1/strategies/", token),
        apiFetch<StrategyTemplate[]>("/api/v1/strategy-templates", token),
      ]);
      setItems(strategies);
      setTemplates(templateList);
      if (!templateList.some((template) => template.id === selectedTemplateId) && templateList[0]) {
        setSelectedTemplateId(templateList[0].id);
      }
      setError("");
    } catch (fetchError) {
      const loadMessage = fetchError instanceof Error ? fetchError.message : "Failed to load strategy registry";
      setError(loadMessage);
    }
  }, [selectedTemplateId, token]);

  useEffect(() => {
    void loadRegistry();
  }, [loadRegistry]);

  const selectedTemplate = useMemo(
    () => templates.find((template) => template.id === selectedTemplateId) ?? null,
    [selectedTemplateId, templates],
  );

  async function handleCreate() {
    if (!token) return;
    try {
      setCreating(true);
      setMessage("");
      const scaffolded = await apiFetch<{
        strategy_id?: string | null;
        name: string;
        file_path: string;
        strategy_class: string;
      }>("/api/v1/strategy-templates/scaffold", token, {
        method: "POST",
        body: JSON.stringify({
          template_id: selectedTemplateId,
          module_name: moduleName,
          description: description || null,
          force: false,
        }),
      });
      const synced = await apiFetch<Strategy[]>("/api/v1/strategies/sync", token, {
        method: "POST",
      });
      setItems(synced);
      setMessage(`Created ${scaffolded.name} from ${selectedTemplateId} and synced it into the registry.`);
      setError("");
    } catch (fetchError) {
      const createMessage = fetchError instanceof Error ? fetchError.message : "Failed to scaffold strategy";
      setError(createMessage);
      setMessage("");
    } finally {
      setCreating(false);
    }
  }

  async function handleSync() {
    if (!token) return;
    try {
      setSyncing(true);
      const synced = await apiFetch<Strategy[]>("/api/v1/strategies/sync", token, {
        method: "POST",
      });
      setItems(synced);
      setMessage(`Synced ${synced.length} strategies from disk into the registry.`);
      setError("");
    } catch (fetchError) {
      const syncMessage = fetchError instanceof Error ? fetchError.message : "Failed to sync strategy registry";
      setError(syncMessage);
      setMessage("");
    } finally {
      setSyncing(false);
    }
  }

  return (
    <div className="space-y-6">
      {error ? (
        <div className="rounded-xl border border-rose-300/30 bg-rose-500/10 p-4 text-sm text-rose-100">{error}</div>
      ) : null}
      {message ? (
        <div className="rounded-xl border border-emerald-300/30 bg-emerald-500/10 p-4 text-sm text-emerald-100">{message}</div>
      ) : null}

      <section className="grid gap-4 xl:grid-cols-[1.1fr_0.9fr]">
        <div className="rounded-[1.75rem] border border-white/10 bg-[radial-gradient(circle_at_top_left,rgba(59,130,246,0.18),transparent_45%),linear-gradient(180deg,rgba(8,12,18,0.94),rgba(10,14,21,0.84))] p-6">
          <p className="text-[11px] uppercase tracking-[0.3em] text-sky-200/80">Strategy Authoring</p>
          <h2 className="mt-3 text-3xl font-semibold text-white md:text-4xl">Create real Nautilus strategy modules without leaving the product surface.</h2>
          <p className="mt-4 max-w-3xl text-sm leading-7 text-zinc-300">
            Strategies are still real Python files on disk, which is exactly how Nautilus wants to load them. The API
            scaffolds the module, the registry syncs it, and the same strategy can immediately flow into research,
            graduation, portfolio construction, and live trading.
          </p>
          <div className="mt-6 grid gap-3 sm:grid-cols-3">
            <SignalCard label="Templates" value={String(templates.length)} tone="cyan" />
            <SignalCard label="Registered" value={String(items.length)} tone="emerald" />
            <SignalCard label="Workflow" value="API -> CLI -> UI" tone="violet" />
          </div>
        </div>

        <section className="rounded-[1.75rem] border border-white/10 bg-[linear-gradient(180deg,rgba(11,16,24,0.92),rgba(8,12,18,0.82))] p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="text-[11px] uppercase tracking-[0.28em] text-zinc-500">Registry Control</p>
              <h3 className="mt-2 text-xl font-semibold text-white">Filesystem-backed sync</h3>
            </div>
            <button
              type="button"
              onClick={() => void handleSync()}
              disabled={syncing}
              className="rounded-2xl border border-cyan-300/30 px-4 py-2 text-sm text-cyan-100 disabled:opacity-60"
            >
              {syncing ? "Syncing..." : "Sync Registry"}
            </button>
          </div>
          <p className="mt-4 text-sm leading-7 text-zinc-300">
            Codex, Claude, or a developer can create strategy files directly in the repo. This sync action pulls those
            files into the strategy registry so the same API/UI can validate and run them.
          </p>
        </section>
      </section>

      <section className="grid gap-6 xl:grid-cols-[0.95fr_1.05fr]">
        <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h3 className="text-lg font-semibold text-white">Scaffold Strategy</h3>
              <p className="mt-1 text-sm text-zinc-400">
                Create a new Nautilus-compatible strategy module from a safe template.
              </p>
            </div>
            <span className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-zinc-300">
              API-first
            </span>
          </div>

          <div className="mt-5 grid gap-4 md:grid-cols-2">
            <label className="space-y-2 text-sm text-zinc-300">
              <span>Template</span>
              <select
                value={selectedTemplateId}
                onChange={(event) => setSelectedTemplateId(event.target.value)}
                className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
              >
                {templates.map((template) => (
                  <option key={template.id} value={template.id}>
                    {template.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="space-y-2 text-sm text-zinc-300">
              <span>Module Name</span>
              <input
                value={moduleName}
                onChange={(event) => setModuleName(event.target.value)}
                className="w-full rounded-2xl border border-white/10 bg-black/30 px-3 py-3 text-white"
                placeholder="user.my_new_strategy"
              />
            </label>
          </div>

          <label className="mt-4 block space-y-2 text-sm text-zinc-300">
            <span>Description</span>
            <textarea
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              rows={4}
              className="w-full rounded-[1.25rem] border border-white/10 bg-black/30 px-3 py-3 text-white"
              placeholder="Optional custom description for the generated strategy class."
            />
          </label>

          <div className="mt-5 flex items-center justify-between gap-3">
            <p className="text-xs text-zinc-500">
              Module names become Python files under `strategies/` and must be valid dotted module paths.
            </p>
            <button
              type="button"
              onClick={() => void handleCreate()}
              disabled={creating || !selectedTemplateId || !moduleName.trim()}
              className="rounded-2xl border border-emerald-300/40 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-100 disabled:opacity-60"
            >
              {creating ? "Creating..." : "Create Strategy"}
            </button>
          </div>
        </section>

        <section className="rounded-[1.5rem] border border-white/10 bg-[linear-gradient(180deg,rgba(8,12,18,0.95),rgba(8,12,18,0.78))] p-5">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h3 className="text-lg font-semibold text-white">Template Preview</h3>
              <p className="mt-1 text-sm text-zinc-400">What the selected template is designed to do by default.</p>
            </div>
          </div>
          {selectedTemplate ? (
            <div className="mt-5 space-y-4">
              <div className="rounded-[1.2rem] border border-white/10 bg-black/20 p-4">
                <p className="text-lg font-semibold text-white">{selectedTemplate.label}</p>
                <p className="mt-2 text-sm leading-7 text-zinc-300">{selectedTemplate.description}</p>
              </div>
              <pre className="overflow-x-auto rounded-[1.2rem] border border-white/10 bg-black/30 p-4 text-xs text-zinc-200">
                {JSON.stringify(selectedTemplate.default_config, null, 2)}
              </pre>
            </div>
          ) : (
            <div className="mt-5 rounded-[1.2rem] border border-dashed border-white/10 bg-black/20 px-4 py-10 text-sm text-zinc-500">
              No strategy templates are available.
            </div>
          )}
        </section>
      </section>

      <section className="space-y-4">
        <h2 className="text-2xl font-semibold text-white">Strategy Registry</h2>
        <div className="grid gap-4 lg:grid-cols-2">
          {items.length === 0 ? <p className="text-sm text-zinc-500">No registered strategies found.</p> : null}
          {items.map((item) => (
            <StrategyCard
              key={item.id}
              name={item.name}
              description={item.description}
              status="ready"
            />
          ))}
        </div>
      </section>
    </div>
  );
}

function SignalCard({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: "cyan" | "emerald" | "violet";
}) {
  const toneClasses =
    tone === "cyan"
      ? "border-cyan-300/20 bg-cyan-400/10 text-cyan-50"
      : tone === "emerald"
        ? "border-emerald-300/20 bg-emerald-400/10 text-emerald-50"
        : "border-violet-300/20 bg-violet-400/10 text-violet-50";

  return (
    <div className={`rounded-2xl border px-4 py-4 ${toneClasses}`}>
      <p className="text-[11px] uppercase tracking-[0.24em] text-white/60">{label}</p>
      <p className="mt-3 text-xl font-semibold">{value}</p>
    </div>
  );
}
