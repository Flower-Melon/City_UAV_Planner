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
      // Routing connections cross lanes; they are not physical road markings.
      if (edge.kind !== 'road' || edge.enabled === false || edge.length_m === 0) continue;
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
          c.fillStyle = color + '26'; c.strokeStyle = color + '94';
          c.fill(part.path, 'evenodd'); c.stroke(part.path);
        }
      }
      if (layers.roads) {
        c.strokeStyle = '#b4c8d4a8'; c.lineWidth = .8 / scale;
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
  selected: null, drag: null, frame: 0, baseDirty: true};
let zones, points, roads, background, buildings, probes;
let probeRecords = [];
let zoneParts = [], pointRecords = [], colors = new Map(), categoryNames = new Map();
let ready = false, buildingRequest;
let liveDrones = [];
let droneSession = null;
let locatedDrone = null;
let abnormalTargets = [], abnormalSelected = null, pickHandler = null, pendingPoint = null;
let abnormalRecords, abnormalSession;
let trafficTargets = [], missionZones = [];
let pursuitSnapshot = null, pursuitFollowing = true, pursuitLocated = false, pursuitVehicle = null;
const markerCanvas = document.createElement('canvas'); markerCanvas.className = 'map-markers';
markerCanvas.setAttribute('aria-hidden', 'true'); canvas.after(markerCanvas);
const markerContext = markerCanvas.getContext('2d');
const droneDelay = 240, targetDelay = 340;
const droneTrailMs = 10_000, droneTrailFadeMs = 680, droneLocateMs = 2200;
const abnormalColors = {vehicle: '#df542f', armed_person: '#914ac5', patient: '#138b80'};
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
      const record = {feature, bounds: boundsOf(rings[0]), path: polygonPath(rings),
        exterior: rings[0], spawnPath: polygonPath([rings[0]])};
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
    buildings = await loadJSON('objects/buildings.json');
    if (!Array.isArray(buildings.features)) throw new Error('buildings.json 缺少 features 数组');
    await visuals.setBuildings(buildings.features, polygonPath);
  })().catch(error => { buildingRequest = null; throw error; });
  await buildingRequest;
}

function resize() {
  const box = canvas.getBoundingClientRect();
  const ratio = Math.min(2, devicePixelRatio || 1);
  if (box.width === state.width && box.height === state.height && ratio === state.ratio) return;
  state.ox += (box.width - state.width) / 2;
  state.oy += (box.height - state.height) / 2;
  state.width = box.width; state.height = box.height;
  state.ratio = ratio;
  canvas.width = Math.round(box.width * state.ratio); canvas.height = Math.round(box.height * state.ratio);
  markerCanvas.width = canvas.width; markerCanvas.height = canvas.height;
}
function fit() {
  resize();
  state.scale = Math.min(state.width, state.height) / 4200;
  state.ox = (state.width - worldSize * state.scale) / 2 + 1000 * state.scale;
  state.oy = (state.height - worldSize * state.scale) / 2 + 3000 * state.scale;
  schedule();
}
function schedule() {
  state.baseDirty = true;
  scheduleMarkers();
}
function scheduleMarkers() {
  if (!state.frame) state.frame = requestAnimationFrame(drawFrame);
}
function visiblePoint(point) {
  const mode = $('point-mode').value;
  return mode === 'all' || (mode === 'verified' && point.properties.execution_enabled === true);
}
function addMarkerSample(record, previous, angle, at, delay, historyMs) {
  const sample = {at, position: record.position_m, angle};
  const last = previous?.samples.at(-1);
  const distance = last ? Math.hypot(last.position[0] - sample.position[0],
    last.position[1] - sample.position[1]) : 0;
  if (!last || at - last.at > 1000 || distance > 60) {
    return {...record, samples: [sample], movingUntil: 0};
  }
  // 保留一个窗口起点之前的实测点，便于在拖尾起点插值。
  const first = historyMs === undefined ? -7
    : Math.max(0, previous.samples.findLastIndex(point => point.at <= at - historyMs));
  const moved = distance > 0 || last.angle !== angle;
  return {...record, samples: [...previous.samples.slice(first), sample],
    movingUntil: moved ? at + delay : previous.movingUntil};
}
function sampledMarkers(records, previous, id, heading, delay, historyMs) {
  const now = performance.now();
  const byId = new Map(previous.map(record => [record[id], record]));
  return records.filter(record => Array.isArray(record.position_m) &&
    record.position_m.length === 3 && record.position_m.every(Number.isFinite))
    .map(record => {
      const last = byId.get(record[id]);
      if (record.updated_at !== undefined && record.updated_at === last?.updated_at) {
        return {...last, ...record};
      }
      return addMarkerSample(record, last, record[heading], now, delay, historyMs);
    });
}
function markerAt(record, now, delay) {
  const time = now - delay;
  const samples = record.samples;
  const i = samples.findIndex(sample => sample.at > time);
  if (i <= 0) return i === 0 ? samples[0] : samples.at(-1);
  const a = samples[i - 1], b = samples[i];
  const t = (time - a.at) / (b.at - a.at);
  const difference = b.angle - a.angle;
  const angle = Number.isFinite(a.angle) && Number.isFinite(b.angle)
    ? a.angle + Math.atan2(Math.sin(difference), Math.cos(difference)) * t : b.angle;
  return {position: a.position.map((value, axis) => value + (b.position[axis] - value) * t), angle};
}
function drawBase() {
  const {ratio, scale, ox, oy, width, height} = state;
  ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.setTransform(ratio * scale, 0, 0, ratio * scale, ratio * ox, ratio * oy);
  if ($('layer-base').checked && background?.complete && background.naturalWidth) {
    ctx.filter = 'brightness(.63) saturate(.55)';
    ctx.drawImage(background, -1000, -3000, worldSize, worldSize);
    ctx.filter = 'none';
  }
  if (!ready) return;
  visuals.draw(ctx, state, {
    zones: $('layer-zones').checked, category: $('category').value,
    buildings: $('layer-buildings').checked, roads: $('layer-roads').checked
  });
  if ($('layer-zones').checked) {
    ctx.font = `600 ${14 / scale}px system-ui, "Microsoft YaHei", sans-serif`;
    ctx.textAlign = 'center'; ctx.lineWidth = 4 / scale; ctx.lineJoin = 'round';
    for (const f of zones.features) {
      if (f.properties.object_type !== 'facility' || ($('category').value && f.properties.category !== $('category').value)) continue;
      const q = f.properties.label_xy_m;
      if (!q || q[0] * scale + ox < -80 || q[0] * scale + ox > width + 80 || q[1] * scale + oy < -20 || q[1] * scale + oy > height + 20) continue;
      ctx.strokeStyle = '#142530'; ctx.fillStyle = '#e8f0f1';
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
  drawProbes();
  ctx.lineWidth = 2 / scale;
  for (const id of missionZones) {
    for (const part of groups.get(id) || []) {
      ctx.fillStyle = '#6de1bc20'; ctx.strokeStyle = '#6de1bca6';
      ctx.fill(part.path, 'evenodd'); ctx.stroke(part.path);
    }
  }
  if (state.selected && (state.selected.properties.object_type !== 'traffic_probe' || $('layer-probes').checked)) {
    ctx.strokeStyle = '#83f7d2'; ctx.lineWidth = 3 / scale;
    if (state.selected.geometry.type === 'Point') {
      const [x, y] = state.selected.geometry.coordinates;
      ctx.beginPath(); ctx.arc(x, y, 8 / scale, 0, Math.PI * 2); ctx.stroke();
    } else for (const part of groups.get(state.selected.id) || []) ctx.stroke(part.path);
  }
}
function droneTrailActive(drone, now) {
  return drone.airborne === true && drone.movingUntil > 0 && now < drone.movingUntil + droneTrailFadeMs;
}
function drawDroneTrail(context, drone, now, head) {
  const end = now - droneDelay, start = end - droneTrailMs;
  const fade = Math.min(1, (drone.movingUntil + droneTrailFadeMs - now) / droneTrailFadeMs);
  let {position: [x, y]} = markerAt(drone, now - droneTrailMs, droneDelay);
  let previousAt = start;
  context.save();
  context.strokeStyle = drone.color; context.lineWidth = 3 / state.scale; context.lineCap = 'round';
  function strokeTo([px, py], at) {
    if (px !== x || py !== y) {
      const progress = ((previousAt + at) / 2 - start) / droneTrailMs;
      context.globalAlpha = fade * (.13 + .69 * progress ** 1.4);
      context.beginPath(); context.moveTo(x, y); context.lineTo(px, py); context.stroke();
      x = px; y = py;
    }
    previousAt = at;
  }
  for (const sample of drone.samples) {
    if (sample.at >= end) break;
    if (sample.at > start) strokeTo(sample.position, sample.at);
  }
  strokeTo(head, end);
  context.restore();
}
function drawDrones(context, now) {
  const {scale} = state;
  const visible = liveDrones.filter(drone => drone.airborne === true ||
    (drone.name === locatedDrone?.name && now < locatedDrone.until));
  const poses = visible.map(drone => drone.airborne === true
    ? markerAt(drone, now, droneDelay) : {position: drone.position_m});
  visible.forEach((drone, index) => {
    if (droneTrailActive(drone, now)) drawDroneTrail(context, drone, now, poses[index].position);
  });
  visible.forEach((drone, index) => {
    const {position: [x, y], angle} = poses[index];
    context.save(); context.translate(x, y); context.scale(1 / scale, 1 / scale);
    context.beginPath();
    if (Number.isFinite(angle)) {
      // 地图 X 向右、Y 向下，箭头沿实际水平速度方向旋转。
      context.rotate(angle);
      context.moveTo(11, 0); context.lineTo(-7, -6); context.lineTo(-4, 0); context.lineTo(-7, 6); context.closePath();
    } else {
      context.arc(0, 0, 6, 0, Math.PI * 2);
    }
    context.fillStyle = drone.color; context.strokeStyle = '#fff'; context.lineWidth = 2; context.fill(); context.stroke(); context.restore();
    const labelX = x + 14 / scale, labelY = y + (index * 18 - 10) / scale;
    context.beginPath(); context.moveTo(x, y); context.lineTo(labelX, labelY); context.strokeStyle = drone.color; context.lineWidth = 1 / scale; context.stroke();
    context.font = `600 ${14 / scale}px "Microsoft YaHei", sans-serif`; context.textAlign = 'left';
    context.strokeStyle = '#fff'; context.lineWidth = 4 / scale; context.lineJoin = 'round'; context.strokeText(drone.name, labelX, labelY);
    context.fillStyle = drone.color; context.fillText(drone.name, labelX, labelY);
  });
}
function drawProbes() {
  if (!$('layer-probes').checked) return;
  const {scale, ox, oy, width, height} = state;
  for (const feature of probeRecords) {
    const p = feature.properties, [x, y] = feature.geometry.coordinates;
    const pad = p.far_m * scale + 16;
    if (x * scale + ox < -pad || x * scale + ox > width + pad || y * scale + oy < -pad || y * scale + oy > height + pad) continue;
    ctx.save(); ctx.translate(x, y); ctx.rotate(p.yaw_deg * Math.PI / 180);
    if ($('layer-probe-coverage').checked || state.selected?.id === feature.id) {
      const angle = p.half_fov_deg * Math.PI / 180;
      ctx.beginPath(); ctx.arc(0, 0, p.far_m, -angle, angle);
      ctx.arc(0, 0, p.near_m, angle, -angle, true); ctx.closePath();
      ctx.fillStyle = '#087c9826'; ctx.strokeStyle = '#087c98'; ctx.lineWidth = 1 / scale; ctx.fill(); ctx.stroke();
    }
    ctx.scale(1 / scale, 1 / scale);
    ctx.beginPath(); ctx.moveTo(6, 0); ctx.lineTo(-3, -3); ctx.lineTo(-3, 3); ctx.closePath();
    ctx.fillStyle = '#7ad2df'; ctx.strokeStyle = '#17313e'; ctx.lineWidth = 1; ctx.fill(); ctx.stroke();
    ctx.restore();
  }
}
function drawAbnormalTarget(context, target, now) {
  const {scale} = state;
  const color = abnormalColors[target.kind];
  const {position: [x, y], angle} = markerAt(target, now, targetDelay);
  context.save(); context.translate(x, y); context.scale(1 / scale, 1 / scale);
  if (target.id === abnormalSelected) {
    context.beginPath(); context.arc(0, 0, 14, 0, Math.PI * 2);
    context.fillStyle = '#ffffffbb'; context.fill(); context.strokeStyle = '#18334a'; context.lineWidth = 2; context.stroke();
  }
  context.fillStyle = color; context.strokeStyle = '#fff'; context.lineWidth = 2;
  context.beginPath();
  if (target.kind === 'vehicle') {
    context.rotate(angle || 0); context.rect(-8, -5, 16, 10); context.fill(); context.stroke();
    context.beginPath(); context.moveTo(8, 0); context.lineTo(3, -3); context.lineTo(3, 3); context.closePath(); context.fillStyle = '#fff'; context.fill();
  } else {
    context.moveTo(0, -9); context.lineTo(8, 0); context.lineTo(0, 9); context.lineTo(-8, 0); context.closePath(); context.fill(); context.stroke();
    context.fillStyle = '#fff'; context.font = 'bold 11px system-ui'; context.textAlign = 'center'; context.fillText(target.kind === 'patient' ? '+' : '!', 0, 4);
  }
  context.restore();
  context.font = `600 ${12 / scale}px system-ui`; context.textAlign = 'left'; context.lineJoin = 'round';
  const label = `${{vehicle: '异常车', armed_person: '持刀人员', patient: '病人'}[target.kind]} ${target.id.split('_').pop()}`;
  context.strokeStyle = '#fff'; context.lineWidth = 4 / scale; context.strokeText(label, x + 16 / scale, y - 8 / scale);
  context.fillStyle = color; context.fillText(label, x + 16 / scale, y - 8 / scale);
}
function drawAbnormal(context, now, tracked) {
  const {scale} = state;
  drawTraffic(context);
  for (const target of abnormalTargets) {
    if (target.id !== tracked?.id) drawAbnormalTarget(context, target, now);
  }
  if (pendingPoint) {
    const [x, y] = pendingPoint, r = 12 / scale;
    context.strokeStyle = '#df542f'; context.lineWidth = 2 / scale; context.setLineDash([4 / scale, 3 / scale]);
    context.beginPath(); context.arc(x, y, r, 0, Math.PI * 2); context.stroke(); context.setLineDash([]);
    context.beginPath(); context.moveTo(x - r, y); context.lineTo(x + r, y); context.moveTo(x, y - r); context.lineTo(x, y + r); context.stroke();
  }
}

function recentTrafficEvents(target) {
  return (target.recent_events || (target.events.length ? target.events : target.confirmed_evidence)).slice(-2);
}

function drawTraffic(context) {
  const {scale} = state;
  for (const target of trafficTargets) {
    // 仅保留最近两次记录点，不连接观测点，也不代替实时车辆。
    for (const event of recentTrafficEvents(target)) {
      const [x, y] = event.position_m;
      const probe = event.source === 'probe', color = probe ? '#ad803c' : '#bc4b43';
      context.save(); context.translate(x, y); context.scale(1 / scale, 1 / scale);
      context.beginPath(); context.arc(0, 0, 9, 0, Math.PI * 2);
      context.fillStyle = '#ffffffd9'; context.fill(); context.strokeStyle = color; context.lineWidth = 2; context.stroke();
      context.beginPath(); context.arc(0, 0, 3, 0, Math.PI * 2); context.fillStyle = color; context.fill();
      const label = probe ? '路口监测点' : '超速记录点';
      context.font = '600 11px "Microsoft YaHei", sans-serif'; context.textAlign = 'left'; context.lineJoin = 'round';
      context.strokeStyle = '#fff'; context.lineWidth = 4; context.strokeText(label, 13, 17);
      context.fillStyle = color; context.fillText(label, 13, 17);
      context.restore();
    }
  }
}

function trackedVehicle(now) {
  if (pursuitSnapshot?.status !== 'tracking') return null;
  const target = abnormalTargets.find(target => target.id === pursuitSnapshot.vehicle_id);
  if (!target) return pursuitVehicle;
  const last = target.samples.at(-1).at;
  return pursuitVehicle && now - last >= targetDelay && pursuitVehicle.samples.at(-1).at > last
    ? pursuitVehicle : target;
}
function setPursuit(value) {
  if (value?.pursuit_id !== pursuitSnapshot?.pursuit_id) {
    pursuitVehicle = null; pursuitLocated = false; pursuitFollowing = true;
  }
  pursuitSnapshot = value;
  const target = value?.target;
  if (target && (!pursuitVehicle || target.last_seen_s !== pursuitVehicle.last_seen_s)) {
    // 同一辆车优先用场景实时坐标，场景查询中断时可用追踪采样继续显示。
    pursuitVehicle = addMarkerSample({id: value.vehicle_id, kind: 'vehicle',
      position_m: target.position_m, last_seen_s: target.last_seen_s},
    pursuitVehicle, target.heading_rad, performance.now(), targetDelay);
  }
  scheduleMarkers();
}
function followTrackedVehicle([x, y]) {
  const {width, height, scale, ox, oy} = state;
  if (!pursuitLocated) {
    state.scale = Math.max(scale, .85); pursuitLocated = true;
    state.ox = width / 2 - x * state.scale;
    state.oy = height / 2 - y * state.scale;
    return true;
  }
  if (!pursuitFollowing || state.drag) return false;
  const dx = width / 2 - (x * scale + ox), dy = height / 2 - (y * scale + oy);
  if (Math.abs(dx) <= width * .16 && Math.abs(dy) <= height * .16) return false;
  state.ox += dx * .045; state.oy += dy * .045;
  return true;
}
function drawPursuitTrail(context, position) {
  const trail = pursuitSnapshot.trail_m || [];
  if (trail.length < 2) return;
  context.save(); context.beginPath(); context.moveTo(...trail[0]);
  for (let i = 1; i < trail.length; i++) context.lineTo(...trail[i]);
  context.lineTo(position[0], position[1]);
  context.strokeStyle = abnormalColors.vehicle; context.lineWidth = 1.5 / state.scale;
  context.lineCap = 'round'; context.lineJoin = 'round'; context.stroke(); context.restore();
}
function drawFrame(now) {
  state.frame = 0;
  const tracked = ready ? trackedVehicle(now) : null;
  const position = tracked && markerAt(tracked, now, targetDelay).position;
  const cameraMoved = position && followTrackedVehicle(position);
  if (state.baseDirty || cameraMoved) {
    state.baseDirty = false;
    drawBase();
  }
  const context = markerContext;
  context.setTransform(1, 0, 0, 1, 0, 0);
  context.clearRect(0, 0, markerCanvas.width, markerCanvas.height);
  if (!ready) return;
  const {ratio, scale, ox, oy} = state;
  context.setTransform(ratio * scale, 0, 0, ratio * scale, ratio * ox, ratio * oy);
  drawDrones(context, now);
  drawAbnormal(context, now, tracked);
  if (tracked) {
    drawPursuitTrail(context, position);
    drawAbnormalTarget(context, tracked, now);
  }
  if (cameraMoved || tracked?.movingUntil > now || liveDrones.some(drone => droneTrailActive(drone, now)) ||
      abnormalTargets.some(target => target.movingUntil > now)) scheduleMarkers();
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
// 与生成端保持一致：内部选所在街区，道路选最近街区，等距时按ID排序。
function spawnZoneAt([x, y]) {
  let best = null, nearest = Infinity;
  ctx.save(); ctx.setTransform(1, 0, 0, 1, 0, 0);
  for (const part of zoneParts) {
    if (part.feature.properties.category === 'transport') continue;
    let distance = 0;
    if (!ctx.isPointInPath(part.spawnPath, x, y)) {
      distance = Infinity;
      for (let i = 1; i < part.exterior.length; i++) {
        const [ax, ay] = part.exterior[i - 1], [bx, by] = part.exterior[i];
        const dx = bx - ax, dy = by - ay, length = dx * dx + dy * dy;
        const t = length ? Math.max(0, Math.min(1, ((x - ax) * dx + (y - ay) * dy) / length)) : 0;
        distance = Math.min(distance, (x - ax - t * dx) ** 2 + (y - ay - t * dy) ** 2);
      }
    }
    if (distance < nearest || distance === nearest && (!best || part.feature.id < best.id)) {
      nearest = distance; best = part.feature;
    }
  }
  ctx.restore();
  return best;
}

function hitAt(x, y) {
  const radius = 8 / state.scale;
  if ($('layer-probes').checked) {
    const probe = probeRecords.map(f => ({f, distance: Math.hypot(f.geometry.coordinates[0] - x, f.geometry.coordinates[1] - y)}))
      .filter(p => p.distance < 10 / state.scale).sort((a, b) => a.distance - b.distance)[0];
    if (probe) return probe.f;
  }
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
  window.dispatchEvent(new CustomEvent('map:selection', {detail: {feature}}));
  $('export').disabled = !feature;
  if (!feature) { target.textContent = '此处没有可选功能区、可见任务点或探头。'; schedule(); return; }
  const title = document.createElement('strong'); title.textContent = feature.properties.name; target.append(title);
  const id = document.createElement('span'); id.textContent = feature.id; target.append(id);
  const fields = {category: '类型', subtype: '二级标签', environment: '环境', area_m2: '面积 m²',
    point_type: '点位用途', position_status: '核验状态', execution_enabled: '静态执行',
    xyz_m: 'UE XYZ（米）', label_xy_m: '标签 XY（米）', region_id: '所属区域', host_region_id: '所在街区',
    junction: '所属路口', region: '地图分区', xy_m: 'UE XY（米）', yaw_deg: '朝向（度）',
    near_m: '感知近界（米）', far_m: '感知远界（米）', half_fov_deg: '半视场角（度）', ground_z_m: '地面高度（米）'};
  const dl = document.createElement('dl');
  for (const [key, label] of Object.entries(fields)) {
    let value = feature.properties[key];
    if (value === undefined || value === null) continue;
    if (key === 'category') value = categoryNames.get(value) || value;
    if (key === 'position_status') value = statusNames[value] || value;
    if (typeof value === 'boolean') value = value ? '是（仍需实时复核）' : '否';
    if (Array.isArray(value)) value = value.map(v => typeof v === 'number' ? v.toFixed(3) : v).join(', ');
    if (key === 'area_m2') value = Number(value).toFixed(1);
    const field = document.createElement('div');
    const dt = document.createElement('dt'), dd = document.createElement('dd');
    dt.textContent = label; dd.textContent = String(value);
    field.append(dt, dd); dl.append(field);
  }
  target.append(dl);
  if (feature.properties.object_type === 'traffic_probe') {
    const note = document.createElement('p');
    note.textContent = '箭头表示感知朝向，扇形表示逻辑感知范围。此处展示增量包布设信息，运行时是否启用尚未读取。';
    target.append(note);
  }
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
    if (q) { state.scale = Math.max(state.scale, feature.properties.object_type === 'traffic_probe' ? 2.5 : .7); state.ox = state.width / 2 - q[0] * state.scale; state.oy = state.height / 2 - q[1] * state.scale; }
  }
  schedule();
}
function resultButton(feature) {
  const button = document.createElement('button'); button.className = 'result'; button.textContent = feature.properties.name;
  const small = document.createElement('small'); small.textContent = feature.id; button.append(small);
  button.onclick = () => {
    if (feature.properties.object_type === 'traffic_probe') $('layer-probes').checked = true;
    showFeature(feature, true);
  }; return button;
}
function exportSelection() {
  const data = state.selected, name = data?.id;
  if (!data) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2) + '\n'], {type: 'application/json'}));
  const a = document.createElement('a'); a.href = url; a.download = name.replace(/[<>:"/\\|?*]/g, '_') + '.json'; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

$('fit').onclick = () => {
  pursuitFollowing = false; pursuitLocated = !!pursuitSnapshot;
  fit();
};
$('zoom-in').onclick = () => zoom(1.5); $('zoom-out').onclick = () => zoom(1 / 1.5);
$('category').onchange = rebuildZones; $('point-mode').onchange = schedule;
$('export').onclick = exportSelection;
$('search').oninput = () => {
  const query = $('search').value.trim().toLowerCase(); const target = $('results'); target.replaceChildren();
  if (!query || !ready) return;
  const matches = [...zones.features, ...points.features, ...probeRecords].filter(f => [f.id, f.properties.name, f.properties.junction].some(v => String(v || '').toLowerCase().includes(query)));
  for (const f of matches.slice(0, 30)) target.append(resultButton(f));
  if (!matches.length) target.textContent = '没有匹配对象。';
};
for (const layer of ['base', 'zones', 'roads', 'probes', 'probe-coverage']) $(`layer-${layer}`).onchange = schedule;
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
  if (event.button !== 0 || !event.isPrimary) return;
  pursuitFollowing = false;
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
  if (ready && drag && !drag.moved) {
    const point = worldAt(event);
    if (pickHandler) { pickHandler(point); return; }
    const now = performance.now();
    const tracked = trackedVehicle(now);
    const targets = tracked ? [...abnormalTargets.filter(target => target.id !== tracked.id), tracked] : abnormalTargets;
    const target = targets.map(target => {
      const [x, y] = markerAt(target, now, targetDelay).position;
      return {target, distance: Math.hypot(x - point[0], y - point[1])};
    })
      .filter(hit => hit.distance * state.scale < 15).sort((a, b) => a.distance - b.distance)[0]?.target;
    const evidenceHit = trafficTargets.find(record => target ? record.target_id === target.id
      : recentTrafficEvents(record).some(({position_m: [x, y]}) =>
        Math.hypot(x - point[0], y - point[1]) * state.scale < 13));
    const eventId = evidenceHit?.target_id || (target && target.id === pursuitSnapshot?.vehicle_id ? target.id : null);
    if (eventId && $('abnormal-panel').hidden)
      window.dispatchEvent(new CustomEvent('workspace:select-event', {detail: {id: eventId}}));
    else if (target) window.dispatchEvent(new CustomEvent('abnormal:select', {detail: {id: target.id}}));
    else showFeature(!$('abnormal-panel').hidden ? spawnZoneAt(point) : hitAt(...point));
  }
};
canvas.onpointercancel = () => { state.drag = null; };
window.addEventListener('resize', () => { resize(); schedule(); });
new ResizeObserver(() => { resize(); schedule(); }).observe(canvas);

async function initialize() {
  [zones, points, roads, probes] = await Promise.all([
    loadJSON('zones/functional_zones.json'), loadJSON('objects/task_points.json'),
    loadJSON('roads/road_network.json'), loadJSON('objects/traffic_probes.json')
  ]);
  if (![zones, points].every(data => Array.isArray(data?.features)) || !Array.isArray(zones?.categories)) {
    throw new Error('标定 JSON 缺少 features 或 categories 数组');
  }
  if (!Array.isArray(roads?.edges)) throw new Error('roads/road_network.json 缺少 edges 数组');
  if (!Array.isArray(probes?.probes)) throw new Error('objects/traffic_probes.json 缺少 probes 数组');
  probeRecords = probes.probes.map(p => ({id: p.id, geometry: {type: 'Point', coordinates: p.xy_m},
    properties: {...p, name: `交通探头 ${p.id}`, object_type: 'traffic_probe'}}));
  await background.decode();
  for (const category of zones.categories) {
    colors.set(category.code, category.color); categoryNames.set(category.code, category.name);
    const option = document.createElement('option'); option.value = category.code; option.textContent = category.name; $('category').append(option);
  }
  CityMapVisuals.legend(zones.categories, $('legend'));
  $('loading').textContent = '正在建立地图缓存…'; await prepareGeometry();
  const verifiedPoints = points.features.filter(point => point.properties.execution_enabled === true).length;
  $('point-mode').options[0].textContent = `仅静态核验点（${verifiedPoints}）`;
  $('point-mode').options[1].textContent = `全部点位（${points.features.length}，含定位参考）`;
  ready = true;
  for (const id of ['search', 'category', 'point-mode', 'layer-buildings']) $(id).disabled = false;
  $('loading').hidden = true; fit();
  window.dispatchEvent(new Event('map:ready'));
}

background = new Image(); background.src = './assets/town12.png'; background.onload = schedule;
background.onerror = () => { $('loading').textContent = '无法读取 assets/town12.png，请保留完整的 web 文件夹。'; };
window.liveMap = {
  setPursuit,
  followPursuit() { pursuitFollowing = true; pursuitLocated = false; if (pursuitSnapshot) setPursuit(pursuitSnapshot); },
  setTraffic(records) { trafficTargets = records; scheduleMarkers(); },
  setMissionZones(ids) {
    if (ids.length === missionZones.length && ids.every((id, index) => id === missionZones[index])) return;
    missionZones = ids;
    schedule();
  },
  centerTraffic(id) {
    const target = trafficTargets.find(t => t.target_id === id);
    if (!target || !ready) return;
    const vehicle = abnormalTargets.find(t => t.kind === 'vehicle' && t.id === id);
    const events = recentTrafficEvents(target);
    const position = vehicle && markerAt(vehicle, performance.now(), targetDelay).position;
    const points = [position, ...events.map(event => event.position_m)].filter(Boolean);
    if (!points.length) return;
    const bounds = CityMapVisuals.bounds(points);
    state.scale = Math.min(5, state.width / (bounds[2] - bounds[0] + 160), state.height / (bounds[3] - bounds[1] + 160));
    state.ox = state.width / 2 - (bounds[0] + bounds[2]) / 2 * state.scale;
    state.oy = state.height / 2 - (bounds[1] + bounds[3]) / 2 * state.scale;
    schedule();
  },
  setAbnormal(records, selectedId, session) {
    if (records !== abnormalRecords || session !== abnormalSession) {
      abnormalTargets = sampledMarkers(records, session === abnormalSession ? abnormalTargets : [],
        'id', 'yaw_rad', targetDelay);
      abnormalRecords = records;
      abnormalSession = session;
    }
    abnormalSelected = selectedId; scheduleMarkers();
  },
  setPickHandler(handler) { pickHandler = handler; canvas.classList.toggle('picking', !!handler); },
  spawnZoneAt,
  getZone: id => zones?.features.find(zone => zone.id === id),
  selectedZone() {
    const feature = state.selected;
    if (!feature) return null;
    return feature.geometry.type === 'Point' ? spawnZoneAt(feature.geometry.coordinates)
      : feature.properties.category !== 'transport' ? feature : null;
  },
  selectZone(id, center = false) {
    const feature = zones?.features.find(zone => zone.id === id);
    if (feature) showFeature(feature, center);
  },
  setPendingPoint(point) { pendingPoint = point; scheduleMarkers(); },
  centerTarget(id) {
    const target = abnormalTargets.find(target => target.id === id);
    if (!target || !ready) return;
    const [x, y] = markerAt(target, performance.now(), targetDelay).position;
    state.scale = Math.max(state.scale, 3);
    state.ox = state.width / 2 - x * state.scale;
    state.oy = state.height / 2 - y * state.scale;
    schedule();
  },
  setDrones(records, session = null) {
    const sameSession = session === droneSession;
    if (!sameSession) locatedDrone = null;
    liveDrones = sampledMarkers(records, sameSession ? liveDrones.filter(drone => drone.airborne === true) : [],
      'name', 'course_rad', droneDelay, droneTrailMs + droneDelay);
    droneSession = session;
    scheduleMarkers();
  },
  isReady: () => ready,
  centerDrone(name) {
    const drone = liveDrones.find(record => record.name === name);
    if (!drone || !ready) return;
    const now = performance.now();
    locatedDrone = drone.airborne === true ? null : {name, until: now + droneLocateMs};
    if (locatedDrone) setTimeout(scheduleMarkers, droneLocateMs);
    const [x, y] = drone.airborne === true ? markerAt(drone, now, droneDelay).position : drone.position_m;
    state.scale = Math.max(state.scale, 6);
    state.ox = state.width / 2 - x * state.scale;
    state.oy = state.height / 2 - y * state.scale;
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
