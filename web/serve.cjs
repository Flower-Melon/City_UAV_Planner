'use strict';
// Map viewer and local AirSim preview endpoints.
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const {createHash} = require('node:crypto');
const {spawn} = require('node:child_process');
const readline = require('node:readline');
const root = fs.realpathSync(path.resolve(__dirname, '..'));
const identity = createHash('sha256').update('town12-live-viewer-v6:' + (process.platform === 'win32' ? root.toLowerCase() : root)).digest('hex');
const preferredPort = 8765;
const types = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
  '.png': 'image/png', '.md': 'text/plain; charset=utf-8'};
let bridge, nextBridgeStart = 0;
let lastLiveRequest = 0;
let capturing = true;
let selectedCamera = 'UAV_01';
let airsimState = {connected: false, updated_at: 0, drones: []};
const cameraFrames = new Map();
let trailsClearedAt = 0;

function startBridge() {
  if (bridge || Date.now() < nextBridgeStart) return;
  const child = spawn('python', ['-u', path.join(__dirname, 'airsim_bridge.py')], {
    cwd: root, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
    env: {...process.env, PYTHONIOENCODING: 'utf-8'}
  });
  bridge = child;
  capturing = true;
  child.stdin.on('error', () => {});
  child.stdin.write(JSON.stringify({capture: true, camera: selectedCamera}) + '\n');
  let diagnostic = '';
  child.stderr.on('data', chunk => { diagnostic = (diagnostic + chunk.toString()).slice(-3000); });
  const lines = readline.createInterface({input: child.stdout});
  lines.on('line', line => {
    try {
      const message = JSON.parse(line);
      if (message.type === 'state') {
        // 已排队的旧采样也不能在清除后重新显示。
        airsimState = {...message, drones: message.drones.map(drone => ({...drone,
          trail: (drone.trail || []).filter(point => point[0] > trailsClearedAt)}))};
      }
      else if (message.type === 'reset') cameraFrames.clear();
      else if (message.type === 'frame') {
        if (capturing && message.name === selectedCamera) {
          cameraFrames.set(message.name, {data: Buffer.from(message.data, 'base64'),
            updated_at: message.updated_at, width: message.width, height: message.height});
        }
      }
    } catch (error) { console.error(`AirSim 数据读取失败：${error.message}`); }
  });
  const stopped = error => {
    if (bridge !== child) return;
    bridge = undefined; nextBridgeStart = Date.now() + 5000;
    cameraFrames.clear();
    airsimState = {connected: false, updated_at: Date.now(), drones: [],
      error: error?.message || diagnostic.trim() || 'AirSim 读取进程已停止'};
    if (airsimState.error) console.error(airsimState.error);
  };
  child.on('error', stopped);
  child.on('close', () => stopped());
}

function sendJSON(response, status, data) {
  const body = Buffer.from(JSON.stringify(data));
  response.writeHead(status, {'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': body.length, 'Cache-Control': 'no-store'});
  response.end(body);
}

function requestLive(camera = selectedCamera) {
  lastLiveRequest = Date.now();
  const changed = camera !== selectedCamera;
  selectedCamera = camera;
  if (changed) cameraFrames.clear();
  startBridge();
  if (bridge && (!capturing || changed)) {
    capturing = true;
    bridge.stdin.write(JSON.stringify({capture: true, camera: selectedCamera}) + '\n');
  }
}

function liveState() {
  const now = Date.now();
  const freshFrame = [...cameraFrames.values()].some(frame => now - frame.updated_at < 2500);
  const connected = (airsimState.connected && now - airsimState.updated_at < 2500) || freshFrame;
  const drones = airsimState.drones.length ? airsimState.drones : Array.from({length: 5}, (_, i) => ({name: `UAV_${String(i + 1).padStart(2, '0')}`}));
  return {...airsimState, connected, drones: drones.map(drone => {
    const frame = cameraFrames.get(drone.name);
    return {...drone, position_m: now - (drone.updated_at || 0) < 1500 ? drone.position_m : null,
      camera_updated_at: frame && now - frame.updated_at < 2500 ? frame.updated_at : 0};
  })};
}
const server = http.createServer((request, response) => {
  response.setHeader('X-Town12-Viewer', identity);
  try {
    const url = new URL(request.url, 'http://127.0.0.1');
    const pathname = decodeURIComponent(url.pathname);
    if (pathname === '/api/airsim/trails/clear') {
      if (request.method !== 'POST') {
        response.writeHead(405, {Allow: 'POST'}); response.end(); return;
      }
      trailsClearedAt = Date.now();
      airsimState = {...airsimState, drones: airsimState.drones.map(drone => ({...drone, trail: []}))};
      if (bridge) bridge.stdin.write('{"clear_trails":true}\n');
      sendJSON(response, 200, {cleared: true}); return;
    }
    if (!['GET', 'HEAD'].includes(request.method)) {
      response.writeHead(405, {Allow: 'GET, HEAD'}); response.end(); return;
    }
    if (pathname === '/api/airsim/state') {
      const camera = url.searchParams.get('camera') || selectedCamera;
      if (camera.length > 128 || /[\x00-\x1f]/.test(camera)) {
        sendJSON(response, 400, {error: '无效的无人机名称'}); return;
      }
      requestLive(camera); sendJSON(response, 200, liveState()); return;
    }
    if (pathname.startsWith('/api/airsim/camera/')) {
      requestLive();
      const frame = cameraFrames.get(pathname.slice('/api/airsim/camera/'.length));
      if (!frame || Date.now() - frame.updated_at >= 2500) {
        sendJSON(response, 503, {error: '摄像头尚未返回画面'}); return;
      }
      response.writeHead(200, {'Content-Type': 'image/jpeg', 'Content-Length': frame.data.length,
        'Cache-Control': 'no-store', 'X-Frame-Time': String(frame.updated_at)});
      if (request.method === 'HEAD') response.end(); else response.end(frame.data);
      return;
    }
    // Relative CSS/JS/Map links resolve from /web/index.html, never from /.
    if (pathname === '/') {
      response.writeHead(302, {Location: '/web/index.html'}); response.end(); return;
    }
    const filename = path.resolve(root, '.' + pathname);
    const relative = path.relative(root, filename).split(path.sep).join('/');
    if (!/^(web|Map)\//.test(relative) && relative !== 'README.md') {
      response.writeHead(404); response.end('Not found'); return;
    }
    const contentType = types[path.extname(filename)];
    const stat = contentType && fs.statSync(filename);
    if (!stat || !stat.isFile()) { response.writeHead(404); response.end('Not found'); return; }
    response.writeHead(200, {'Content-Type': contentType, 'Content-Length': stat.size, 'Cache-Control': 'no-cache'});
    if (request.method === 'HEAD') response.end();
    else fs.createReadStream(filename).on('error', () => response.destroy()).pipe(response);
  } catch {
    response.writeHead(404); response.end('Not found');
  }
});
function open(url) {
  if (process.platform === 'win32') {
    const child = spawn('cmd.exe', ['/c', 'start', '', url], {windowsHide: true, stdio: 'ignore'});
    child.on('error', () => console.log(`请手动打开 ${url}`));
  }
}
function isExistingViewer(port) {
  return new Promise(resolve => {
    const request = http.request({host: '127.0.0.1', port, path: '/web/index.html', method: 'HEAD', agent: false}, response => {
      resolve(response.statusCode === 200 && response.headers['x-town12-viewer'] === identity);
      response.resume();
    });
    const timer = setTimeout(() => { request.destroy(); resolve(false); }, 750);
    request.on('close', () => clearTimeout(timer));
    request.on('error', () => resolve(false)); request.end();
  });
}
function listen(port) {
  return new Promise((resolve, reject) => {
    const failed = error => { server.removeListener('listening', started); reject(error); };
    const started = () => { server.removeListener('error', failed); resolve(); };
    server.once('error', failed); server.once('listening', started); server.listen(port, '127.0.0.1');
  });
}
(async () => {
  for (let port = preferredPort; port <= Math.min(65535, preferredPort + 19); port++) {
    const url = `http://127.0.0.1:${port}/web/index.html`;
    try { await listen(port); }
    catch (error) {
      if (error.code !== 'EADDRINUSE') throw error;
      if (await isExistingViewer(port)) { console.log(`已在运行：${url}`); open(url); return; }
      continue;
    }
    console.log(`Town12 标定可视化：${url}\n按 Ctrl+C 停止服务。`); open(url); return;
  }
  throw Error('8765至8784端口均被占用，请关闭不需要的本地服务后重试。');
})().catch(error => { console.error(error.message); process.exitCode = 1; if (server.listening) server.close(); });
for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => {
  if (bridge) bridge.kill();
  server.close(); process.exit(0);
});
setInterval(() => {
  if (bridge && capturing && Date.now() - lastLiveRequest > 10000) {
    capturing = false; bridge.stdin.write('{"capture":false}\n');
    cameraFrames.clear();
  }
}, 5000).unref();
process.on('exit', () => { if (bridge) bridge.kill(); });
