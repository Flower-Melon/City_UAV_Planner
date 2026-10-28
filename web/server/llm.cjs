'use strict';
// 网页任务流程统一入口：状态读取、模型规划、任务队列和进度查询。
const {spawn} = require('node:child_process');
const {randomUUID} = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const PythonRuntime = require('./python-runtime.cjs');
const TERMINAL = new Set(['completed', 'failed']);
const DEFAULT_FLIGHT = Object.freeze({flight_height: 40, flight_speed: 5});

function requestError(message, statusCode) {
  return Object.assign(new Error(message), {statusCode});
}

function positive(value, name) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0)
    throw requestError(`${name}必须为正的有限数值`, 400);
  return value;
}

function send(response, code, result) {
  if (response.destroyed || response.writableEnded) return;
  response.writeHead(code, {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store'});
  response.end(JSON.stringify(result));
}

function planningFleet(state, now = Date.now()) {
  if (!state?.connected || !Array.isArray(state.drones) || !state.drones.length ||
      state.drones.some(d => !Number.isFinite(d.updated_at) || now - d.updated_at > 1000 ||
        d.updated_at > now || typeof d.available !== 'boolean' ||
        !Array.isArray(d.position_m) || d.position_m.length !== 3 || !d.position_m.every(Number.isFinite))) return null;
  return state.drones.map(({name, position_m, available}) => ({name, position_m: [...position_m], available}));
}

// 管理一批搜索任务；同机按序执行，不同机并行。
class MissionExecutor {
  constructor({root, runtime, timeoutMs = 15000}) {
    this.root = root;
    this.runtime = runtime;
    this.timeoutMs = timeoutMs;
    this.mission = null;
    this.starting = false;
    this.uncertain = false;
    this.closed = false;
    this.runPromise = null;
  }

  assertIdle() {
    if (this.closed) throw requestError('执行服务已停止，请重启网页服务。', 503);
    if (this.starting || this.uncertain || this.mission && !TERMINAL.has(this.mission.status))
      throw requestError('已有搜索任务正在执行或等待确认，请先查看进度。', 409);
  }

  validate(plan) {
    if (!plan || plan.status !== 'ready' ||
        !Array.isArray(plan.commands) || !plan.commands.length)
      throw requestError('只接受通过校验的 ready 搜索规划。', 400);
    const map = JSON.parse(fs.readFileSync(path.join(this.root, 'Map', 'zones', 'functional_zones.json'), 'utf8').replace(/^\uFEFF/, ''));
    const zoneIds = new Set(map.features.map(feature => feature.id));
    const sequences = new Map(), targets = new Set();
    const tasks = plan.commands.map(command => {
      if (!command || typeof command !== 'object') throw requestError('搜索任务必须是对象。', 400);
      const name = command.drone_id, sequence = command.sequence, raw = command.target;
      if (typeof name !== 'string' || !name.trim() || name !== name.trim())
        throw requestError('drone_id 必须是实际无人机名称。', 400);
      if (!Number.isSafeInteger(sequence) || sequence < 1)
        throw requestError('sequence 必须为正整数。', 400);
      const numbers = sequences.get(name) || new Set();
      if (numbers.has(sequence)) throw requestError('同一无人机的 sequence 不能重复。', 400);
      numbers.add(sequence); sequences.set(name, numbers);
      if (!raw || typeof raw.zone_id !== 'string' || !zoneIds.has(raw.zone_id))
        throw requestError('target 必须包含真实的 zone_id。', 400);
      if (Object.keys(raw).some(key => key !== 'zone_id'))
        throw requestError('target 仅接受 zone_id，搜索按完整街区道路覆盖。', 400);
      const key = raw.zone_id;
      if (targets.has(key)) throw requestError('同一搜索范围不能重复派发。', 400);
      targets.add(key);
      return {drone_id: name, sequence, target: {zone_id: raw.zone_id}, status: 'queued'};
    });
    for (const numbers of sequences.values()) {
      if ([...numbers].some(number => number > numbers.size))
        throw requestError('同一无人机的 sequence 必须从 1 开始连续编号。', 400);
    }
    return tasks.sort((a, b) => a.drone_id < b.drone_id ? -1 : a.drone_id > b.drone_id ? 1 : a.sequence - b.sequence);
  }

  async start(plan, flight) {
    this.assertIdle(); this.starting = true;
    try {
      const settings = Object.fromEntries(Object.keys(DEFAULT_FLIGHT).map(key => [key, positive(flight[key], key)]));
      const tasks = this.validate(plan);
      const fleet = await this.runtime.request('fleet', {}, {channel: 'fleet', timeoutMs: this.timeoutMs});
      const available = new Set(Array.isArray(fleet) ? fleet.filter(drone => drone.available === true).map(drone => drone.name) : []);
      if (tasks.some(task => !available.has(task.drone_id)))
        throw requestError('规划中的无人机已离线或不可用，请重新规划。', 409);
      if (this.closed) throw requestError('执行服务已停止。', 503);
      this.mission = {mission_id: randomUUID().replaceAll('-', ''), status: 'running', executed: false, settings, tasks};
      this.runPromise = this.run();
      return this.status();
    } finally { this.starting = false; }
  }

  async run() {
    try {
      const queues = new Map();
      for (const task of this.mission.tasks) {
        if (!queues.has(task.drone_id)) queues.set(task.drone_id, []);
        queues.get(task.drone_id).push(task);
      }
      await Promise.all([...queues.values()].map(queue => this.fly(queue)));
    } catch (error) {
      this.mission.error_type = error.errorType || error.name;
    } finally {
      for (const task of this.mission.tasks) if (task.status === 'queued') task.status = 'skipped';
      this.mission.status = this.mission.error_type || this.mission.tasks.some(task => task.status !== 'completed') ? 'failed' : 'completed';
    }
  }

  async fly(queue) {
    let failed = false;
    for (const task of queue) {
      if (failed || this.closed) { task.status = 'skipped'; continue; }
      task.status = 'running'; this.mission.executed = true;
      try {
        const result = await this.runtime.request('search', {
          drone_id: task.drone_id, target: structuredClone(task.target), flight: {...this.mission.settings}
        }, {channel: `drone:${task.drone_id}`, timeoutMs: 0});
        if (result?.completed !== true) throw Object.assign(Error('搜索进程未确认任务完成。'), {uncertain: true});
        task.status = 'completed';
      } catch (error) {
        Object.assign(task, {status: 'failed', error_type: error.errorType || error.name, error: error.message});
        this.uncertain ||= !!error.uncertain;
        failed = true;
      }
    }
  }

  status() { return this.mission && structuredClone(this.mission); }
  close() { this.closed = true; }
}

// 网页接口只在此组织“取状态 → 规划 → 派发”，进度直接来自任务对象。
class LlmController {
  constructor({root, getState = () => null, spawnProcess = spawn,
    timeoutMs = 660000, executionTimeoutMs = 15000, runtime = null}) {
    this.runtime = runtime || new PythonRuntime({root, spawnProcess});
    this.executor = new MissionExecutor({root, runtime: this.runtime, timeoutMs: executionTimeoutMs});
    this.getState = getState;
    this.timeoutMs = timeoutMs;
    this.executionTimeoutMs = executionTimeoutMs;
    this.plan = null;
    this.planning = false;
    this.closed = false;
  }

  start() { this.runtime.start(); }

  snapshot() {
    return {execution: this.executor.status(), plan: this.plan && structuredClone(this.plan),
      starting: this.executor.starting, uncertain: this.executor.uncertain};
  }

  validate(data) {
    const instruction = data?.instruction, conversation = data?.conversation ?? [];
    if (typeof instruction !== 'string' || !instruction.trim() || instruction.length > 4000 || instruction.includes('\0'))
      throw requestError('请输入 1–4000 字的任务描述。', 400);
    if (!Array.isArray(conversation) || conversation.length > 12 || conversation.length % 2 ||
        conversation.some((message, i) => !message || typeof message !== 'object' ||
          Object.keys(message).sort().join(',') !== 'content,role' || message.role !== (i % 2 ? 'assistant' : 'user') ||
          typeof message.content !== 'string' || !message.content.trim() || message.content.includes('\0')) ||
        instruction.length + conversation.reduce((n, message) => n + message.content.length, 0) > 16000)
      throw requestError('对话记录无效或过长，请开始新任务。', 400);
    const flight = {...DEFAULT_FLIGHT, flight_height: positive(data.flight?.flight_height ?? DEFAULT_FLIGHT.flight_height, '飞行高度')};
    return {instruction: instruction.trim(), conversation, flight};
  }

  handle(request, response, url) {
    if (url.pathname === '/api/llm/plan') return this.handlePlan(request, response);
    if (url.pathname === '/api/llm/mission') return this.handleMission(request, response);
    send(response, 404, {error: '接口不存在'});
  }

  async handlePlan(request, response) {
    if (request.method !== 'POST') {
      response.setHeader('Allow', 'POST'); send(response, 405, {error: '请使用 POST 提交任务。'}); return;
    }
    if (request.headers.origin && request.headers.origin !== `http://${request.headers.host}`) {
      send(response, 403, {error: '请从本地地图页面提交任务。'}); return;
    }
    if (request.headers['content-type']?.split(';')[0].trim().toLowerCase() !== 'application/json') {
      send(response, 415, {error: '任务必须使用 JSON 格式提交。'}); return;
    }
    try {
      if (this.planning) throw requestError('已有任务正在规划，请等待完成。', 409);
      this.executor.assertIdle();
    } catch (error) { send(response, error.statusCode || 409, {error: error.message}); return; }
    this.planning = true;
    let cancelled = false, dispatching = false, phase = 'input';
    const disconnected = () => {
      if (!response.writableEnded && !dispatching) {
        cancelled = true; this.runtime.cancel('planning'); this.runtime.cancel('fleet');
      }
    };
    response.once('close', disconnected);
    try {
      const chunks = []; let size = 0;
      for await (const chunk of request) {
        size += chunk.length;
        if (size > 65536) throw requestError('任务内容过长。', 413);
        chunks.push(chunk);
      }
      let data;
      try { data = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
      catch { throw requestError('无法读取任务，请提交有效 JSON。', 400); }
      const {instruction, conversation, flight} = this.validate(data);
      if (cancelled || response.destroyed || this.closed) return;
      phase = 'fleet';
      const fleet = planningFleet(this.getState()) || await this.runtime.request('fleet', {},
        {channel: 'fleet', timeoutMs: this.executionTimeoutMs});
      if (cancelled || this.closed) return;
      phase = 'plan';
      const result = await this.runtime.request('plan', {instruction, conversation, fleet},
        {channel: 'planning', timeoutMs: this.timeoutMs});
      if (cancelled || this.closed) return;
      if (!result || !['ready', 'needs_clarification'].includes(result.status) ||
          typeof result.message !== 'string' || !result.message.trim() ||
          !Array.isArray(result.commands) || (result.status === 'ready') !== (result.commands.length > 0))
        throw Error('规划进程返回格式异常，请重试。');
      if (result.status === 'ready') {
        phase = 'dispatch'; dispatching = true; this.plan = structuredClone(result);
        const execution = await this.executor.start(result, flight);
        send(response, 200, {...result, execution});
      } else send(response, 200, result);
    } catch (error) {
      send(response, error.statusCode || (phase === 'plan' ? 502 : 503), {error: error.message});
    } finally {
      this.planning = false;
      response.removeListener('close', disconnected);
    }
  }

  handleMission(request, response) {
    if (request.method !== 'GET') {
      response.setHeader('Allow', 'GET'); send(response, 405, {error: '请求方法错误。'}); return;
    }
    if (request.headers.origin && request.headers.origin !== `http://${request.headers.host}`) {
      send(response, 403, {error: '请从本地地图页面操作任务。'}); return;
    }
    send(response, this.closed ? 503 : 200, {...this.snapshot(), ...(this.closed ? {error: '执行服务已停止。'} : {})});
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    this.executor.close();
    this.runtime.close();
  }
}

module.exports = LlmController;
module.exports.MissionExecutor = MissionExecutor;
