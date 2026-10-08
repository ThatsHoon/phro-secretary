import {test} from 'node:test';
import assert from 'node:assert/strict';
import {STATES,frame,validSheet,reaction,lookDirection} from './sprite.js';
test('emotions map to existing rows with a fallback',()=>{
  for(const e of ['neutral','happy','laugh','surprised','sad','angry','thinking','embarrassed',undefined,'excited'])assert.ok(STATES.includes(reaction(e)));
  assert.equal(reaction('unknown'),'waving');
});
test('look stays in the front-facing arc',()=>{
  assert.equal(lookDirection(0),0);assert.equal(lookDirection(120),45);assert.equal(lookDirection(9999),90);
  assert.equal(lookDirection(-120),315);assert.equal(lookDirection(-9999),270);
});
test('all animation frames stay within their populated rows',()=>{
  const counts=[6,8,8,4,5,8,6,6,6];
  STATES.forEach((s,row)=>{for(let t=0;t<2000;t+=125){const f=frame(s,t);assert.equal(f.y,row*208);assert.ok(f.x>=0&&f.x<counts[row]*192);}});
});
test('v2 directions span both rows; v1 falls back to idle',()=>{
  assert.deepEqual(frame('look',0,2,180),{x:0,y:2080});
  assert.deepEqual(frame('look',0,2,337.5),{x:1344,y:2080});
  assert.deepEqual(frame('look',0,1,180),{x:0,y:0});
  assert.equal(validSheet(1536,2288,2),true);assert.equal(validSheet(1536,1872,1),true);
  assert.equal(validSheet(1536,1872,2),false);
});
