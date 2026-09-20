// Pure map math for the CAD-style /cameras editor. Unit coordinate space:
// x,y ∈ 0..1 over the floorplan image, y DOWN (image convention), north = up.
// Azimuths are compass degrees: 0 = north, clockwise. Ported from the legacy
// cameras.js (same conventions the server's ground projection mirrors).

export const CARDINALS = [
  "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
];

export const CARDINAL_DEG = Object.fromEntries(
  CARDINALS.map((c, i) => [c, i * 22.5]),
);

export function cardinalOf(az) {
  return CARDINALS[Math.round(normAngle(az) / 22.5) % 16];
}

export function normAngle(a) {
  return ((a % 360) + 360) % 360;
}

export function clamp01(v) {
  return Math.min(1, Math.max(0, v));
}

// Snap a value to a step; step 0/undefined or bypass=true passes through.
export function snap(v, step, bypass) {
  if (bypass || !step) return v;
  return Math.round(v / step) * step;
}

// Map height/width ratio: the floorplan's pixel aspect, 1 for the square
// default map.
export function mapAspect(doc) {
  const fp = doc && doc.floorplan;
  return fp && fp.w && fp.h ? fp.h / fp.w : 1;
}

// Feet per unit on each axis (needs map_scale_ft = map width in feet).
export function unitToFt(doc) {
  const s = doc && doc.map_scale_ft;
  if (!s) return null;
  return { x: s, y: s * mapAspect(doc) };
}

// Compass azimuth -> unit-space direction (y down: north is -y).
export function azDir(az) {
  const r = (az * Math.PI) / 180;
  return { x: Math.sin(r), y: -Math.cos(r) };
}

// Unit-space vector (camera -> pointer) -> compass azimuth.
export function dirAz(dx, dy) {
  return normAngle((Math.atan2(dx, -dy) * 180) / Math.PI);
}

// SVG path of a view wedge: apex at pos, bisected by az, fov wide, radius r.
export function wedgePath(pos, az, fov, r) {
  const a0 = ((az - fov / 2) * Math.PI) / 180;
  const a1 = ((az + fov / 2) * Math.PI) / 180;
  const p0 = { x: pos.x + Math.sin(a0) * r, y: pos.y - Math.cos(a0) * r };
  const p1 = { x: pos.x + Math.sin(a1) * r, y: pos.y - Math.cos(a1) * r };
  const large = fov > 180 ? 1 : 0;
  return `M ${pos.x} ${pos.y} L ${p0.x} ${p0.y} ` +
    `A ${r} ${r} 0 ${large} 1 ${p1.x} ${p1.y} Z`;
}

// Floorplan rotation (true-north correction) as an SVG transform on the plan
// IMAGE only — geometry stays north-up. Rotates in aspect-corrected
// (real-world) space: rotating raw unit coords on a non-square map would
// shear the picture.
export function floorplanTransform(doc) {
  const fp = doc && doc.floorplan;
  const rot = fp && fp.rotation_deg;
  if (!rot) return null;
  const a = mapAspect(doc);
  return `translate(0.5 0.5) scale(1 ${1 / a}) rotate(${rot}) ` +
    `scale(1 ${a}) translate(-0.5 -0.5)`;
}
