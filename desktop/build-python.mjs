// Stages the Python runtime the installer ships: the official embeddable CPython plus the backend's
// runtime packages (requirements.txt minus test-only ones). Run by `npm run dist` before electron-builder.
// Output: desktop/build/python (git-ignored). Needs the dev .venv (same CPython minor) for pip.
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {existsSync, mkdirSync, readFileSync, rmSync, writeFileSync} from 'node:fs';
import path from 'node:path';

const VERSION = '3.12.10';
const URL = `https://www.python.org/ftp/python/${VERSION}/python-${VERSION}-embed-amd64.zip`;
// Pinned 2026-10-05; matches the MD5 python.org publishes for this file (fe8ef205...). A changed file fails the build.
const SHA256 = '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3';
const DEV_ONLY = new Set(['pytest']);

const here = import.meta.dirname, root = path.resolve(here, '..');
const out = path.join(here, 'build', 'python'), zip = path.join(here, 'build', `python-${VERSION}-embed-amd64.zip`);
mkdirSync(path.dirname(out), {recursive: true});
if (!existsSync(zip)) {
  const response = await fetch(URL);
  if (!response.ok) throw Error(`download failed: ${response.status}`);
  writeFileSync(zip, Buffer.from(await response.arrayBuffer()));
}
const digest = createHash('sha256').update(readFileSync(zip)).digest('hex');
if (digest !== SHA256) {
  rmSync(zip);
  throw Error(`python embed sha256 ${digest} does not match the pinned ${SHA256}`);
}
rmSync(out, {recursive: true, force: true});
execFileSync('powershell', ['-NoProfile', '-Command', `Expand-Archive -LiteralPath '${zip}' -DestinationPath '${out}'`]);
// The embeddable build ignores PYTHONPATH and site-packages unless its ._pth file lists them.
const pth = path.join(out, `python${VERSION.split('.').slice(0, 2).join('')}._pth`);
writeFileSync(pth, readFileSync(pth, 'utf8').replace('#import site', 'import site') + 'Lib\\site-packages\n');
const wanted = readFileSync(path.join(root, 'requirements.txt'), 'utf8').split(/\r?\n/)
  .map(line => line.trim()).filter(line => line && !line.startsWith('#') && !DEV_ONLY.has(line.split(/[=<>\[]/)[0]));
execFileSync(path.join(root, '.venv', 'Scripts', 'python.exe'),
  ['-m', 'pip', 'install', '--disable-pip-version-check', '--no-compile', '--only-binary=:all:',
   '--python-version', VERSION, '--platform', 'win_amd64', '--implementation', 'cp',
   '--target', path.join(out, 'Lib', 'site-packages'), ...wanted], {stdio: 'inherit'});
// pip's console-script launchers are unused by the app and embed this build machine's python path.
rmSync(path.join(out, 'Lib', 'site-packages', 'bin'), {recursive: true, force: true});
console.log('staged', out, wanted.join(' '));
