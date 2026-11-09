'use strict';
// 无人机监控接口：管理采集进程、实时状态、轨迹及画面缓存。
const path = require('node:path');
const net = require('node:net');
const {spawn} = require('node:child_process');
const readline = require('node:readline');

const BOUNDARY = 'uavframe';
const UE_VIDEO_PORT = 41460;

function send(response, status, value) {
  if (response.destroyed || response.writableEnded) return;
  const body = Buffer.from(JSON.stringify(value));
  response.writeHead(status, {'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': body.length, 'Cache-Control': 'no-store'});
  response.end(body);
}

module.exports = class AirsimController {
  constructor({root, spawnProcess = spawn}) {
    this.root = root;
    this.spawnProcess = spawnProcess;
    this.bridge = null;
    this.nextBridgeStart = this.lastLiveRequest = 0;
    this.capturing = true;
    this.selectedCamera = 'UAV_01';
    this.state = {connected: false, updated_at: 0, drones: []};
    this.frames = new Map();
    // 推送无人机视角视频的网页连接；画面由 CityNPC 在 UE 内直接输出（CityDroneVideo）。
    this.streams = new Set();
    this.ue = null;
    this.closed = false;
    this.idleTimer = setInterval(() => {
      if (this.bridge && this.capturing && Date.now() - this.lastLiveRequest > 10000) {
        this.capturing = false;
        this.bridge.stdin.write('{"capture":false}\n');
        this.frames.clear();
      }
    }, 5000);
    this.idleTimer.unref();
  }

  startBridge() {
    if (this.closed || this.bridge || Date.now() < this.nextBridgeStart) return;
    const child = this.spawnProcess('python', ['-u', path.join(this.root, 'web', 'utils', 'airsim_bridge.py')], {
      cwd: this.root, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
      env: {...process.env, PYTHONIOENCODING: 'utf-8'}
    });
    this.bridge = child;
    this.capturing = true;
    child.stdin.on('error', () => {});
    child.stdin.write(JSON.stringify({capture: true, camera: this.selectedCamera}) +'\n');
    let diagnostic = '';
    child.stderr.on('data', chunk => { diagnostic = (diagnostic + chunk.toString()).slice(-3000); });
    const lines = readline.createInterface({input: child.stdout});
    lines.on('line', line => {
      if (this.bridge !== child) return;
      try {
        const message = JSON.parse(line);
        if (message.type === 'state') {
          this.state = message;
        } else if (message.type === 'reset') this.frames.clear();
        else if (message.type === 'frame' && this.capturing && message.name === this.selectedCamera) {
          this.frames.set(message.name, {data: Buffer.from(message.data, 'base64'),
            updated_at: message.updated_at, width: message.width, height: message.height});
        }
      } catch (error) { console.error(`AirSim 数据读取失败：${error.message}`); }
    });
    const stopped = error => {
      if (this.bridge !== child) return;
      this.bridge = null;
      this.nextBridgeStart = Date.now() + 5000;
      this.frames.clear();
      this.state = {connected: false, updated_at: Date.now(), drones: [],
        error: error?.message || diagnostic.trim() || 'AirSim 读取进程已停止'};
      if (this.state.error) console.error(this.state.error);
    };
    child.on('error', stopped);
    child.on('close', () => stopped());
  }

  requestLive(camera = this.selectedCamera, capture = true) {
    this.lastLiveRequest = Date.now();
    const changed = camera !== this.selectedCamera;
    this.selectedCamera = camera;
    if (changed) this.frames.clear();
    this.startBridge();
    if (this.bridge && (this.capturing !== capture || changed)) {
      this.capturing = capture;
      if (!capture) this.frames.clear();
      this.bridge.stdin.write(JSON.stringify({capture, camera: this.selectedCamera}) + '\n');
    }
  }

  // 每个连接只保留最新帧：上一帧还没写出时直接丢弃，避免积压造成延迟。
  broadcast(camera, jpeg, capturedAt) {
    if (!this.streams.size) return;
    const head = Buffer.from(`--${BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: ${jpeg.length}\r\n` +
      `X-Frame-Time: ${capturedAt}\r\nX-Sent-Time: ${Date.now()}\r\n\r\n`);
    const part = Buffer.concat([head, jpeg, Buffer.from('\r\n')]);
    for (const client of this.streams) {
      if (client.camera !== camera) continue;
      if (client.busy) { client.dropped++; continue; }
      if (!client.response.write(part)) {
        client.busy = true;
        client.response.once('drain', () => { client.busy = false; });
      }
    }
  }

  openStream(request, response, url) {
    const camera = url.searchParams.get('camera') || this.selectedCamera;
    if (camera.length > 128 || /[^\w.-]/.test(camera)) { send(response, 400, {error: '无效的无人机名称'}); return; }
    response.writeHead(200, {'Content-Type': `multipart/x-mixed-replace; boundary=${BOUNDARY}`,
      'Cache-Control': 'no-store', Connection: 'keep-alive'});
    // mode：capture 出图，off 连上但不出图（测基线用）；fps 默认 30，0 为每帧。
    const fps = url.searchParams.get('fps');
    const client = {response, camera, busy: false, dropped: 0,
      mode: url.searchParams.get('mode') === 'off' ? 'off' : 'capture',
      fps: fps === null ? 30 : Math.max(0, Math.min(120, Number(fps) || 0))};
    this.streams.add(client);
    response.on('close', () => { this.streams.delete(client); this.updateUe(); });
    this.updateUe();
  }

  // CityNPC 视频输出：本机 TCP，UE 发 [u32 头长][头 JSON][u32 图长][JPEG]，网页服务回一行 JSON 选择无人机与模式。
  updateUe() {
    const clients = [...this.streams];
    if (!clients.length) {
      if (this.ue) { const old = this.ue; this.ue = null; old.socket.destroy(); }
      return;
    }
    const wanted = clients[clients.length - 1];
    const control = JSON.stringify({drone: wanted.camera, mode: wanted.mode, fps: wanted.fps}) + '\n';
    if (this.ue) {
      if (this.ue.control !== control && this.ue.connected) this.ue.socket.write(control);
      this.ue.control = control;
      return;
    }
    const socket = net.createConnection({host: '127.0.0.1', port: UE_VIDEO_PORT});
    const ue = this.ue = {socket, control, connected: false, buffer: Buffer.alloc(0)};
    socket.setNoDelay(true);
    socket.on('connect', () => { ue.connected = true; socket.write(ue.control); });
    socket.on('data', chunk => {
      ue.buffer = Buffer.concat([ue.buffer, chunk]);
      for (;;) {
        if (ue.buffer.length < 4) break;
        const headLength = ue.buffer.readUInt32LE(0);
        if (ue.buffer.length < 8 + headLength) break;
        const imageLength = ue.buffer.readUInt32LE(4 + headLength);
        if (ue.buffer.length < 8 + headLength + imageLength) break;
        let head = {};
        try { head = JSON.parse(ue.buffer.subarray(4, 4 + headLength).toString('utf8')); } catch {}
        const jpeg = Buffer.from(ue.buffer.subarray(8 + headLength, 8 + headLength + imageLength));
        ue.buffer = ue.buffer.subarray(8 + headLength + imageLength);
        this.broadcast(head.drone, jpeg, head.t || Date.now());
      }
    });
    const retry = () => {
      if (this.ue !== ue) return;
      this.ue = null;
      setTimeout(() => this.updateUe(), 2000).unref();
    };
    socket.on('error', retry);
    socket.on('close', retry);
  }

  // 规划模块读取原始采样，避免将网页展示用的占位无人机用于任务派发。
  getState() { return this.state; }

  clearTrails() {
    this.state = {...this.state, drones: this.state.drones.map(drone => ({...drone, trail: []}))};
    // 清空采集进程中的全部轨迹；旧进程的在途消息由上方child检查直接丢弃。
    const child = this.bridge;
    this.bridge = null;
    this.frames.clear();
    this.nextBridgeStart = 0;
    if (child) {
      child.kill();
      this.startBridge();
    }
  }

  liveState() {
    const now = Date.now();
    const freshFrame = [...this.frames.values()].some(frame => now - frame.updated_at < 2500);
    const connected = (this.state.connected && now - this.state.updated_at < 2500) || freshFrame;
    const drones = this.state.drones.length ? this.state.drones :
      Array.from({length: 5}, (_, i) => ({name: `UAV_${String(i + 1).padStart(2, '0')}`}));
    return {...this.state, connected, drones: drones.map(drone => {
      const frame = this.frames.get(drone.name);
      return {...drone, position_m: now - (drone.updated_at || 0) < 1500 ? drone.position_m : null,
        camera_updated_at: frame && now - frame.updated_at < 2500 ? frame.updated_at : 0};
    })};
  }

  handle(request, response, url) {
    if (this.closed) { send(response, 503, {error: '网页服务已停止。'}); return; }
    const pathname = decodeURIComponent(url.pathname);
    if (pathname === '/api/airsim/trails/clear') {
      if (request.method !== 'POST') {
        response.writeHead(405, {Allow: 'POST'}); response.end(); return;
      }
      this.clearTrails();
      send(response, 200, {cleared: true}); return;
    }
    if (!['GET', 'HEAD'].includes(request.method)) {
      response.writeHead(405, {Allow: 'GET, HEAD'}); response.end(); return;
    }
    if (pathname === '/api/airsim/stream') { this.openStream(request, response, url); return; }
    if (pathname === '/api/airsim/state') {
      const camera = url.searchParams.get('camera') || this.selectedCamera;
      if (camera.length > 128 || /[\x00-\x1f]/.test(camera)) {
        send(response, 400, {error: '无效的无人机名称'}); return;
      }
      this.requestLive(camera, url.searchParams.get('capture') !== 'false');
      send(response, 200, this.liveState()); return;
    }
    if (pathname.startsWith('/api/airsim/camera/')) {
      this.requestLive();
      const frame = this.frames.get(pathname.slice('/api/airsim/camera/'.length));
      if (!frame || Date.now() - frame.updated_at >= 2500) {
        send(response, 503, {error: '摄像头尚未返回画面'}); return;
      }
      response.writeHead(200, {'Content-Type': 'image/jpeg', 'Content-Length': frame.data.length,
        'Cache-Control': 'no-store', 'X-Frame-Time': String(frame.updated_at)});
      if (request.method === 'HEAD') response.end(); else response.end(frame.data);
      return;
    }
    send(response, 404, {error: '接口不存在'});
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    clearInterval(this.idleTimer);
    const child = this.bridge;
    this.bridge = null;
    this.frames.clear();
    for (const client of this.streams) client.response.end();
    this.streams.clear();
    this.updateUe();
    if (child) child.kill();
  }
};
