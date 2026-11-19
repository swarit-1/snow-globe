import { defineConfig } from 'vitest/config';

/**
 * Two projects:
 * - unit: pure TypeScript math, CPU reference and Monte Carlo (src/**).
 * - headless: Playwright Chromium + SwiftShader determinism, golden and GPU-vs-CPU tests (test/**).
 *   These launch a browser, so files run one at a time with long timeouts.
 */
export default defineConfig({
  test: {
    projects: [
      {
        test: {
          name: 'unit',
          include: ['src/**/*.test.ts'],
          environment: 'node',
          testTimeout: 120_000,
        },
      },
      {
        test: {
          name: 'headless',
          include: ['test/**/*.test.ts'],
          environment: 'node',
          testTimeout: 300_000,
          hookTimeout: 120_000,
          fileParallelism: false,
        },
      },
    ],
  },
});
