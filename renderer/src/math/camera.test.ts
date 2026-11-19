import { describe, expect, it } from 'vitest';
import { altAzToDirection, cameraBasis, directionToAltAz, pixelRay, projectDirection, type CameraPose } from './camera.ts';
import { DEG } from './constants.ts';
import { dot, length } from './vec3.ts';

const pose = (o: Partial<CameraPose> = {}): CameraPose => ({ width: 320, height: 180, heading_deg: 0, pitch_deg: 0, roll_deg: 0, hfov_deg: 60, ...o });

describe('camera rays (ENU, heading clockwise from north, pitch up positive)', () => {
  it('points the optical axis by heading and pitch', () => {
    const cases: [number, number, [number, number, number]][] = [
      [0, 0, [0, 1, 0]],
      [90, 0, [1, 0, 0]],
      [180, 0, [0, -1, 0]],
      [270, 0, [-1, 0, 0]],
      [0, 90, [0, 0, 1]],
      [45, 30, [Math.sin(45 * DEG) * Math.cos(30 * DEG), Math.cos(45 * DEG) * Math.cos(30 * DEG), Math.sin(30 * DEG)]],
    ];
    for (const [h, p, f] of cases) {
      const b = cameraBasis(pose({ heading_deg: h, pitch_deg: p }));
      for (let i = 0; i < 3; i++) expect(b.forward[i]).toBeCloseTo(f[i]!, 12);
    }
  });

  it('has an orthonormal, right-handed basis for arbitrary poses', () => {
    for (let k = 0; k < 50; k++) {
      const b = cameraBasis(pose({ heading_deg: k * 37.3, pitch_deg: -80 + k * 3.1, roll_deg: -170 + k * 7.1 }));
      for (const v of [b.forward, b.right, b.up]) expect(length(v)).toBeCloseTo(1, 12);
      expect(dot(b.forward, b.right)).toBeCloseTo(0, 12);
      expect(dot(b.forward, b.up)).toBeCloseTo(0, 12);
      expect(dot(b.right, b.up)).toBeCloseTo(0, 12);
      // right x up = backward for a camera looking along forward (image x right, y up, z toward viewer)
      const c = [b.right[1] * b.up[2] - b.right[2] * b.up[1], b.right[2] * b.up[0] - b.right[0] * b.up[2], b.right[0] * b.up[1] - b.right[1] * b.up[0]];
      for (let i = 0; i < 3; i++) expect(c[i]).toBeCloseTo(-b.forward[i]!, 12);
    }
  });

  it('level camera: up is world up, right is 90 degrees clockwise of heading', () => {
    const b = cameraBasis(pose({ heading_deg: 30 }));
    expect(b.up[2]).toBeCloseTo(1, 12);
    expect(directionToAltAz(b.right).az_deg).toBeCloseTo(120, 10);
  });

  it('positive roll turns the camera clockwise as seen from behind (top of frame tilts to the right)', () => {
    const b = cameraBasis(pose({ heading_deg: 0, roll_deg: 90 }));
    // camera up now points east (world right of a north-facing camera)
    expect(b.up[0]).toBeCloseTo(1, 12);
    expect(b.right[2]).toBeCloseTo(-1, 12);
  });

  it('spans the horizontal field of view between the left and right image edges', () => {
    const b = cameraBasis(pose({ hfov_deg: 60 }));
    const l = pixelRay(b, 0, 90);
    const r = pixelRay(b, 320, 90);
    expect(Math.acos(dot(l, r)) / DEG).toBeCloseTo(60, 9);
    const top = pixelRay(b, 160, 0);
    const bottom = pixelRay(b, 160, 180);
    const vfov = (2 * Math.atan(Math.tan(30 * DEG) * (180 / 320))) / DEG;
    expect(Math.acos(dot(top, bottom)) / DEG).toBeCloseTo(vfov, 9);
    expect(top[2]).toBeGreaterThan(0);
    expect(bottom[2]).toBeLessThan(0);
  });

  it('the image centre is the optical axis and projection inverts pixelRay', () => {
    const b = cameraBasis(pose({ heading_deg: 211, pitch_deg: 7, roll_deg: -3 }));
    const c = pixelRay(b, 160, 90);
    for (let i = 0; i < 3; i++) expect(c[i]).toBeCloseTo(b.forward[i]!, 12);
    for (const [px, py] of [
      [0.5, 0.5],
      [100.25, 33.75],
      [319.5, 179.5],
    ] as const) {
      const p = projectDirection(b, pixelRay(b, px, py))!;
      expect(p.px).toBeCloseTo(px, 9);
      expect(p.py).toBeCloseTo(py, 9);
    }
    expect(projectDirection(b, [-b.forward[0], -b.forward[1], -b.forward[2]])).toBeNull();
  });

  it('round-trips altitude and azimuth', () => {
    for (const [alt, az] of [
      [0, 0],
      [-12, 359.5],
      [45, 123.4],
      [89, 271],
    ] as const) {
      const r = directionToAltAz(altAzToDirection(alt, az));
      expect(r.alt_deg).toBeCloseTo(alt, 10);
      expect(r.az_deg).toBeCloseTo(az, 9);
    }
  });
});
