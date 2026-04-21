/**
 * Narrow JSON-Schema subset consumed by SchemaForm.
 *
 * The backend (`backend/src/msai/services/nautilus/schema_hooks.py`)
 * emits schemas matching this shape for any `StrategyConfig` subclass
 * whose types the Nautilus schema hook covers. Anything outside this
 * union falls through to the JSON-textarea fallback in `run-form.tsx`.
 */

/** Integer or float field. */
export interface NumberField {
  type: "integer" | "number";
  title?: string;
  description?: string;
  default?: number;
  minimum?: number;
  maximum?: number;
}

/** Boolean field. */
export interface BooleanField {
  type: "boolean";
  title?: string;
  description?: string;
  default?: boolean;
}

/**
 * String field. May be a decimal (format=decimal), a Nautilus-typed
 * identifier (x-format=instrument-id / bar-type), an enum (via `enum`),
 * or a free-form string.
 */
export interface StringField {
  type: "string";
  title?: string;
  description?: string;
  default?: string;
  format?: "decimal";
  /** Format hint from the msgspec schema_hook — drives the widget choice. */
  "x-format"?: "instrument-id" | "bar-type";
  examples?: string[];
  enum?: string[];
}

/** Nullable variant: `T | null` in Python shows up as anyOf(T, null). */
export interface NullableField {
  anyOf: [SchemaField, { type: "null" }];
  title?: string;
  description?: string;
  default?: unknown;
}

export type SchemaField =
  | NumberField
  | BooleanField
  | StringField
  | NullableField;

/** Top-level object schema — what `GET /api/v1/strategies/{id}` returns in `config_schema`. */
export interface ObjectSchema {
  type: "object";
  title?: string;
  properties: Record<string, SchemaField>;
  required?: string[];
}

/** Narrow a SchemaField to its non-null branch if nullable. */
export function unwrapNullable(field: SchemaField): {
  inner: SchemaField;
  nullable: boolean;
} {
  if ("anyOf" in field) {
    const nonNull = field.anyOf.find(
      (f): f is Exclude<SchemaField, { type: "null" }> =>
        "type" in f && f.type !== "null",
    );
    return {
      inner: nonNull ?? ({ type: "string" } as StringField),
      nullable: true,
    };
  }
  return { inner: field, nullable: false };
}
