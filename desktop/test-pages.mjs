// Page scripts load as browser modules; a syntax error leaves a page blank with no test noticing (the record
// view's expansion tab once did). node --check parses each one as a module (package.json "type": "module").
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {readdirSync} from 'node:fs';

const here = import.meta.dirname;
for (const file of readdirSync(here).filter(f => f.endsWith('.js'))) {
  test(`page script parses: ${file}`, () => {
    const result = spawnSync(process.execPath, ['--check', file], {cwd: here, encoding: 'utf8'});
    assert.equal(result.status, 0, result.stderr);
  });
}
