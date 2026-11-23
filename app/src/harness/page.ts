/**
 * Browser entry for the test pages: exposes the stress run, the view driver and the overlay to
 * Playwright (test/stress.browser.test.ts, test/view.browser.test.ts, scripts/stress.ts).
 */
import * as overlay from '../overlay/index.ts';
import * as samples from '../overlay/samples.ts';
import { runStress } from './stress.ts';
import { VARIANTS } from './mutants.ts';
import { ViewDriver } from './viewdriver.ts';

(globalThis as unknown as { SnowGlobeHarness: unknown }).SnowGlobeHarness = { runStress, VARIANTS, overlay, samples, ViewDriver };
