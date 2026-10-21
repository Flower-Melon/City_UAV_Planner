'use strict';
const {spawn} = require('node:child_process');
const terminal = new Set(['completed', 'failed', 'cancelled']);

// 隔离飞行进程和 HTTP 生命周期；输入已校验规划，提供派发、状态与取消接口。
module.exports = function createMissionAPI({root, spawnProcess = spawn, timeoutMs = 15000}) {
  let worker, nextId = 0, buffer = '', mission = null, plan = null;
  let starting = false, uncertain = false, closed = false, unavailable = false;
  const pending = new Map();

  // 返回最近状态；尚未确认的派发保持锁定，禁止重复起飞。
  function snapshot() { return {execution: mission, plan, starting, uncertain}; }

  // 根据本地执行状态拒绝重叠任务；状态不明时只能先查询或停止。
  function assertIdle() {
    if (closed || unavailable) throw Error('执行服务已停止，请重启网页服务并核实无人机状态。');
    if (starting || uncertain || mission && !terminal.has(mission.status))
      throw Error('已有搜索任务正在执行或等待确认，请先查看进度或停止任务。');
  }

  // 启动唯一执行进程；标准输出仅接收协议，飞行日志走标准错误。
  function connect() {
    if (worker) return;
    worker = spawnProcess('python', ['-B', '-u', '-X', 'utf8', '-m', 'llm.functions.worker', '--execution-only'], {
      cwd: root, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
      env: {...process.env, PYTHONIOENCODING: 'utf-8'}
    });
    const child = worker;
    child.stdin.on('error', () => {});
    child.stderr.resume();
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', chunk => {
      if (unavailable) return;
      buffer += chunk;
      if (buffer.length > 1048576) { lost(); return; }
      let end;
      while ((end = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
        let message;
        try { message = JSON.parse(line); } catch { lost(); return; }
        const call = pending.get(message.id);
        if (!call) continue;
        pending.delete(message.id); clearTimeout(call.timer);
        if (message.result?.mission_id) {
          mission = message.result; starting = false; uncertain = false;
        }
        if (message.error) {
          if (call.operation === 'dispatch') { starting = false; uncertain = false; }
          call.reject(Error(typeof message.error === 'string' ? message.error : '执行请求失败。'));
        } else call.resolve(message.result);
      }
    });
    child.once('error', lost); child.once('close', lost);
  }

  // 管道异常不强杀控制进程；关闭输入让 Python 请求取消、等待飞行停止。
  function lost() {
    if (unavailable) return;
    unavailable = true; uncertain = starting || !!mission && !terminal.has(mission.status);
    worker?.stdin.end();
    for (const call of pending.values()) {
      clearTimeout(call.timer); call.reject(Error('执行进程连接中断，已请求停止；请核实 AirSim 状态。'));
    }
    pending.clear();
  }

  // 发送一条带 ID 的请求；超时不杀飞行进程，迟到结果仍用于恢复状态。
  function rpc(operation, data = {}) {
    if (closed || unavailable) return Promise.reject(Error('执行服务不可用，请核实无人机状态。'));
    connect();
    const id = ++nextId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        uncertain = true;
        if (operation !== 'dispatch') pending.delete(id);
        reject(Error('执行状态确认超时，请查看进度；任务可能已经开始，请勿重复提交。'));
      }, timeoutMs);
      pending.set(id, {resolve, reject, timer, operation});
      worker.stdin.write(JSON.stringify({id, operation, ...data}) + '\n');
    });
  }

  // 只接收服务器规划结果；本地执行器再次校验区域、顺序和实时可用状态。
  async function start(result, flight) {
    assertIdle(); starting = true; plan = result; mission = null;
    try { return await rpc('dispatch', {plan: result, flight}); }
    catch (error) { if (!uncertain) starting = false; throw error; }
  }

  // 查询当前进程的实际状态；无任务时不启动 Python，更不会触发飞行。
  async function status() {
    if (unavailable) throw Error('执行进程连接中断，已请求停止；请核实 AirSim 状态。');
    if (worker && (starting || uncertain || mission && !terminal.has(mission.status)))
      await rpc('status', mission ? {mission_id: mission.mission_id} : {});
    return snapshot();
  }

  // 写入 JSON 响应；页面离开不取消任务，刷新后可重新查询。
  function send(response, code, result) {
    if (response.destroyed || response.writableEnded) return;
    response.writeHead(code, {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store'});
    response.end(JSON.stringify(result));
  }

  // GET 查询、POST 取消；取消只接受当前任务 ID，不接受客户端飞行指令。
  async function handle(request, response) {
    const cancel = request.url.split('?')[0] === '/api/llm/cancel';
    if (request.method !== (cancel ? 'POST' : 'GET')) {
      response.setHeader('Allow', cancel ? 'POST' : 'GET'); send(response, 405, {error: '请求方法错误。'}); return;
    }
    if (request.headers.origin && request.headers.origin !== `http://${request.headers.host}`) {
      send(response, 403, {error: '请从本地地图页面操作任务。'}); return;
    }
    try {
      if (!cancel) { send(response, 200, await status()); return; }
      if (request.headers['content-type']?.split(';')[0].trim() !== 'application/json') {
        send(response, 415, {error: '请提交 JSON。'}); return;
      }
      const chunks = []; let size = 0;
      for await (const chunk of request) {
        size += chunk.length;
        if (size > 1024) { send(response, 413, {error: '请求过长。'}); return; }
        chunks.push(chunk);
      }
      let data;
      try { data = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
      catch { send(response, 400, {error: '无效 JSON。'}); return; }
      if (!mission || data?.mission_id !== mission.mission_id) {
        send(response, 409, {error: '任务已变化，请刷新执行进度。'}); return;
      }
      await rpc('cancel', {mission_id: mission.mission_id});
      send(response, 200, snapshot());
    } catch (error) { send(response, 503, {...snapshot(), error: error.message}); }
  }

  // 服务退出时关闭输入，由 Python 取消并等待停止；不强杀无人机控制进程。
  function close() { closed = true; worker?.stdin.end(); }
  return {start, status, assertIdle, handle, close};
};
