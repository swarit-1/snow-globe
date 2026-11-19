/**
 * v0 cloud-type constants (PRD §9.2 pass 3). These are fixed placeholders, not fitted values:
 * TODO(M2) derive optical depth from HRRR cloud water/ice paths and fit cell sizes on dev frames.
 */

export type SlabKind = 'low' | 'mid' | 'high' | 'ceiling';

/** Stable noise identity per layer kind, so the same layer gets the same clouds whatever the list order. */
export const LAYER_SLOT: Readonly<Record<SlabKind, number>> = { low: 0, mid: 1, high: 2, ceiling: 3 };

export interface SlabTypeParams {
  /** Octave-0 lattice cell size of the cloud noise (m); octave o uses cell / 2^o. */
  cellSizeM: number;
  /** In-cloud extinction coefficient (per m). In-cloud visibility is 3.912 / beta. */
  betaPerM: number;
}

export const SLAB_TYPES: Readonly<Record<SlabKind, SlabTypeParams>> = {
  /** Stratus / stratocumulus: in-cloud visibility ~130 m. */
  low: { cellSizeM: 2400, betaPerM: 0.03 },
  /** Altostratus / altocumulus: in-cloud visibility ~330 m. */
  mid: { cellSizeM: 3600, betaPerM: 0.012 },
  /** Cirrus: optical depth ~0.6 per km of thickness, so usually semi-transparent. */
  high: { cellSizeM: 7200, betaPerM: 0.0006 },
  /** Ceiling slab (solid, no noise): treated as low cloud. */
  ceiling: { cellSizeM: 2400, betaPerM: 0.03 },
};

/** Henyey-Greenstein asymmetry of cloud droplets, used by the two-stream transmission below. */
export const CLOUD_ASYMMETRY_G = 0.85;

/**
 * Total (direct + diffuse) transmission of a conservative-scattering cloud of optical depth tau,
 * two-stream approximation: 1 / (1 + 0.75 (1 - g) tau).
 */
export function cloudTotalTransmission(tau: number): number {
  return 1 / (1 + 0.75 * (1 - CLOUD_ASYMMETRY_G) * tau);
}

/** Direct-beam transmission through a cloud of vertical optical depth tau for a Sun with sin(altitude) = mu. */
export function cloudDirectTransmission(tau: number, mu: number): number {
  return Math.exp(-tau / Math.max(mu, 0.05));
}
