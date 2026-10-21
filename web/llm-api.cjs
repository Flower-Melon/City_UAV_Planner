'use strict';
// 网页规划适配层；可将 ready 结果交给独立执行器，不让 HTTP 请求管理飞行生命周期。
const {spawn} = require('node:child_process');

module.exports = function createLlmAPI({root, spawnProcess = spawn, timeoutMs = 660000, getFleet = () => null,
  assertIdle = () => {}, onReady = null}) {
  let active = null;

  function send(response, status, data) {
    if (response.destroyed || response.writableEnded) return;
    const body = Buffer.from(JSON.stringify(data));
    response.writeHead(status, {'Content-Type': 'application/json; charset=utf-8',
      'Content-Length': body.length, 'Cache-Control': 'no-store'});
    response.end(body);
  }

  async function handle(request, response) {
    if (request.method !== 'POST') {
      response.setHeader('Allow', 'POST'); send(response, 405, {error: '请使用 POST 提交任务。'}); return;
    }
    if (request.headers.origin && request.headers.origin !== `http://${request.headers.host}`) {
      send(response, 403, {error: '请从本地地图页面提交任务。'}); return;
    }
    if (request.headers['content-type']?.split(';')[0].trim().toLowerCase() !== 'application/json') {
      send(response, 415, {error: '任务必须使用 JSON 格式提交。'}); return;
    }
    if (active) { send(response, 409, {error: '已有任务正在规划，请等待完成后再提交。'}); return; }
    let instruction, conversation, flight;
    try {
      const chunks = []; let size = 0;
      for await (const chunk of request) {
        size += chunk.length;
        if (size > 65536) { send(response, 413, {error: '任务内容过长。'}); request.resume(); return; }
        chunks.push(chunk);
      }
      const data = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      if (!data || typeof data.instruction !== 'string' || !data.instruction.trim() ||
          data.instruction.length > 4000 || data.instruction.includes('\0')) {
        send(response, 400, {error: '请输入 1–4000 字的任务描述。'}); return;
      }
      instruction = data.instruction.trim();
      if (onReady) {
        const height = data.flight?.flight_height ?? 40;
        if (typeof height !== 'number' || !Number.isFinite(height) || height <= 0) {
          send(response, 400, {error: '飞行高度必须为正的有限数值。'}); return;
        }
        flight = {flight_height: height, flight_speed: 5, capture_interval: 3};
      }
      conversation = data.conversation ?? [];
      if (!Array.isArray(conversation) || conversation.length > 12 || conversation.length % 2 ||
          conversation.some((message, i) => !message || typeof message !== 'object' ||
            Object.keys(message).sort().join(',') !== 'content,role' ||
            message.role !== (i % 2 ? 'assistant' : 'user') || typeof message.content !== 'string' ||
            !message.content.trim() || message.content.includes('\0')) ||
          instruction.length + conversation.reduce((n, m) => n + m.content.length, 0) > 16000) {
        send(response, 400, {error: '对话记录无效或过长，请开始新任务。'}); return;
      }
    } catch {
      send(response, 400, {error: '无法读取任务，请提交有效 JSON。'}); return;
    }
    // 读取请求体期间可能已有另一请求进入规划。
    if (active) { send(response, 409, {error: '已有任务正在规划，请等待完成后再提交。'}); return; }
    try { assertIdle(); }
    catch (error) { send(response, 409, {error: error.message}); return; }
    if (response.destroyed) return;
    let child;
    try {
      child = spawnProcess('python', ['-B', '-X', 'utf8', '-m', 'llm', '--request-stdin'], {
        cwd: root, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
        env: {...process.env, PYTHONIOENCODING: 'utf-8'}
      });
    } catch {
      send(response, 503, {error: '无法启动 Python，请检查本地 Python 环境。'}); return;
    }
    active = child;
    let output = '', diagnostic = '', finished = false;
    function finish(status, data) {
      if (finished) return;
      finished = true; clearTimeout(timer);
      response.removeListener('close', disconnected);
      send(response, status, data);
    }
    function disconnected() {
      if (!response.writableEnded) { finished = true; clearTimeout(timer); child.kill(); }
    }
    const timer = setTimeout(() => {
      finish(504, {error: '规划超时，请稍后重试。'}); child.kill();
    }, timeoutMs);
    response.once('close', disconnected);
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', chunk => {
      if (finished) return;
      output += chunk;
      if (output.length > 1048576) {
        finish(502, {error: '规划结果异常过大，请缩小任务范围。'}); child.kill();
      }
    });
    // 只识别 CLI 生成的结构化错误；忽略其他底层诊断。
    child.stderr.setEncoding('utf8');
    child.stderr.on('data', chunk => { diagnostic = (diagnostic + chunk).slice(-8192); });
    child.once('error', () => {
      if (active === child) active = null;
      finish(503, {error: '无法启动 Python，请检查本地 Python 环境。'});
    });
    child.once('close', async code => {
      if (finished) { if (active === child) active = null; return; }
      // 规划已经返回；之后只派发一次，不把执行确认绑到规划超时。
      clearTimeout(timer);
      try {
      if (code !== 0) {
        let message = '规划失败，请检查模型配置、Python 依赖及 AirSim 是否已启动。';
        for (const line of diagnostic.trim().split('\n').reverse()) {
          try {
            const error = JSON.parse(line);
            if (error?.type === 'llm_error' && typeof error.message === 'string' &&
                error.message.trim() && error.message.length <= 1000) {
              message = error.message; break;
            }
          } catch { /* 普通诊断或异常栈不发往浏览器。 */ }
        }
        finish(502, {error: message}); return;
      }
      try {
        const result = JSON.parse(output);
        if (!['ready', 'needs_clarification'].includes(result.status) || result.executed !== false ||
            !Array.isArray(result.commands) || (result.status === 'ready') !== (result.commands.length > 0)) {
          throw new Error('invalid result');
        }
        if (onReady && result.status === 'ready') {
          try { finish(200, {...result, execution: await onReady(result, flight)}); }
          catch (error) { finish(503, {error: error.message}); return; }
        } else finish(200, result);
      } catch { finish(502, {error: '规划进程返回格式异常，请重试。'}); }
      } finally { if (active === child) active = null; }
    });
    child.stdin.on('error', () => { /* 进程提前退出时由 error/close 统一处理。 */ });
    const live_fleet = getFleet();
    child.stdin.end(JSON.stringify({instruction, conversation, ...(live_fleet ? {live_fleet} : {})}), 'utf8');
  }

  return {handle, close() { if (active) active.kill(); }};
};
