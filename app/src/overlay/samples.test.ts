import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { coreQuery, DISPLAY, PARSER_CASES, photoLabel } from './samples.ts';

/** The repo's viewpoint manifests (viewpoints/manifests/<viewpoint_id>.json), read for their names only. */
const MANIFESTS = fileURLToPath(new URL('../../../viewpoints/manifests/', import.meta.url));
const manifest = (id: string): { place: string; truth_camera: { view_label: string } } => JSON.parse(readFileSync(`${MANIFESTS}${id}.json`, 'utf8'));

describe('sample display names follow the viewpoint manifests', () => {
  it('names each place as the manifest does, and each camera for what its view label says it shows', () => {
    const viewpoints = new Map<string, string>();
    for (const c of PARSER_CASES) if (c.exit === 0) {
      const q = coreQuery(c.utterance);
      viewpoints.set(q.place_id, q.viewpoint_id);
    }
    for (const [placeId, display] of Object.entries(DISPLAY)) {
      const id = viewpoints.get(placeId);
      expect(id, placeId).toBeDefined();
      const m = manifest(id!);
      expect(display.place_name, id).toBe(m.place);
      const camera = display.viewpoint_name.replace(/ camera$/, '');
      expect(m.truth_camera.view_label, id).toContain(camera);
    }
  });

  it('gives the sample photo the Point Reyes viewpoint’s camera name', () => {
    expect(photoLabel().camera_name).toBe(DISPLAY['point-reyes']!.viewpoint_name);
  });
});
