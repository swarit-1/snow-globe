import { defineConfig } from 'vitest/config';

/**
 * Two projects:
 * - unit: the overlay's pure logic (protocol, gate, scheduler, state machine, caption, chips, formats,
 *   pixel tag) and the TypeScript mirror of the Rust key-mash simulation, all in Node.
 * - browser: Playwright Chromium (headless shell): the DOM view, and the key-mash stress run on the
 *   browser's real event loop. These launch a browser, so files run one at a time with long timeouts.
 */
export default defineConfig({
  test: {
    projects: [
      {
        test: {
          name: 'unit',
          include: ['overlay/**/*.test.ts', 'harness/**/*.test.ts', 'preview/**/*.test.ts'],
          environment: 'node',
          testTimeout: 120_000,
        },
      },
      {
        test: {
          name: 'browser',
          include: ['test/**/*.test.ts'],
          environment: 'node',
          testTimeout: 900_000,
          hookTimeout: 120_000,
          fileParallelism: false,
        },
      },
    ],
  },
});
