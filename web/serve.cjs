'use strict';
// 网页服务入口：静态资源、接口分发及各后端模块的生命周期。
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const {createHash} = require('node:crypto');
const {spawn} = require('node:child_process');
const AirsimController = require('./server/airsim.cjs');
const LlmController = require('./server/llm.cjs');
const AbnormalController = require('./server/abnormal.cjs');
const root = fs.realpathSync(path.resolve(__dirname, '..'));
const airsimAPI = new AirsimController({root});
const llmAPI = new LlmController({root, getState: () => airsimAPI.getState()});
const abnormalAPI = new AbnormalController({root});
const identity = createHash('sha256').update('town12-live-viewer-v9-abnormal:' + (process.platform === 'win32' ? root.toLowerCase() : root)).digest('hex');
const preferredPort = 8765;
const types = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
  '.png': 'image/png', '.md': 'text/plain; charset=utf-8'};

const server = http.createServer((request, response) => {
  response.setHeader('X-Town12-Viewer', identity);
  try {
    const url = new URL(request.url, 'http://127.0.0.1');
    const pathname = decodeURIComponent(url.pathname);
    if (pathname.startsWith('/api/airsim/')) { void airsimAPI.handle(request, response, url); return; }
    if (pathname.startsWith('/api/llm/')) { void llmAPI.handle(request, response, url); return; }
    if (pathname.startsWith('/api/abnormal/')) { void abnormalAPI.handle(request, response, url); return; }
    if (!['GET', 'HEAD'].includes(request.method)) {
      response.writeHead(405, {Allow: 'GET, HEAD'}); response.end(); return;
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
    llmAPI.start();
    console.log(`Town12 标定可视化：${url}\n按 Ctrl+C 停止服务。`); open(url); return;
  }
  throw Error('8765至8784端口均被占用，请关闭不需要的本地服务后重试。');
})().catch(error => { console.error(error.message); process.exitCode = 1; if (server.listening) server.close(); });
for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => {
  llmAPI.close();
  abnormalAPI.close();
  airsimAPI.close();
  server.close(); process.exit(0);
});
process.on('exit', () => { llmAPI.close(); abnormalAPI.close(); airsimAPI.close(); });
