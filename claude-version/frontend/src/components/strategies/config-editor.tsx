"use client";

import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

export interface ConfigEditorProps {
  value: string;
  onChange: (value: string) => void;
  className?: string;
}

export function ConfigEditor({
  value,
  onChange,
  className,
}: ConfigEditorProps): React.ReactElement {
  return (
    <Textarea
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={cn(
        "min-h-[200px] font-mono text-sm leading-relaxed",
        className,
      )}
      spellCheck={false}
      placeholder='{"key": "value"}'
    />
  );
}
