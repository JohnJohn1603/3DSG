const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync('scripts/causal_timeline_viewer.html', 'utf8');
const start = html.indexOf('function assertT4OverlayBinding(');
const end = html.indexOf('function renderDetails(', start);
assert(start >= 0 && end > start, 'T4 overlay helpers are present in the shared D2 viewer');
const helpers = html.slice(start, end) + '\nthis.testApi={assertT4OverlayBinding,mergeT4Overlay};';
const sandbox = {};
vm.runInNewContext(helpers, sandbox, {filename: 'causal_timeline_viewer.html#t4-overlay-contract'});
const {assertT4OverlayBinding, mergeT4Overlay} = sandbox.testApi;

const frame = {revision: 2, frame_index: 7, frame_id: 'frame-7', unified_sha256: 'a'.repeat(64),
  object_source_revision: 13, room_source_revision: 9};
const baseGraph = {graph_revision_id: 'unified-000002', object_revision: 13, room_revision: 9,
  rooms: [{node_id: 'room:1'}], objects: [{hypothesis_id: 'obj:1', causal_object_id: 'obj:1'}],
  edges: [{relation: 'nearby', source_id: 'a', target_id: 'b'}]};
const overlay = {schema: 't4-room-belief-semantic-overlay/v1', status: 'complete',
  graph_revision_id: 'unified-000002', graph_sha256: 'a'.repeat(64), unified_revision: 2,
  frame_index: 7, frame_id: 'frame-7', object_revision: 13, room_revision: 9,
  scope_status: 'unavailable', scope_policy: {mode: 'verified_only'},
  room_semantics_availability: {status: 'not_scored'}, room_diagnostics: {mean_no_neutral: {'room:1': {not_scored: true}}},
  room_contains_edges: [], rooms: {'mean_no_neutral': {'room:1': {}}},
  objects: [{object_id: 'obj:1', hypothesis_id: 'obj:1', causal_object_id: 'obj:1',
    semantic_consensus: {status: 'provisional'}, membership: {status: 'unavailable'}}]};

assert.strictEqual(assertT4OverlayBinding(frame, baseGraph, overlay, frame.unified_sha256), overlay);
const rendered = mergeT4Overlay(baseGraph, overlay);
assert.strictEqual(rendered.rooms[0].node_id, 'room:1', 'semantic overlay cannot replace room geometry source');
assert.strictEqual(rendered.edges[0].relation, 'nearby', 'structural graph edges are retained');
assert.strictEqual(rendered.objects[0].semantic_consensus.status, 'provisional');
assert.throws(() => assertT4OverlayBinding(frame, baseGraph,
  {...overlay, graph_sha256: 'b'.repeat(64)}, frame.unified_sha256), /graph hash/);
assert.throws(() => assertT4OverlayBinding(frame, baseGraph,
  {...overlay, room_revision: 8}, frame.unified_sha256), /source revisions/);

const failed = {...overlay, status: 'failed', objects: [], room_contains_edges: [],
  room_diagnostics: {mean_no_neutral: {'room:1': {committed_label: null}}},
  room_semantics_availability: {status: 'failed', reason: 'fixture'},
  rooms: {'mean_no_neutral': {'room:1': {committed_label: null}}}};
assert.doesNotThrow(() => assertT4OverlayBinding(frame, baseGraph, failed, frame.unified_sha256));
const failedGraph = mergeT4Overlay(baseGraph, failed);
assert.strictEqual(failedGraph.objects[0].semantic_consensus.reason, 'semantic_update_failed',
  'failure does not reuse stale semantic state');
assert.deepStrictEqual(failedGraph.room_contains_edges, []);

console.log('T4 live viewer overlay binding/merge tests passed');
