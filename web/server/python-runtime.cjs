'use strict';
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

function requestError(message, statusCode) {
  return Object.assign(new Error(message), {statusCode});
}

// 管理 Python 通信通道；规划、状态读取和每架无人机使用独立进程。
class PythonRuntime {
  constructor({root, spawnProcess = spawn, pythonExecutable = process.env.CITY_UAV_PYTHON || 'python'}) {
    this.root = root;
    this.spawnProcess = spawnProcess;
    this.pythonExecutable = pythonExecutable;
    this.workers = new Map();
    this.processes = new Set();
    this.stopping = false;
    this.nextId = 0;
    this.closed = false;
  }

  start(channel = 'planning') {
    if (this.closed) throw Error('执行服务已停止，请重启网页服务。');
    if (this.stopping) throw Error('正在终止后台进程，请等待无人机复位。');
    let fingerprint = '';
    if (channel === 'planning' || channel.startsWith('vision:')) {
      try {
        const stat = fs.statSync(path.join(this.root, 'llm', '.env'));
        fingerprint = `${stat.mtimeMs}:${stat.size}`;
      } catch { /* 模型也可以直接读取环境变量。 */ }
    }
    let worker = this.workers.get(channel);
    if (worker && (channel === 'planning' || channel.startsWith('vision:')) && worker.fingerprint !== fingerprint) {
      if (worker.pending.size) throw requestError('已有任务正在规划，请等待完成。', 409);
      this.cancel(channel);
      worker = null;
    }
    if (worker) return worker;
    const child = this.spawnProcess(this.pythonExecutable, ['-B', '-u', '-X', 'utf8',
      path.join(this.root, 'web', 'utils', 'python_bridge.py')], {
      cwd: this.root, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
      env: {...process.env, PYTHONIOENCODING: 'utf-8'}
    });
    worker = {channel, child, fingerprint, pending: new Map(), buffer: '', failed: false, exited: false};
    this.workers.set(channel, worker);
    this.processes.add(worker);
    child.stdout.setEncoding('utf8');
    child.stderr.resume();
    child.stdout.on('data', chunk => this.receive(worker, chunk));
    child.stdin.on('error', () => this.fail(worker, Error('Python 通信中断，请核实无人机状态。')));
    child.stdout.on('error', () => this.fail(worker, Error('Python 结果读取失败，请核实无人机状态。')));
    child.once('error', () => this.fail(worker, Error('无法启动 Python，请检查本地环境。'), {spawnFailure: true}));
    child.once('close', () => {
      worker.exited = true;
      this.processes.delete(worker);
      this.fail(worker, Error('Python 进程已退出，请核实无人机状态。'), {exited: true});
    });
    return worker;
  }

  request(operation, data = {}, {channel = 'planning', timeoutMs = 15000, onEvent = null} = {}) {
    let worker;
    try { worker = this.start(channel); }
    catch (error) { return Promise.reject(error); }
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      const timer = timeoutMs > 0 ? setTimeout(() => {
        this.fail(worker, requestError('Python 请求超时，请稍后重试。', 504), {terminate: true});
      }, timeoutMs) : null;
      worker.pending.set(id, {operation, resolve, reject, timer, onEvent});
      try {
        worker.child.stdin.write(JSON.stringify({id, operation, ...data}) + '\n', error => {
          if (error) this.fail(worker, Error('无法向 Python 发送请求，请核实无人机状态。'));
        });
      } catch (error) { this.fail(worker, error); }
    });
  }

  receive(worker, chunk) {
    if (worker.failed) return;
    worker.buffer += chunk;
    if (worker.buffer.length > 1048576) {
      this.fail(worker, Error('Python 返回内容过大。'), {terminate: true}); return;
    }
    let end;
    while ((end = worker.buffer.indexOf('\n')) >= 0) {
      const line = worker.buffer.slice(0, end); worker.buffer = worker.buffer.slice(end + 1);
      let message;
      try { message = JSON.parse(line); }
      catch { this.fail(worker, Error('Python 返回格式无效。'), {terminate: true}); return; }
      const call = message && worker.pending.get(message.id);
      if (!call || (!Object.hasOwn(message, 'result') && !Object.hasOwn(message, 'event') && typeof message.error !== 'string')) {
        this.fail(worker, Error('Python 返回了无法匹配的结果。'), {terminate: true}); return;
      }
      if (Object.hasOwn(message, 'event')) {
        try { call.onEvent?.(message.event); }
        catch (error) { this.fail(worker, error, {terminate: true}); return; }
        continue;
      }
      clearTimeout(call.timer); worker.pending.delete(message.id);
      if (Object.hasOwn(message, 'error')) {
        call.reject(Object.assign(new Error(message.error), {errorType: message.error_type || 'PythonError'}));
      } else call.resolve(message.result);
    }
  }

  fail(worker, error, {terminate = false, spawnFailure = false, exited = false} = {}) {
    if (worker.failed) return;
    worker.failed = true;
    const searching = [...worker.pending.values()].some(call => call.operation === 'search');
    for (const call of worker.pending.values()) {
      clearTimeout(call.timer);
      call.reject(Object.assign(new Error(error.message), {
        statusCode: error.statusCode, uncertain: !spawnFailure && call.operation === 'search'
      }));
    }
    worker.pending.clear();
    if (this.workers.get(worker.channel) === worker) this.workers.delete(worker.channel);
    if (!exited) {
      // 飞行进程关闭输入后，仍会完成当前区域及悬停清理。
      if (terminate && !searching && !worker.channel.startsWith('drone:')) worker.child.kill();
      else worker.child.stdin.end();
    }
  }

  cancel(channel) {
    const worker = this.workers.get(channel);
    if (worker) this.fail(worker, Error('请求已取消。'), {terminate: true});
  }

  // “新任务”强制停止全部任务进程，等待退出后才允许复位，不能沿用飞行的优雅收尾。
  async terminateAll() {
    this.stopping = true;
    try {
      const results = await Promise.allSettled([...this.processes].map(worker => new Promise((resolve, reject) => {
        if (worker.exited) { resolve(); return; }
        const child = worker.child;
        let settled = false;
        const finish = error => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          child.removeListener('close', closed);
          if (error) reject(error); else resolve();
        };
        const closed = () => finish();
        const timer = setTimeout(() => finish(Error('后台进程未能退出，请点击新任务重试复位。')), 10000);
        child.once('close', closed);
        this.fail(worker, Error('任务已被新任务操作终止。'), {exited: true});
        const kill = () => {
          try { if (!worker.exited) child.kill('SIGKILL'); }
          catch { finish(Error('无法终止后台进程，请重试复位。')); }
        };
        if (process.platform === 'win32' && Number.isInteger(child.pid)) {
          // 同时终止该Python进程启动的辅助子进程；只按本服务登记的PID操作。
          const killer = spawn('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'],
            {windowsHide: true, stdio: 'ignore'});
          killer.once('error', kill);
          killer.once('close', code => { if (code !== 0) kill(); });
        } else kill();
      })));
      const failure = results.find(result => result.status === 'rejected');
      if (failure) throw failure.reason;
    } finally { this.stopping = false; }
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    for (const worker of this.workers.values()) {
      if (worker.channel.startsWith('drone:')) worker.child.stdin.end();
      else this.fail(worker, Error('网页服务已停止。'), {terminate: true});
    }
  }
}

module.exports = PythonRuntime;
