/** Minimal --flag value parser for the renderer scripts (no dependency). */
import { isAbsolute, resolve } from 'node:path';

export function parseArgs(argv: string[], spec: { values: string[]; flags: string[] }): { values: Record<string, string>; flags: Set<string> } {
  const values: Record<string, string> = {};
  const flags = new Set<string>();
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]!;
    if (a === '--') continue;
    if (!a.startsWith('--')) throw new Error(`unexpected argument: ${a}`);
    const [name, inline] = a.slice(2).split('=', 2) as [string, string | undefined];
    if (spec.flags.includes(name)) {
      flags.add(name);
    } else if (spec.values.includes(name)) {
      const v = inline ?? argv[++i];
      if (v === undefined) throw new Error(`--${name} needs a value`);
      values[name] = v;
    } else {
      throw new Error(`unknown option --${name}`);
    }
  }
  return { values, flags };
}

/** Resolves a user path against the directory pnpm was invoked from (INIT_CWD), not the package dir. */
export function userPath(p: string): string {
  return isAbsolute(p) ? p : resolve(process.env.INIT_CWD ?? process.cwd(), p);
}
