"use client";

import { useEffect, useState } from "react";

type ConfigEditorProps = {
  value: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
};

export function ConfigEditor({ value, onChange }: ConfigEditorProps) {
  const [text, setText] = useState<string>(JSON.stringify(value, null, 2));
  const [error, setError] = useState<string>("");

  useEffect(() => {
    setText(JSON.stringify(value, null, 2));
  }, [value]);

  return (
    <div className="space-y-2">
      <textarea
        className="min-h-56 w-full rounded-lg border border-white/10 bg-black/40 p-3 font-mono text-sm text-zinc-200"
        value={text}
        onChange={(event) => {
          const next = event.target.value;
          setText(next);
          try {
            const parsed = JSON.parse(next) as Record<string, unknown>;
            setError("");
            onChange(parsed);
          } catch {
            setError("Invalid JSON");
          }
        }}
      />
      {error ? <p className="text-sm text-rose-300">{error}</p> : null}
    </div>
  );
}
