// Import a character sheet folder (create-pet / Codex pet output: pet.json + spritesheet.webp).
// Only the manifest and one WebP are read; nothing in the folder is executed. A failed import leaves the
// installed characters untouched: files are staged, then swapped in, then the list is rewritten atomically.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const SIZES = {'1536x2288': 2, '1536x1872': 1};  // sprite.js validSheet()

// Dimensions from the WebP header (lossy VP8, lossless VP8L or extended VP8X).
function webpSize(buf) {
  if (buf.length < 30 || buf.toString('ascii', 0, 4) !== 'RIFF' || buf.toString('ascii', 8, 12) !== 'WEBP')
    throw Error('not a WebP image');
  const chunk = buf.toString('ascii', 12, 16);
  if (chunk === 'VP8 ') return {width: buf.readUInt16LE(26) & 0x3fff, height: buf.readUInt16LE(28) & 0x3fff};
  if (chunk === 'VP8L') {
    const b = buf.readUInt32LE(21);
    return {width: (b & 0x3fff) + 1, height: ((b >>> 14) & 0x3fff) + 1};
  }
  if (chunk === 'VP8X') return {width: buf.readUIntLE(24, 3) + 1, height: buf.readUIntLE(27, 3) + 1};
  throw Error('unsupported WebP layout');
}

function readLimited(file, limit, label) {
  const size = fs.statSync(file).size;
  if (size > limit) throw Error(`${label} is larger than ${limit} bytes`);
  return fs.readFileSync(file);
}

function writeAtomic(file, data) {
  const tmp = file + '.tmp';
  fs.writeFileSync(tmp, data);
  fs.renameSync(tmp, file);
}

function installPet(source, petsDir, builtInIds) {
  let manifest;
  try {manifest = JSON.parse(readLimited(path.join(source, 'pet.json'), 65536, 'pet.json').toString('utf8'));}
  catch (e) {throw Error(e.code === 'ENOENT' ? 'pet.json not found in the folder' : 'pet.json: ' + e.message);}
  const id = manifest.id;
  if (typeof id !== 'string' || !/^[a-z0-9][a-z0-9_-]{0,79}$/.test(id)) throw Error('pet.json id must be lowercase letters, digits, - or _');
  if (builtInIds.includes(id)) throw Error(`"${id}" is a built-in character; change the id to import it separately`);
  const sheetName = manifest.spritesheetPath || 'spritesheet.webp';
  if (typeof sheetName !== 'string' || path.basename(sheetName) !== sheetName || !sheetName.endsWith('.webp'))
    throw Error('spritesheetPath must be a .webp file next to pet.json');
  const sheet = readLimited(path.join(source, sheetName), 20 * 1024 * 1024, 'sprite sheet');
  const {width, height} = webpSize(sheet);
  const version = SIZES[`${width}x${height}`];
  if (!version) throw Error(`sheet is ${width}x${height}; expected 1536x2288 (V2) or 1536x1872 (V1)`);

  fs.mkdirSync(petsDir, {recursive: true});
  const target = path.join(petsDir, id), staging = target + '.staging', old = target + '.old';
  fs.rmSync(staging, {recursive: true, force: true});
  fs.mkdirSync(staging);
  fs.writeFileSync(path.join(staging, 'spritesheet.webp'), sheet);
  fs.writeFileSync(path.join(staging, 'pet.json'), JSON.stringify(manifest));
  fs.rmSync(old, {recursive: true, force: true});
  if (fs.existsSync(target)) fs.renameSync(target, old);
  fs.renameSync(staging, target);
  fs.rmSync(old, {recursive: true, force: true});

  const listFile = path.join(petsDir, 'catalog.json');
  let list = [];
  try {list = JSON.parse(fs.readFileSync(listFile, 'utf8'));} catch {}  // missing or damaged: rebuilt below
  if (!Array.isArray(list)) list = [];
  const entry = {id, name: String(manifest.displayName || id).slice(0, 80), version, author: String(manifest.author || '').slice(0, 80),
    source: 'imported', sheet: `pets/${id}/spritesheet.webp`, sha256: crypto.createHash('sha256').update(sheet).digest('hex')};
  writeAtomic(listFile, JSON.stringify([...list.filter(p => p && p.id !== id), entry], null, 2));
  return entry;
}

module.exports = {installPet, webpSize};
