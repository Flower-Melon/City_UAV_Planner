'use strict';
const PythonRuntime = require('./python-runtime.cjs');

function sendJSON(response, status, data) {
  if (response.destroyed || response.writableEnded) return;
  const body = Buffer.from(JSON.stringify(data));
  response.writeHead(status, {'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': body.length, 'Cache-Control': 'no-store'});
  response.end(body);
}

const abnormalKinds = new Set(['vehicle', 'armed_person', 'patient']);
const abnormalBehaviors = new Set(['cruise', 'surge', 'weave', 'brake', 'wrong_way', 'mixed', 'stop']);
const validAbnormalId = value => typeof value === 'string' && /^[a-zA-Z0-9_-]{1,100}$/.test(value);
const abnormalError = message => Object.assign(Error(message), {statusCode: 400});

async function readAbnormalBody(request) {
  if (request.headers.origin && request.headers.origin !== `http://${request.headers.host}`)
    throw abnormalError('请求来源不匹配');
  if (!(request.headers['content-type'] || '').startsWith('application/json')) throw abnormalError('需要 JSON 请求');
  let content = '';
  for await (const chunk of request) {
    content += chunk;
    if (Buffer.byteLength(content) > 8192) throw abnormalError('请求过大');
  }
  try {
    const value = JSON.parse(content);
    if (!value || Array.isArray(value) || typeof value !== 'object') throw Error();
    return value;
  } catch { throw abnormalError('请求必须是 JSON 对象'); }
}

module.exports = class AbnormalController {
  constructor({root}) {
    this.runtime = new PythonRuntime({root});
    this.jobs = new Map();
    this.queue = Promise.resolve();
    this.reading = null;
    this.frames = new Map();
  }

  job(operation, data) {
    if (!validAbnormalId(data.request_id)) throw abnormalError('无效的请求编号');
    const signature = JSON.stringify([operation, data]);
    const previous = this.jobs.get(data.request_id);
    if (previous) {
      if (previous.signature !== signature) throw abnormalError('请求编号已用于其他操作');
      return previous;
    }
    // 单一写入通道保留未结束任务；只清理旧的已结束任务。
    for (const [id, item] of this.jobs) {
      if (item.status !== 'running' && Date.now() - item.created_at > 86400000) this.jobs.delete(id);
    }
    const job = {id: data.request_id, status: 'running', created_at: Date.now(), signature};
    this.jobs.set(job.id, job);
    this.queue = this.queue.then(async () => {
      try {
        job.result = await this.runtime.request(operation, data, {channel: 'abnormal-actions', timeoutMs: 0});
        job.status = 'completed';
      } catch (error) { job.error = error.message; job.status = 'failed'; }
    });
    return job;
  }

  async handle(request, response, url) {
    try {
      const route = url.pathname.slice('/api/abnormal/'.length);
      if (request.method === 'POST' && ['spawn', 'behavior', 'remove'].includes(route)) {
        const data = await readAbnormalBody(request);
        if (route === 'spawn') {
          if (!abnormalKinds.has(data.kind)) throw abnormalError('请选择异常类型');
          if (!Array.isArray(data.position) || data.position.length !== 2 ||
              !data.position.every(value => typeof value === 'number' && Number.isFinite(value)) ||
              data.position[0] < -1000 || data.position[0] > 3000 || data.position[1] < -3000 || data.position[1] > 1000)
            throw abnormalError('请点击地图范围内的位置');
        } else if (!validAbnormalId(data.target_id)) throw abnormalError('无效的目标编号');
        if ((route === 'behavior' || data.kind === 'vehicle') && !abnormalBehaviors.has(data.behavior)) throw abnormalError('无效的车辆行为');
        const job = this.job(`abnormal_${route}`, data);
        sendJSON(response, 202, this.publicJob(job)); return;
      }
      if (request.method !== 'GET') { sendJSON(response, 405, {error: '不支持的请求方法'}); return; }
      if (route.startsWith('jobs/')) {
        const job = this.jobs.get(route.slice(5));
        sendJSON(response, job ? 200 : 404, job ? this.publicJob(job) : {error: '未找到操作记录，请核实地图上的已有目标'}); return;
      }
      const target_id = url.searchParams.get('target') || null;
      if (target_id !== null && !validAbnormalId(target_id)) throw abnormalError('无效的目标编号');
      if (route === 'state') {
        // 合并同一时刻的状态请求；操作与观测使用不同 worker。
        if (!this.reading) {
          const task = this.runtime.request('abnormal_state', {target_id, watch: url.searchParams.get('watch') === '1'},
            {channel: 'abnormal-monitor', timeoutMs: 0});
          this.reading = task;
          task.finally(() => { if (this.reading === task) this.reading = null; }).catch(() => {});
        }
        sendJSON(response, 200, await this.reading); return;
      }
      if (route === 'camera') {
        if (!target_id) throw abnormalError('请选择监控目标');
        if (!this.frames.has(target_id)) {
          const task = this.runtime.request('abnormal_frame', {target_id}, {channel: 'abnormal-camera', timeoutMs: 0});
          this.frames.set(target_id, task);
          task.finally(() => { if (this.frames.get(target_id) === task) this.frames.delete(target_id); }).catch(() => {});
        }
        const result = await this.frames.get(target_id);
        const data = Buffer.from(result.data, 'base64');
        response.writeHead(200, {'Content-Type': 'image/jpeg', 'Content-Length': data.length,
          'Cache-Control': 'no-store', 'X-Simulation-Session': result.session});
        response.end(data); return;
      }
      sendJSON(response, 404, {error: '接口不存在'});
    } catch (error) { sendJSON(response, error.statusCode || 503, {error: error.message}); }
  }

  publicJob({signature, ...job}) { return job; }
  close() { this.runtime.close(); }
};
