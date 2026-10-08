export const STATES = ['idle','running-right','running-left','waving','jumping','failed','waiting','running','review'];
const COUNTS = [6,8,8,4,5,8,6,6,6];
export function frame(state, elapsed, version = 2, direction = 0) {
  if (state === 'look' && version === 2) {
    const n = ((Math.round(direction / 22.5) % 16) + 16) % 16;
    return { x: n % 8 * 192, y: (9 + Math.floor(n / 8)) * 208 };
  }
  const row = Math.max(0, STATES.indexOf(state));
  return { x: Math.floor(Math.max(0, elapsed) / 125) % COUNTS[row] * 192, y: row * 208 };
}
// Reply emotion (server.EMOTIONS_OK) → a short reaction from rows every sheet has.
export const EMOTION_MOTION = {happy:'jumping', laugh:'jumping', surprised:'jumping', sad:'failed', angry:'failed',
  embarrassed:'waiting', thinking:'review', neutral:'waving'};
export const reaction = emotion => EMOTION_MOTION[emotion] || 'waving';
// Front-facing look frame toward a horizontal cursor offset: 0 is front, 1..4 turn right, 15..12 turn left.
export function lookDirection(dx) {
  const n = Math.max(-4, Math.min(4, Math.round(dx / 60)));
  return ((n + 16) % 16) * 22.5;
}
// Chromium decodes a truncated WebP from its header alone and yields blank pixels, so a usable sheet must
// also have something drawn in its first idle frame.
export function hasPixels(image) {
  const ctx = new OffscreenCanvas(192, 208).getContext('2d', {willReadFrequently: true});
  ctx.drawImage(image, 0, 0, 192, 208, 0, 0, 192, 208);
  const data = ctx.getImageData(0, 0, 192, 208).data;
  for (let i = 3; i < data.length; i += 4) if (data[i] > 16) return true;
  return false;
}
export function validSheet(width, height, version) {
  return (version === 1 || version === 2) && width === 1536 && height === (version === 2 ? 2288 : 1872);
}
