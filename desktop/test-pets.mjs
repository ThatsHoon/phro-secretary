import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {createRequire} from 'node:module';
const {installPet, webpSize} = createRequire(import.meta.url)('./pets.cjs');

// Minimal extended-WebP header: enough for webpSize(); the renderer decodes the real image.
function webp(width, height) {
  const b = Buffer.alloc(30);
  b.write('RIFF', 0); b.writeUInt32LE(22, 4); b.write('WEBPVP8X', 8); b.writeUInt32LE(10, 16);
  b.writeUIntLE(width - 1, 24, 3); b.writeUIntLE(height - 1, 27, 3);
  return b;
}
function kit(dir, manifest, sheet = webp(1536, 2288), name = 'spritesheet.webp') {
  mkdirSync(dir, {recursive: true});
  writeFileSync(path.join(dir, 'pet.json'), JSON.stringify(manifest));
  writeFileSync(path.join(dir, name), sheet);
  return dir;
}

test('webp dimensions for the three layouts', () => {
  assert.deepEqual(webpSize(webp(1536, 1872)), {width: 1536, height: 1872});
  const lossless = Buffer.alloc(30); lossless.write('RIFF', 0); lossless.write('WEBPVP8L', 8);
  lossless.writeUInt32LE((1535) | (2287 << 14), 21);
  assert.deepEqual(webpSize(lossless), {width: 1536, height: 2288});
  assert.throws(() => webpSize(Buffer.from('not an image at all, long enough....')));
});

test('imports, replaces and refuses bad kits without touching installed ones', () => {
  const tmp = mkdtempSync(path.join(tmpdir(), 'pets-')), pets = path.join(tmp, 'installed');
  try {
    const entry = installPet(kit(path.join(tmp, 'a'), {id: 'mini', displayName: 'Mini'}), pets, ['clawd']);
    assert.equal(entry.version, 2);
    assert.deepEqual(JSON.parse(readFileSync(path.join(pets, 'catalog.json'))).map(p => p.id), ['mini']);
    installPet(kit(path.join(tmp, 'b'), {id: 'mini', displayName: 'Mini 2'}, webp(1536, 1872)), pets, []);
    const list = JSON.parse(readFileSync(path.join(pets, 'catalog.json')));
    assert.deepEqual(list.map(p => [p.id, p.name, p.version]), [['mini', 'Mini 2', 1]]);
    const bad = [
      [{id: 'clawd'}, undefined, /built-in/],
      [{id: '../x'}, undefined, /id must/],
      [{id: 'evil', spritesheetPath: '../../secret.webp'}, undefined, /next to pet.json/],
      [{id: 'tiny'}, webp(512, 512), /expected 1536x2288/],
    ];
    for (const [manifest, sheet, message] of bad)
      assert.throws(() => installPet(kit(path.join(tmp, 'bad-' + manifest.id.replace(/\W/g, '')), manifest, sheet), pets, ['clawd']), message);
    assert.throws(() => installPet(path.join(tmp, 'missing'), pets, []), /pet.json not found/);
    assert.equal(JSON.parse(readFileSync(path.join(pets, 'catalog.json'))).length, 1);
    assert.ok(existsSync(path.join(pets, 'mini', 'spritesheet.webp')) && !existsSync(path.join(pets, 'mini.staging')));
  } finally {rmSync(tmp, {recursive: true, force: true});}
});
