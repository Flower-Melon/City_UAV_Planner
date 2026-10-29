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
    this.nextId = 0;
    this.closed = false;
  }

  start(channel = 'planning') {
    if (this.closed) throw Error('执行服务已停止，请重启网页服务。');
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
    worker = {channel, child, fingerprint, pending: new Map(), buffer: '', failed: false};
    this.workers.set(channel, worker);
    child.stdout.setEncoding('utf8');
    child.stderr.resume();
    child.stdout.on('data', chunk => this.receive(worker, chunk));
    child.stdin.on('error', () => this.fail(worker, Error('Python 通信中断，请核实无人机状态。')));
    child.stdout.on('error', () => this.fail(worker, Error('Python 结果读取失败，请核实无人机状态。')));
    child.once('error', () => this.fail(worker, Error('无法启动 Python，请检查本地环境。'), {spawnFailure: true}));
    child.once('close', () => this.fail(worker, Error('Python 进程已退出，请核实无人机状态。'), {exited: true}));
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
