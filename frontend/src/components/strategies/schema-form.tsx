"use client";

/**
 * SchemaForm — auto-renders a strategy's config fields from its JSON Schema.
 *
 * Consumes the schema produced by
 * ``backend/src/msai/services/nautilus/schema_hooks.build_user_schema``
 * and exposed via ``GET /api/v1/strategies/{id}`` as ``config_schema``.
 * Supports int / float / decimal / boolean / enum / string (incl.
 * Nautilus-typed strings via ``x-format``) / nullable (anyOf(T, null)).
 *
 * Anything outside that subset is silently skipped — the caller
 * (``run-form.tsx``) should check ``config_schema_status === 'ready'``
 * and fall back to a raw JSON textarea when the schema isn't available.
 *
 * Design decisions (per 2026-04-20 council):
 * - Server validation remains authoritative. This component does NOT
 *   validate on submit — that's the 422 round-trip's job.
 * - ``instrument_id`` / ``bar_type`` are hidden by default (backend
 *   injects them from the separate ``instruments`` top-level input to
 *   avoid duplicate data-entry fields). Pass ``hiddenFields={[]}`` to
 *   override.
 * - Field errors are rendered inline under the field using the
 *   ``errors[fieldName]`` map. The 422 envelope from the API
 *   (``error.details[].field``) is the source of these messages.
 */

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

import type {
  BooleanField,
  NumberField,
  ObjectSchema,
  SchemaField,
  StringField,
} from "./schema-form.types";
import { unwrapNullable } from "./schema-form.types";

export interface SchemaFormProps {
  schema: ObjectSchema;
  /** Current form value — typically seeded from `default_config`. */
  value: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
  /** Map of field name → server 422 error message to render inline. */
  errors?: Record<string, string>;
  /**
   * Fields to NOT render — useful when the backend injects them from
   * other inputs (default: ``instrument_id`` and ``bar_type``).
   */
  hiddenFields?: string[];
}

const DEFAULT_HIDDEN: readonly string[] = ["instrument_id", "bar_type"];

export function SchemaForm({
  schema,
  value,
  onChange,
  errors,
  hiddenFields = [...DEFAULT_HIDDEN],
}: SchemaFormProps): React.ReactElement {
  const hiddenSet = new Set(hiddenFields);
  const required = new Set(schema.required ?? []);

  const setField = (name: string, v: unknown): void => {
    const next = { ...value };
    if (v === undefined) {
      delete next[name];
    } else {
      next[name] = v;
    }
    onChange(next);
  };

  return (
    <div className="space-y-3">
      {Object.entries(schema.properties).map(([name, field]) => {
        if (hiddenSet.has(name)) return null;
        return (
          <FieldRenderer
            key={name}
            name={name}
            field={field}
            value={value[name]}
            required={required.has(name)}
            onChange={(v) => setField(name, v)}
            error={errors?.[name]}
          />
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------
// FieldRenderer — dispatches on field type
// ---------------------------------------------------------------------

interface FieldRendererProps {
  name: string;
  field: SchemaField;
  value: unknown;
  required: boolean;
  onChange: (v: unknown) => void;
  error?: string;
}

function FieldRenderer({
  name,
  field,
  value,
  required,
  onChange,
  error,
}: FieldRendererProps): React.ReactElement {
  const { inner, nullable } = unwrapNullable(field);
  const title = getFieldTitle(inner, name);
  const isNull = value === null;

  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between">
        <Label htmlFor={`sf-${name}`}>
          {title}
          {required && !nullable && (
            <span className="ml-1 text-destructive">*</span>
          )}
        </Label>
        {hasDescription(inner) && (
          <span className="text-xs text-muted-foreground">
            {(inner as { description?: string }).description}
          </span>
        )}
      </div>
      {nullable && (
        // Nullable fields (T | None in Python, anyOf(T, null) in schema)
        // expose a checkbox that explicitly emits `null` — without it,
        // the user can only omit the field or provide a concrete value,
        // which differs from submitting an explicit null for downstream
        // consumers that treat "omitted" and "null" differently.
        // Addresses Codex code-review P2 2026-04-21.
        <label
          className="flex items-center gap-2 text-xs text-muted-foreground"
          htmlFor={`sf-${name}-null`}
        >
          <input
            id={`sf-${name}-null`}
            type="checkbox"
            className="size-3 rounded border-input bg-background accent-primary"
            checked={isNull}
            onChange={(e) => onChange(e.target.checked ? null : undefined)}
          />
          Use null (unset)
        </label>
      )}
      {!isNull && renderWidget({ name, field: inner, value, onChange })}
      {error && (
        <p className="text-xs text-destructive" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------
// Widget dispatch
// ---------------------------------------------------------------------

interface WidgetProps {
  name: string;
  field: SchemaField;
  value: unknown;
  onChange: (v: unknown) => void;
}

function renderWidget({
  name,
  field,
  value,
  onChange,
}: WidgetProps): React.ReactElement | null {
  if (!("type" in field)) return null; // Nullable wrapper — unreachable here.

  if (field.type === "integer" || field.type === "number") {
    return (
      <NumberWidget
        name={name}
        field={field as NumberField}
        value={value}
        onChange={onChange}
      />
    );
  }
  if (field.type === "boolean") {
    return (
      <BooleanWidget
        name={name}
        field={field as BooleanField}
        value={value}
        onChange={onChange}
      />
    );
  }
  if (field.type === "string") {
    const str = field as StringField;
    if (str.enum) {
      return (
        <EnumWidget name={name} field={str} value={value} onChange={onChange} />
      );
    }
    return (
      <StringWidget name={name} field={str} value={value} onChange={onChange} />
    );
  }
  return null;
}

function NumberWidget({
  name,
  field,
  value,
  onChange,
}: {
  name: string;
  field: NumberField;
  value: unknown;
  onChange: (v: unknown) => void;
}): React.ReactElement {
  const current =
    typeof value === "number"
      ? String(value)
      : typeof value === "string"
        ? value
        : (field.default?.toString() ?? "");
  return (
    <Input
      id={`sf-${name}`}
      type="number"
      step={field.type === "integer" ? 1 : "any"}
      min={field.minimum}
      max={field.maximum}
      value={current}
      onChange={(e) => {
        const raw = e.target.value;
        if (raw === "") {
          onChange(undefined);
          return;
        }
        const n =
          field.type === "integer" ? parseInt(raw, 10) : parseFloat(raw);
        onChange(Number.isNaN(n) ? raw : n);
      }}
    />
  );
}

function BooleanWidget({
  name,
  field,
  value,
  onChange,
}: {
  name: string;
  field: BooleanField;
  value: unknown;
  onChange: (v: unknown) => void;
}): React.ReactElement {
  const checked = typeof value === "boolean" ? value : (field.default ?? false);
  return (
    <input
      id={`sf-${name}`}
      type="checkbox"
      className="size-4 rounded border-input bg-background accent-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      checked={checked}
      onChange={(e) => onChange(e.target.checked)}
    />
  );
}

function StringWidget({
  name,
  field,
  value,
  onChange,
}: {
  name: string;
  field: StringField;
  value: unknown;
  onChange: (v: unknown) => void;
}): React.ReactElement {
  const current = typeof value === "string" ? value : (field.default ?? "");
  const placeholder = field.examples?.[0] ?? "";
  return (
    <Input
      id={`sf-${name}`}
      type="text"
      inputMode={field.format === "decimal" ? "decimal" : undefined}
      placeholder={placeholder}
      value={current}
      onChange={(e) => {
        const raw = e.target.value;
        if (raw === "" && field.default === undefined) {
          onChange(undefined);
          return;
        }
        onChange(raw);
      }}
    />
  );
}

function EnumWidget({
  name,
  field,
  value,
  onChange,
}: {
  name: string;
  field: StringField;
  value: unknown;
  onChange: (v: unknown) => void;
}): React.ReactElement {
  const current = typeof value === "string" ? value : (field.default ?? "");
  return (
    <Select value={String(current)} onValueChange={onChange}>
      <SelectTrigger id={`sf-${name}`}>
        <SelectValue placeholder="Select..." />
      </SelectTrigger>
      <SelectContent>
        {(field.enum ?? []).map((opt) => (
          <SelectItem key={opt} value={opt}>
            {opt}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

// ---------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------

function getFieldTitle(field: SchemaField, fallbackName: string): string {
  if ("title" in field && field.title) return field.title;
  return humanize(fallbackName);
}

function hasDescription(field: SchemaField): boolean {
  return "description" in field && !!field.description;
}

function humanize(name: string): string {
  return name.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// ---------------------------------------------------------------------
// Default-value seeding helper for callers
// ---------------------------------------------------------------------

/**
 * Seed a form-state object from a schema's default_config dict, skipping
 * fields listed in ``hiddenFields``. Called once when the strategy
 * selection changes in ``run-form.tsx``.
 */
export function seedFromDefaults(
  defaults: Record<string, unknown> | null | undefined,
  hiddenFields: readonly string[] = DEFAULT_HIDDEN,
): Record<string, unknown> {
  if (!defaults) return {};
  const hiddenSet = new Set(hiddenFields);
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(defaults)) {
    if (hiddenSet.has(k)) continue;
    out[k] = v;
  }
  return out;
}
