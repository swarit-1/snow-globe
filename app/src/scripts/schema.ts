/**
 * Writes protocol.schema.json from overlay/protocol.ts (the single definition of the core-to-webview
 * protocol, PRD §10.3). protocol.test.ts fails while the committed file differs from what this writes.
 */
import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { protocolSchema } from '../overlay/protocol.ts';

export const SCHEMA_PATH = fileURLToPath(new URL('../protocol.schema.json', import.meta.url));

/** The schema file's exact text. */
export function schemaText(): string {
  return `${JSON.stringify(protocolSchema(), null, 2)}\n`;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  writeFileSync(SCHEMA_PATH, schemaText());
  console.log(`wrote ${SCHEMA_PATH}`);
}
