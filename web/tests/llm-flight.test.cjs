'use strict';
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const {Readable} = require('node:stream');
const {test} = require('node:test');
const LlmController = require('../server/llm.cjs');

const root = path.resolve(__dirname, '../..');
const defaults = {max_height: 60, flight_speed: 30, flight_height: 15};
const custom = {max_height: 90, flight_speed: 12.5, flight_height: 25};

test('flight settings preserve defaults, partial requests and all three custom values', () => {
  const controller = new LlmController({root, runtime: {}});
  assert.deepEqual(controller.validate({instruction: '搜索学校'}).flight, defaults);
  assert.deepEqual(controller.validate({instruction: '搜索学校', flight: {flight_height: 20}}).flight,
    {...defaults, flight_height: 20});
  assert.deepEqual(controller.validate({instruction: '搜索学校', flight: custom}).flight, custom);
});

test('invalid flight settings fail before planning or dispatch', () => {
  const controller = new LlmController({root, runtime: {}});
  for (const flight of [null, [], 'invalid', {max_height: 14.9},
    ...['max_height', 'flight_speed', 'flight_height'].flatMap(key =>
      [0, -1, Infinity, NaN, '30', true].map(value => ({[key]: value})))]) {
    assert.throws(() => controller.validate({instruction: '搜索学校', flight}),
      error => error.statusCode === 400);
  }
  assert.equal(controller.validate({instruction: '搜索学校', flight: {max_height: 15}}).flight.max_height, 15);
});

test('HTTP planning passes flight settings through mission queues to each Python search request', async () => {
  const fixture = fs.mkdtempSync(path.join(root, '.flight-test-'));
  assert.equal(path.dirname(fs.realpathSync(fixture)), fs.realpathSync(root));
  try {
    fs.mkdirSync(path.join(fixture, 'Map/zones'), {recursive: true});
    fs.writeFileSync(path.join(fixture, 'Map/zones/functional_zones.json'),
      JSON.stringify({features: [{id: 'zone-1'}, {id: 'zone-2'}]}));
    for (const number of ['000001', '000002'])
      fs.mkdirSync(path.join(fixture, 'output', number, 'capture'), {recursive: true});
    const commands = ['zone-1', 'zone-2'].map((zone_id, index) =>
      ({drone_id: 'UAV_01', sequence: index + 1, target: {zone_id}}));
    const searches = [];
    const runtime = {request: async (operation, data) => {
      if (operation === 'fleet') return [{name: 'UAV_01', available: true, position_m: [0, 0, 0]}];
      if (operation === 'plan') {
        assert.equal(data.instruction, '巡查两个街区');
        assert.equal(Object.hasOwn(data, 'flight'), false);
        return {status: 'ready', message: '已规划。', commands};
      }
      assert.equal(operation, 'search');
      searches.push(structuredClone(data));
      return {completed: true, capture_directory: `output/${String(searches.length).padStart(6, '0')}/capture`};
    }};
    const controller = new LlmController({root: fixture, runtime});
    controller.executor.analyzeTask = async () => {};
    const request = Readable.from([Buffer.from(JSON.stringify({instruction: '巡查两个街区', flight: custom}))]);
    request.method = 'POST';
    request.headers = {'content-type': 'application/json', host: '127.0.0.1'};
    const response = Object.assign(new EventEmitter(), {
      writeHead(code) { this.code = code; },
      end(body) { this.body = JSON.parse(body); this.writableEnded = true; }
    });
    await controller.handlePlan(request, response);
    await controller.executor.runPromise;
    assert.equal(response.code, 200);
    assert.deepEqual(response.body.execution.settings, custom);
    assert.equal(searches.length, 2);
    searches.forEach((search, index) => assert.deepEqual(search, {
      drone_id: 'UAV_01', target: {zone_id: `zone-${index + 1}`}, flight: custom
    }));
    assert.equal(controller.snapshot().execution.status, 'completed');
    assert.deepEqual(controller.snapshot().execution.settings, custom);
  } finally {
    fs.rmSync(fixture, {recursive: true, force: true});
  }
});
