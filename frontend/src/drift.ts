import type { Tone } from "./ui";

/** Server clock vs Discord's, for the Time drift tile: green under 30 s, yellow from 30 s to 60 s,
 * red over 60 s. The word goes on a badge so colour isn't the only signal. */
export function driftTone(seconds: number): [Tone, string] {
  const s = Math.abs(seconds);
  return s < 30 ? ["ok", "OK"] : s <= 60 ? ["warn", "Check NTP"] : ["danger", "Fix the clock"];
}
