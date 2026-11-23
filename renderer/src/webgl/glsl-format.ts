/** Formatting of TypeScript constants for injection into GLSL ES 3.00 sources (every literal float has a '.' or an exponent). */
import type { RGB } from '../light.ts';

/** A float literal with 10 significant digits. */
export const glslFloat = (x: number): string => {
  const s = x.toPrecision(10);
  return /[.e]/.test(s) ? s : `${s}.0`;
};

/** A vec3 literal. */
export const glslVec3 = (c: RGB): string => `vec3(${glslFloat(c[0])}, ${glslFloat(c[1])}, ${glslFloat(c[2])})`;
