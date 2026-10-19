'use strict';

// Cache the current viewport, rather than magnifying a fixed world raster.
// Geometry and line widths are rendered again only when the view/layers change.
class CityMapVisuals {
  constructor() {
    this.cache = document.createElement('canvas');
    this.key = '';
    this.roads = [];
    this.buildings = [];
  }

  static bounds(coordinates) {
    const b = [Infinity, Infinity, -Infinity, -Infinity];
    for (const [x, y] of coordinates) {
      b[0] = Math.min(b[0], x); b[1] = Math.min(b[1], y);
      b[2] = Math.max(b[2], x); b[3] = Math.max(b[3], y);
    }
    return b;
  }

  async prepare(zoneParts, network, colors) {
    this.zones = zoneParts;
    this.colors = colors;
    for (let i = 0; i < network.edges.length; i++) {
      const edge = network.edges[i];
      if (edge.enabled === false || edge.length_m === 0) continue;
      const line = edge.polyline_xy_m, path = new Path2D();
      line.forEach(([x, y], j) => j ? path.lineTo(x, y) : path.moveTo(x, y));
      this.roads.push({path, bounds: CityMapVisuals.bounds(line)});
      if (i % 2000 === 0) await new Promise(resolve => setTimeout(resolve, 0));
    }
    this.invalidate();
  }

  async setBuildings(features, polygonPath) {
    const records = [];
    for (let i = 0; i < features.length; i++) {
      const f = features[i];
      const polygons = f.geometry.type === 'Polygon' ? [f.geometry.coordinates] : f.geometry.coordinates;
      for (const rings of polygons) {
        records.push({path: polygonPath(rings), bounds: CityMapVisuals.bounds(rings[0])});
      }
      if (i % 800 === 0) await new Promise(resolve => setTimeout(resolve, 0));
    }
    this.buildings = records;
    this.invalidate();
  }

  invalidate() { this.key = ''; }

  draw(target, view, layers) {
    const {scale, ox, oy, width, height, ratio} = view;
    const key = JSON.stringify([scale, ox, oy, width, height, ratio, layers]);
    if (key !== this.key) {
      const pixelWidth = Math.max(1, Math.round(width * ratio));
      const pixelHeight = Math.max(1, Math.round(height * ratio));
      if (this.cache.width !== pixelWidth) this.cache.width = pixelWidth;
      if (this.cache.height !== pixelHeight) this.cache.height = pixelHeight;
      const c = this.cache.getContext('2d');
      c.setTransform(1, 0, 0, 1, 0, 0); c.clearRect(0, 0, pixelWidth, pixelHeight);
      c.setTransform(ratio * scale, 0, 0, ratio * scale, ratio * ox, ratio * oy);
      const pad = 5 / scale;
      const bounds = [-ox / scale - pad, -oy / scale - pad,
        (width - ox) / scale + pad, (height - oy) / scale + pad];
      const visible = record => {
        const b = record.bounds;
        return b[0] <= bounds[2] && b[2] >= bounds[0] && b[1] <= bounds[3] && b[3] >= bounds[1];
      };
      if (layers.zones) {
        c.lineWidth = 1 / scale;
        for (const part of this.zones) {
          if (!visible(part) || (layers.category && part.feature.properties.category !== layers.category)) continue;
          const color = this.colors.get(part.feature.properties.category) || '#a0adb6';
          c.fillStyle = color + '42'; c.strokeStyle = color;
          c.fill(part.path, 'evenodd'); c.stroke(part.path);
        }
      }
      if (layers.roads) {
        c.strokeStyle = '#dfe8fb'; c.lineWidth = .8 / scale;
        for (const record of this.roads) if (visible(record)) c.stroke(record.path);
      }
      if (layers.buildings) {
        c.fillStyle = '#18294377'; c.strokeStyle = '#172c42'; c.lineWidth = .7 / scale;
        for (const record of this.buildings) {
          if (visible(record)) { c.fill(record.path, 'evenodd'); c.stroke(record.path); }
        }
      }
      this.key = key;
    }
    target.save(); target.setTransform(1, 0, 0, 1, 0, 0);
    target.drawImage(this.cache, 0, 0); target.restore();
  }

  static legend(categories, container) {
    container.replaceChildren();
    for (const category of categories) {
      const item = document.createElement('span');
      const swatch = document.createElement('i');
      swatch.className = 'legend-swatch'; swatch.style.backgroundColor = category.color;
      swatch.setAttribute('aria-hidden', 'true');
      item.append(swatch, document.createTextNode(category.name)); container.append(item);
    }
  }
};

const $ = id => document.getElementById(id);
const canvas = $('map'), ctx = canvas.getContext('2d');
const state = {scale: 1, ox: 0, oy: 0, width: 1, height: 1, ratio: 1,
  selected: null, drag: null, frame: 0};
let zones, points, roads, background, buildings;
let zoneParts = [], pointRecords = [], colors = new Map(), categoryNames = new Map();
let ready = false, buildingRequest;
let liveDrones = [];
const worldSize = 4000;
const visuals = new CityMapVisuals();

class SpatialGrid {
  constructor(size = 100) { this.size = size; this.cells = new Map(); }
  insert(record) {
    const [x0, y0, x1, y1] = record.bounds;
    for (let x = Math.floor(x0 / this.size); x <= Math.floor(x1 / this.size); x++) {
      for (let y = Math.floor(y0 / this.size); y <= Math.floor(y1 / this.size); y++) {
        const key = `${x},${y}`;
        if (!this.cells.has(key)) this.cells.set(key, []);
        this.cells.get(key).push(record);
      }
    }
  }
  query(x, y) { return this.cells.get(`${Math.floor(x / this.size)},${Math.floor(y / this.size)}`) || []; }
}
const zoneIndex = new SpatialGrid();
const groups = new Map();
const yieldFrame = () => new Promise(resolve => setTimeout(resolve, 0));

async function loadJSON(name) {
  const response = await fetch(`../Map/${name}`, {cache: 'no-store'});
  if (!response.ok) throw new Error(`无法读取 ${name}（HTTP ${response.status}）`);
  try { return await response.json(); }
  catch { throw new Error(`${name} 不是有效的 JSON 文件`); }
}
function polygonPath(rings) {
  const path = new Path2D();
  for (const ring of rings) {
    ring.forEach(([x, y], i) => i ? path.lineTo(x, y) : path.moveTo(x, y));
    path.closePath();
  }
  return path;
}
function boundsOf(ring) {
  const b = [Infinity, Infinity, -Infinity, -Infinity];
  for (const [x, y] of ring) {
    b[0] = Math.min(b[0], x); b[1] = Math.min(b[1], y);
    b[2] = Math.max(b[2], x); b[3] = Math.max(b[3], y);
  }
  return b;
}
async function prepareGeometry() {
  for (let i = 0; i < zones.features.length; i++) {
    const feature = zones.features[i];
    const polygons = feature.geometry.type === 'Polygon' ? [feature.geometry.coordinates] : feature.geometry.coordinates;
    const parts = [];
    for (const rings of polygons) {
      const record = {feature, bounds: boundsOf(rings[0]), path: polygonPath(rings)};
      parts.push(record); zoneIndex.insert(record); zoneParts.push(record);
    }
    groups.set(feature.id, parts);
    if (i % 16 === 0) await yieldFrame();
  }
  pointRecords = points.features;
  await visuals.prepare(zoneParts, roads, colors);
  rebuildZones();
}
function rebuildZones() {
  visuals.invalidate();
  schedule();
}
async function ensureBuildings() {
  if (!buildingRequest) buildingRequest = (async () => {
    buildings = await loadJSON('buildings.json');
    if (!Array.isArray(buildings.features)) throw new Error('buildings.json 缺少 features 数组');
    await visuals.setBuildings(buildings.features, polygonPath);
  })().catch(error => { buildingRequest = null; throw error; });
  await buildingRequest;
}

function resize() {
  const box = canvas.getBoundingClientRect();
  state.width = box.width; state.height = box.height;
  state.ratio = Math.min(2, devicePixelRatio || 1);
  canvas.width = Math.round(box.width * state.ratio); canvas.height = Math.round(box.height * state.ratio);
}
function fit() {
  resize();
  state.scale = Math.min(state.width, state.height) / 4200;
  state.ox = (state.width - worldSize * state.scale) / 2 + 1000 * state.scale;
  state.oy = (state.height - worldSize * state.scale) / 2 + 3000 * state.scale;
  schedule();
}
function schedule() {
  if (!state.frame) state.frame = requestAnimationFrame(() => { state.frame = 0; draw(); });
}
function visiblePoint(point) {
  const mode = $('point-mode').value;
  return mode === 'all' || (mode === 'verified' && point.properties.execution_enabled === true);
}
function draw() {
  const {ratio, scale, ox, oy, width, height} = state;
  ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.setTransform(ratio * scale, 0, 0, ratio * scale, ratio * ox, ratio * oy);
  if ($('layer-base').checked && background?.complete && background.naturalWidth) ctx.drawImage(background, -1000, -3000, worldSize, worldSize);
  if (!ready) return;
  visuals.draw(ctx, state, {
    zones: $('layer-zones').checked, category: $('category').value,
    buildings: $('layer-buildings').checked, roads: $('layer-roads').checked
  });
  if ($('layer-zones').checked) {
    ctx.font = `600 ${16 / scale}px system-ui, "Microsoft YaHei", sans-serif`;
    ctx.textAlign = 'center'; ctx.lineWidth = 4 / scale; ctx.lineJoin = 'round';
    for (const f of zones.features) {
      if (f.properties.object_type !== 'facility' || ($('category').value && f.properties.category !== $('category').value)) continue;
      const q = f.properties.label_xy_m;
      if (!q || q[0] * scale + ox < -80 || q[0] * scale + ox > width + 80 || q[1] * scale + oy < -20 || q[1] * scale + oy > height + 20) continue;
      ctx.strokeStyle = '#142638'; ctx.fillStyle = '#fff';
      ctx.strokeText(f.properties.name, q[0], q[1]); ctx.fillText(f.properties.name, q[0], q[1]);
    }
  }
  ctx.lineWidth = 1 / scale;
  for (const point of pointRecords) {
    if (!visiblePoint(point)) continue;
    const [x, y] = point.geometry.coordinates;
    if (x * scale + ox < -8 || x * scale + ox > width + 8 || y * scale + oy < -8 || y * scale + oy > height + 8) continue;
    const access = point.properties.point_type === 'outdoor_access';
    ctx.beginPath(); ctx.arc(x, y, (access ? 2 : 4) / scale, 0, Math.PI * 2);
    ctx.fillStyle = point.properties.execution_enabled ? (access ? '#eeeef4' : '#ffde69') : '#8293a4';
    ctx.strokeStyle = point.properties.execution_enabled ? '#253c56' : '#dbe4ec'; ctx.fill(); ctx.stroke();
  }
  if (state.selected) {
    ctx.strokeStyle = '#ff3d7b'; ctx.lineWidth = 3 / scale;
    if (state.selected.geometry.type === 'Point') {
      const [x, y] = state.selected.geometry.coordinates;
      ctx.beginPath(); ctx.arc(x, y, 8 / scale, 0, Math.PI * 2); ctx.stroke();
    } else for (const part of groups.get(state.selected.id) || []) ctx.stroke(part.path);
  }
  if ($('layer-trails').checked) {
    for (const drone of liveDrones) {
      const trail = drone.trail || [];
      if (trail.length < 2) continue;
      ctx.save(); ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      const sections = Math.min(8, trail.length - 1);
      for (let section = 0; section < sections; section++) {
        const first = Math.floor(section * (trail.length - 1) / sections);
        const last = Math.floor((section + 1) * (trail.length - 1) / sections);
        ctx.beginPath(); ctx.moveTo(trail[first][1], trail[first][2]);
        for (let i = first + 1; i <= last; i++) ctx.lineTo(trail[i][1], trail[i][2]);
        ctx.globalAlpha = .25 + .7 * (section + 1) / sections;
        ctx.strokeStyle = '#fff'; ctx.lineWidth = 5 / scale; ctx.stroke();
        ctx.strokeStyle = drone.color; ctx.lineWidth = 3 / scale; ctx.stroke();
      }
      ctx.restore();
    }
  }
  if ($('layer-drones').checked) {
    liveDrones.forEach((drone, index) => {
      const [x, y] = drone.position_m;
      ctx.save(); ctx.translate(x, y); ctx.scale(1 / scale, 1 / scale);
      ctx.beginPath();
      if (Number.isFinite(drone.course_rad)) {
        // 地图 X 向右、Y 向下，箭头沿实际水平速度方向旋转。
        ctx.rotate(drone.course_rad);
        ctx.moveTo(11, 0); ctx.lineTo(-7, -6); ctx.lineTo(-4, 0); ctx.lineTo(-7, 6); ctx.closePath();
      } else {
        ctx.arc(0, 0, 6, 0, Math.PI * 2);
      }
      ctx.fillStyle = drone.color; ctx.strokeStyle = '#fff'; ctx.lineWidth = 2; ctx.fill(); ctx.stroke(); ctx.restore();
      const labelX = x + 14 / scale, labelY = y + (index * 18 - 10) / scale;
      ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(labelX, labelY); ctx.strokeStyle = drone.color; ctx.lineWidth = 1 / scale; ctx.stroke();
      ctx.font = `600 ${14 / scale}px "Microsoft YaHei", sans-serif`; ctx.textAlign = 'left';
      ctx.strokeStyle = '#fff'; ctx.lineWidth = 4 / scale; ctx.lineJoin = 'round'; ctx.strokeText(drone.name, labelX, labelY);
      ctx.fillStyle = drone.color; ctx.fillText(drone.name, labelX, labelY);
    });
  }
}
function zoom(factor, x = state.width / 2, y = state.height / 2) {
  const wx = (x - state.ox) / state.scale, wy = (y - state.oy) / state.scale;
  state.scale = Math.max(.04, Math.min(30, state.scale * factor));
  state.ox = x - wx * state.scale; state.oy = y - wy * state.scale; schedule();
}
function worldAt(event) {
  const box = canvas.getBoundingClientRect();
  return [(event.clientX - box.left - state.ox) / state.scale, (event.clientY - box.top - state.oy) / state.scale];
}
function hitAt(x, y) {
  const radius = 8 / state.scale;
  const point = pointRecords.filter(visiblePoint).map(f => ({f, distance: Math.hypot(f.geometry.coordinates[0] - x, f.geometry.coordinates[1] - y)}))
    .filter(p => p.distance < radius).sort((a, b) => a.distance - b.distance)[0];
  if (point) return point.f;
  if (!$('layer-zones').checked) return null;
  const category = $('category').value;
  const candidates = zoneIndex.query(x, y).filter(p => (!category || p.feature.properties.category === category) &&
    x >= p.bounds[0] && x <= p.bounds[2] && y >= p.bounds[1] && y <= p.bounds[3]);
  // Smaller facilities are selected before the containing parcel.
  candidates.sort((a, b) => (a.feature.properties.area_m2 || Infinity) - (b.feature.properties.area_m2 || Infinity));
  ctx.save(); ctx.setTransform(1, 0, 0, 1, 0, 0);
  const hit = candidates.find(p => ctx.isPointInPath(p.path, x, y, 'evenodd'));
  ctx.restore(); return hit?.feature || null;
}
const statusNames = {static_verified: '静态核验通过', reference_only: '仅定位参考'};
function showFeature(feature, center = false) {
  state.selected = feature; const target = $('object-detail'); target.replaceChildren();
  $('export').disabled = !feature;
  if (!feature) { target.textContent = '此处没有可选功能区或可见任务点。'; schedule(); return; }
  const title = document.createElement('strong'); title.textContent = feature.properties.name; target.append(title);
  const id = document.createElement('span'); id.textContent = feature.id; target.append(id);
  const fields = {category: '类型', subtype: '二级标签', environment: '环境', area_m2: '面积 m²',
    point_type: '点位用途', position_status: '核验状态', execution_enabled: '静态执行',
    xyz_m: 'UE XYZ（米）', label_xy_m: '标签 XY（米）', region_id: '所属区域', host_region_id: '所在街区'};
  const dl = document.createElement('dl');
  for (const [key, label] of Object.entries(fields)) {
    let value = feature.properties[key];
    if (value === undefined || value === null) continue;
    if (key === 'category') value = categoryNames.get(value) || value;
    if (key === 'position_status') value = statusNames[value] || value;
    if (typeof value === 'boolean') value = value ? '是（仍需实时复核）' : '否';
    if (Array.isArray(value)) value = value.map(v => typeof v === 'number' ? v.toFixed(3) : v).join(', ');
    if (key === 'area_m2') value = Number(value).toFixed(1);
    const dt = document.createElement('dt'), dd = document.createElement('dd');
    dt.textContent = label; dd.textContent = String(value); dl.append(dt, dd);
  }
  target.append(dl);
  const nearby = feature.properties.nearby_graph_edge_ids;
  if (nearby?.length) {
    const note = document.createElement('p'); note.textContent = `15米内邻近道路分段 ${nearby.length} 条；此关联仅表示空间邻近。`; target.append(note);
  }
  const related = points.features.filter(p => p.properties.region_id === feature.id || p.properties.service_region_id === feature.id);
  if (related.length) {
    const container = document.createElement('div'); container.className = 'related';
    const label = document.createElement('b'); label.textContent = '关联任务点'; container.append(label);
    for (const p of related) container.append(resultButton(p));
    target.append(container);
  }
  if (center) {
    const q = feature.geometry.type === 'Point' ? feature.geometry.coordinates : feature.properties.label_xy_m;
    if (q) { state.scale = Math.max(state.scale, .7); state.ox = state.width / 2 - q[0] * state.scale; state.oy = state.height / 2 - q[1] * state.scale; }
  }
  schedule();
}
function resultButton(feature) {
  const button = document.createElement('button'); button.className = 'result'; button.textContent = feature.properties.name;
  const small = document.createElement('small'); small.textContent = feature.id; button.append(small);
  button.onclick = () => showFeature(feature, true); return button;
}
function exportSelection() {
  const data = state.selected, name = data?.id;
  if (!data) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2) + '\n'], {type: 'application/json'}));
  const a = document.createElement('a'); a.href = url; a.download = name.replace(/[<>:"/\\|?*]/g, '_') + '.json'; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

$('fit').onclick = fit; $('zoom-in').onclick = () => zoom(1.5); $('zoom-out').onclick = () => zoom(1 / 1.5);
$('category').onchange = rebuildZones; $('point-mode').onchange = schedule;
$('export').onclick = exportSelection;
$('search').oninput = () => {
  const query = $('search').value.trim().toLowerCase(); const target = $('results'); target.replaceChildren();
  if (!query || !ready) return;
  const matches = [...zones.features, ...points.features].filter(f => [f.id, f.properties.name].some(v => String(v).toLowerCase().includes(query)));
  for (const f of matches.slice(0, 30)) target.append(resultButton(f));
  if (!matches.length) target.textContent = '没有匹配对象。';
};
for (const layer of ['base', 'zones', 'roads', 'drones', 'trails']) $(`layer-${layer}`).onchange = schedule;
$('layer-buildings').onchange = async () => {
  if ($('layer-buildings').checked) {
    try { await ensureBuildings(); } catch (error) { $('layer-buildings').checked = false; $('object-detail').textContent = error.message; }
  }
  schedule();
};
canvas.addEventListener('wheel', event => {
  event.preventDefault(); const box = canvas.getBoundingClientRect();
  const pixels = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? state.height : 1);
  zoom(Math.exp(-Math.max(-240, Math.min(240, pixels)) * .0018), event.clientX - box.left, event.clientY - box.top);
}, {passive: false});
canvas.onpointerdown = event => {
  state.drag = {x: event.clientX, y: event.clientY, ox: state.ox, oy: state.oy, moved: false}; canvas.setPointerCapture(event.pointerId);
};
canvas.onpointermove = event => {
  const drag = state.drag;
  if (drag) {
    const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
    if (Math.hypot(dx, dy) > 3) drag.moved = true;
    if (drag.moved) { state.ox = drag.ox + dx; state.oy = drag.oy + dy; schedule(); }
  }
  const [x, y] = worldAt(event); $('coords').textContent = `X ${x.toFixed(2)} · Y ${y.toFixed(2)} m`;
};
canvas.onpointerup = event => {
  const drag = state.drag; state.drag = null;
  if (ready && drag && !drag.moved) showFeature(hitAt(...worldAt(event)));
};
canvas.onpointercancel = () => { state.drag = null; };
window.addEventListener('resize', () => { resize(); schedule(); });

async function initialize() {
  [zones, points, roads] = await Promise.all([
    loadJSON('functional_zones.json'), loadJSON('task_points.json'),
    loadJSON('road_network.json')
  ]);
  if (![zones, points].every(data => Array.isArray(data?.features)) || !Array.isArray(zones?.categories)) {
    throw new Error('标定 JSON 缺少 features 或 categories 数组');
  }
  if (!Array.isArray(roads?.edges)) throw new Error('road_network.json 缺少 edges 数组');
  await background.decode();
  for (const category of zones.categories) {
    colors.set(category.code, category.color); categoryNames.set(category.code, category.name);
    const option = document.createElement('option'); option.value = category.code; option.textContent = category.name; $('category').append(option);
  }
  CityMapVisuals.legend(zones.categories, $('legend'));
  $('loading').textContent = '正在建立地图缓存…'; await prepareGeometry();
  const roadCount = roads.edges.filter(edge => edge.kind === 'road').length;
  const connectionCount = roads.edges.filter(edge => edge.kind !== 'road' && edge.enabled !== false).length;
  $('zone-stats').textContent = `${zones.features.length} 个功能区 / 设施 · ${points.features.length} 个点位\n${roadCount.toLocaleString()} 条道路分段 · ${connectionCount.toLocaleString()} 条已启用连接`;
  const verifiedPoints = points.features.filter(point => point.properties.execution_enabled === true).length;
  $('point-mode').options[0].textContent = `仅静态核验点（${verifiedPoints}）`;
  $('point-mode').options[1].textContent = `全部点位（${points.features.length}，含定位参考）`;
  ready = true;
  for (const id of ['search', 'category', 'point-mode', 'layer-buildings']) $(id).disabled = false;
  $('loading').hidden = true; fit();
}

background = new Image(); background.src = './assets/town12.png'; background.onload = schedule;
background.onerror = () => { $('loading').textContent = '无法读取 assets/town12.png，请保留完整的 web 文件夹。'; };
window.liveMap = {
  clearTrails() {
    liveDrones = liveDrones.map(drone => ({...drone, trail: []}));
    schedule();
  },
  setDrones(records) {
    liveDrones = records.filter(drone => Array.isArray(drone.position_m) && drone.position_m.length === 3 && drone.position_m.every(Number.isFinite));
    schedule();
  },
  isReady: () => ready,
  centerDrone(name) {
    const drone = liveDrones.find(record => record.name === name);
    if (!drone || !ready) return;
    state.scale = Math.max(state.scale, 6);
    state.ox = state.width / 2 - drone.position_m[0] * state.scale;
    state.oy = state.height / 2 - drone.position_m[1] * state.scale;
    schedule();
  }
};
fit();

if (location.protocol === 'file:') {
  $('loading').textContent = '请先启动本地 HTTP 服务，再打开终端显示的地址。';
} else {
  initialize().catch(error => {
    $('loading').hidden = false; $('loading').textContent = error.message + '；请检查 Map 文件夹，再刷新页面。';
  });
}
