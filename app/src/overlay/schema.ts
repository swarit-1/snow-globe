/**
 * Tiny schema combinators: each one is a runtime validator and a JSON Schema (2020-12) at once, so the
 * overlay's message protocol (protocol.ts) is written once and the committed protocol.schema.json is
 * generated from it (PRD §10.3: the webview validates what the core sends before anything paints).
 *
 * The validators follow JSON Schema's semantics where they could differ in JavaScript: string lengths
 * count Unicode code points, `integer` accepts any whole number, patterns are Unicode regular
 * expressions, and an object accepts no property it does not declare. protocol.test.ts checks the
 * generated schema and the validators agree, with Ajv, on valid messages and on mutated ones.
 */

/** A JSON Schema fragment. */
export type JsonSchema = { readonly [k: string]: unknown };

/** A validator for values of type T, with its JSON Schema. */
export interface V<T> {
  readonly schema: JsonSchema;
  /** Null if `v` is valid; otherwise the first problem, as "path: message" (never the value itself). */
  validate(v: unknown, path: string): string | null;
  /** Phantom field carrying T for `Infer`; never set. */
  readonly _type?: T;
}

/** The type a validator accepts. */
export type Infer<X> = X extends V<infer T> ? T : never;

type Simplify<T> = { [K in keyof T]: T[K] } & {};

interface Doc {
  readonly description?: string;
}

const withDoc = (schema: Record<string, unknown>, o: Doc | undefined): JsonSchema =>
  o?.description === undefined ? schema : { ...schema, description: o.description };

/** Unicode code points in `s` (JSON Schema's string length). */
export function codePoints(s: string): number {
  let n = 0;
  for (const _ of s) n += 1;
  return n;
}

const kind = (v: unknown): string => (v === null ? 'null' : Array.isArray(v) ? 'array' : typeof v);

/** A string with optional length bounds and a pattern. */
export function str(o: Doc & { readonly min?: number; readonly max?: number; readonly pattern?: string } = {}): V<string> {
  const re = o.pattern === undefined ? null : new RegExp(o.pattern, 'u');
  const schema: Record<string, unknown> = { type: 'string' };
  if (o.min !== undefined) schema.minLength = o.min;
  if (o.max !== undefined) schema.maxLength = o.max;
  if (o.pattern !== undefined) schema.pattern = o.pattern;
  return {
    schema: withDoc(schema, o),
    validate(v, path) {
      if (typeof v !== 'string') return `${path}: expected a string, got ${kind(v)}`;
      const n = o.min !== undefined || o.max !== undefined ? codePoints(v) : 0;
      if (o.min !== undefined && n < o.min) return `${path}: shorter than ${o.min}`;
      if (o.max !== undefined && n > o.max) return `${path}: longer than ${o.max}`;
      if (re !== null && !re.test(v)) return `${path}: does not match ${o.pattern}`;
      return null;
    },
  };
}

/** Exactly `value`. */
export function lit<const L extends string | number | boolean>(value: L, o?: Doc): V<L> {
  return {
    schema: withDoc({ const: value }, o),
    validate: (v, path) => (v === value ? null : `${path}: expected ${JSON.stringify(value)}`),
  };
}

/** One of the given strings. */
export function enumOf<const L extends readonly string[]>(values: L, o?: Doc): V<L[number]> {
  const set = new Set<unknown>(values);
  return {
    schema: withDoc({ enum: [...values] }, o),
    validate: (v, path) => (set.has(v) ? null : `${path}: expected one of ${values.join(', ')}`),
  };
}

/** A whole number in [min, max]. */
export function int(o: Doc & { readonly min: number; readonly max: number }): V<number> {
  return {
    schema: withDoc({ type: 'integer', minimum: o.min, maximum: o.max }, o),
    validate(v, path) {
      if (typeof v !== 'number' || !Number.isInteger(v)) return `${path}: expected an integer, got ${kind(v)}`;
      if (v < o.min || v > o.max) return `${path}: outside ${o.min}..${o.max}`;
      return null;
    },
  };
}

/** A finite number in [min, max]. */
export function num(o: Doc & { readonly min: number; readonly max: number }): V<number> {
  return {
    schema: withDoc({ type: 'number', minimum: o.min, maximum: o.max }, o),
    validate(v, path) {
      if (typeof v !== 'number' || !Number.isFinite(v)) return `${path}: expected a number, got ${kind(v)}`;
      if (!(v >= o.min && v <= o.max)) return `${path}: outside ${o.min}..${o.max}`;
      return null;
    },
  };
}

/** true or false. */
export function bool(o?: Doc): V<boolean> {
  return {
    schema: withDoc({ type: 'boolean' }, o),
    validate: (v, path) => (typeof v === 'boolean' ? null : `${path}: expected a boolean, got ${kind(v)}`),
  };
}

/** `inner`, or null. */
export function nullable<T>(inner: V<T>, o?: Doc): V<T | null> {
  return {
    schema: withDoc({ anyOf: [inner.schema, { type: 'null' }] }, o),
    validate: (v, path) => (v === null ? null : inner.validate(v, path)),
  };
}

/** An array of `item`, with length bounds and optionally unique items (primitives only). */
export function arr<T>(item: V<T>, o: Doc & { readonly min?: number; readonly max: number; readonly unique?: boolean }): V<T[]> {
  const schema: Record<string, unknown> = { type: 'array', items: item.schema, maxItems: o.max };
  if (o.min !== undefined) schema.minItems = o.min;
  if (o.unique) schema.uniqueItems = true;
  return {
    schema: withDoc(schema, o),
    validate(v, path) {
      if (!Array.isArray(v)) return `${path}: expected an array, got ${kind(v)}`;
      if (o.min !== undefined && v.length < o.min) return `${path}: fewer than ${o.min} items`;
      if (v.length > o.max) return `${path}: more than ${o.max} items`;
      for (let i = 0; i < v.length; i++) {
        const e = item.validate(v[i], `${path}[${i}]`);
        if (e !== null) return e;
      }
      if (o.unique && new Set(v.map((x) => JSON.stringify(x))).size !== v.length) return `${path}: items are not unique`;
      return null;
    },
  };
}

type ObjType<P extends Record<string, V<unknown>>, O extends keyof P> = Simplify<
  { -readonly [K in Exclude<keyof P, O>]: Infer<P[K]> } & { -readonly [K in O]?: Infer<P[K]> }
>;

/** An object validator that also exposes its properties (for discriminated unions). */
export interface ObjV<T> extends V<T> {
  readonly props: Readonly<Record<string, V<unknown>>>;
}

/** An object with exactly these properties; those listed in `optional` may be absent. */
export function obj<P extends Record<string, V<unknown>>, O extends keyof P & string = never>(
  props: P,
  o: Doc & { readonly optional?: readonly O[] } = {},
): ObjV<ObjType<P, O>> {
  const optional = new Set<string>(o.optional ?? []);
  const keys = Object.keys(props);
  const required = keys.filter((k) => !optional.has(k));
  const properties: Record<string, unknown> = {};
  for (const k of keys) properties[k] = props[k]!.schema;
  return {
    props,
    schema: withDoc({ type: 'object', additionalProperties: false, required, properties }, o),
    validate(v, path) {
      if (typeof v !== 'object' || v === null || Array.isArray(v)) return `${path}: expected an object, got ${kind(v)}`;
      const rec = v as Record<string, unknown>;
      for (const k of required) if (!Object.hasOwn(rec, k)) return `${path}: missing ${k}`;
      for (const k of Object.keys(rec)) {
        // Own properties only: a key named after an Object.prototype member ("constructor",
        // "__proto__", "toString") must be refused like any other unknown key, not resolve to a built-in.
        const p = Object.hasOwn(props, k) ? props[k] : undefined;
        if (p === undefined) return `${path}: unexpected property`; // never echo the name: it could be content
        const e = p.validate(rec[k], `${path}.${k}`);
        if (e !== null) return e;
      }
      return null;
    },
  };
}

/** An object with no declared properties ({} only), such as the core's empty `overrides`. */
export function emptyObj(o?: Doc): V<Record<string, never>> {
  return {
    schema: withDoc({ type: 'object', additionalProperties: false, maxProperties: 0 }, o),
    validate(v, path) {
      if (typeof v !== 'object' || v === null || Array.isArray(v)) return `${path}: expected an object, got ${kind(v)}`;
      return Object.keys(v).length === 0 ? null : `${path}: expected an empty object`;
    },
  };
}

/**
 * One of several object variants, told apart by the constant value of `field` in each. The schema is
 * a plain `oneOf`; because the constants differ, exactly one variant can match, so picking the
 * variant by `field` and validating only it is the same rule, with a clearer message.
 */
export function discriminated<const Vs extends readonly ObjV<unknown>[]>(field: string, variants: Vs, o?: Doc): V<Infer<Vs[number]>> {
  const byValue = new Map<unknown, ObjV<unknown>>();
  for (const variant of variants) {
    const c = (variant.props[field]?.schema as { const?: unknown } | undefined)?.const;
    if (c === undefined) throw new Error(`discriminated: a variant has no constant ${field}`);
    if (byValue.has(c)) throw new Error(`discriminated: two variants with ${field} = ${String(c)}`);
    byValue.set(c, variant);
  }
  const allowed = [...byValue.keys()].map((k) => JSON.stringify(k)).join(', ');
  return {
    schema: withDoc({ oneOf: variants.map((x) => x.schema) }, o),
    validate(v, path) {
      if (typeof v !== 'object' || v === null || Array.isArray(v)) return `${path}: expected an object, got ${kind(v)}`;
      const variant = Object.hasOwn(v, field) ? byValue.get((v as Record<string, unknown>)[field]) : undefined;
      if (variant === undefined) return `${path}.${field}: expected one of ${allowed}`;
      return variant.validate(v, path);
    },
  };
}
