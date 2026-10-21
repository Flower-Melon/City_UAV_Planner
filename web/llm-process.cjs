'use strict';
// 给HTTP层保留一次请求的进程接口，底层Python/SDK连接跨任务复用。
const {spawn} = require('node:child_process');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
const fs = require('node:fs');
const path = require('node:path');

module.exports = function createPlannerProcess({root, spawnProcess = spawn}) {
  let worker = null, active = null, nextId = 0, fingerprint;

  function settle(code, result, error) {
    const task = active; active = null;
    if (!task) return;
    if (result !== undefined) task.stdout.end(JSON.stringify(result));
    if (error) task.stderr.end(JSON.stringify({type: 'llm_error', message: error}) + '\n');
    task.emit('close', code);
  }
  function stop() {
    const old = worker; worker = null;
    if (old) old.kill();
    settle(1, undefined, '规划进程已停止，请重试。');
  }
  function start() {
    // .env更新后新建进程，避免dotenv旧环境覆盖用户的新配置。
    let current;
    try { const s = fs.statSync(path.join(root, 'llm', '.env')); current = `${s.mtimeMs}:${s.size}`; }
    catch { current = ''; }
    if (worker && current !== fingerprint) stop();
    if (worker) return;
    fingerprint = current;
    const child = spawnProcess('python', ['-B', '-u', '-X', 'utf8', '-m', 'llm.functions.worker'], {
      cwd: root, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
      env: {...process.env, PYTHONIOENCODING: 'utf-8'}
    });
    worker = child;
    let buffer = '', diagnostic = '';
    child.stdin.on('error', () => {});
    child.stderr.setEncoding('utf8');
    child.stderr.on('data', chunk => { diagnostic = (diagnostic + chunk).slice(-8192); });
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', chunk => {
      if (worker !== child) return;
      buffer += chunk;
      if (buffer.length > 1048576) { stop(); return; }
      let end;
      while ((end = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
        try {
          const message = JSON.parse(line);
          if (!active || message.id !== active.id) { stop(); return; }
          if (typeof message.error === 'string' && message.error.length <= 1000)
            settle(1, undefined, message.error);
          else if (message.result && typeof message.result === 'object') settle(0, message.result);
          else { stop(); return; }
        } catch { stop(); return; }
      }
    });
    const exited = () => {
      if (worker !== child) return;
      worker = null;
      // 仅将CLI结构化错误送往既有HTTP错误过滤器。
      if (active) active.stderr.write(diagnostic);
      settle(1);
    };
    child.once('error', exited); child.once('close', exited);
  }
  function requestProcess() {
    if (active) throw Error('planner busy');
    start();
    const task = new EventEmitter();
    task.id = ++nextId;
    task.stdin = new PassThrough(); task.stdout = new PassThrough(); task.stderr = new PassThrough();
    task.kill = stop;
    active = task;
    const chunks = [];
    task.stdin.on('data', chunk => chunks.push(chunk));
    task.stdin.once('finish', () => {
      if (active !== task || !worker) return;
      try {
        const data = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        worker.stdin.write(JSON.stringify({...data, id: task.id}) + '\n');
      } catch { stop(); }
    });
    return task;
  }
  return {start, spawnProcess: requestProcess, close: stop};
};
