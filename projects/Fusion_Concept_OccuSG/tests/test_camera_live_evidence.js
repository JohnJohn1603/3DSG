const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('scripts/causal_timeline_viewer.html', 'utf8');
const start = html.indexOf('function liveEvidenceRows(');
const end = html.indexOf('function renderEvidence(', start);
assert(start >= 0 && end > start, 'live evidence resolver exists in shared viewer');
const source = 'let LIVE_EVIDENCE_ROWS={};\n' + html.slice(start, end) +
  '\nthis.testApi={liveEvidenceRows};';
const rgbHash = 'a'.repeat(64), maskHash = 'b'.repeat(64);
const frame = {frame_id: 'run-frame-0', frame_index: 0, source_hashes: {'rgb.npy': rgbHash},
  live_evidence: {schema: 'camera-live-frame-evidence/v1', run_id: 'pilot-1',
    scene_id: 'scene-1', sequence_id: 'route-1', frame_id: 'run-frame-0', frame_index: 0,
    source_hashes: {'rgb.npy': rgbHash}, camera_rgb: {source_sha256: rgbHash},
    observations: [{observation_id: 'run-frame-0:det-0000', frame_id: 'run-frame-0', frame_index: 0,
      source_hashes: {'rgb.npy': rgbHash, 'mask.npy': maskHash}}]}};
const sandbox = {LIVE_MODE: true, EVIDENCE: {observations: {'old-c2-observation': {frame_index: 0}}},
  INDEX: {run_id: 'pilot-1', timeline_manifest: {scene_id: 'scene-1', sequence_id: 'route-1'}}};
vm.runInNewContext(source, sandbox, {filename: 'causal_timeline_viewer.html#live-evidence'});
const rows = sandbox.testApi.liveEvidenceRows(frame);
assert.equal(rows.length, 1);
assert.equal(rows[0][0], 'run-frame-0:det-0000', 'live mode uses only the current frame bundle');
assert.throws(() => sandbox.testApi.liveEvidenceRows({...frame,
  live_evidence: {...frame.live_evidence, run_id: 'another-run'}}), /identity mismatch/);
assert.throws(() => sandbox.testApi.liveEvidenceRows({...frame,
  source_hashes: {'rgb.npy': 'c'.repeat(64)}}), /source hash differs/);
console.log('camera-live evidence binding tests passed: current run/frame only; stale cache ignored');
