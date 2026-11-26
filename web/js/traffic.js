'use strict';

(() => {
  const labels = {preparing: '正在调度三机', tracking: '协同追踪中', returning: '无人机正在返航',
    complete: '追踪完成', failed: '追踪未完成', stopped: '追踪已结束'};
  const roleNames = {lead: '主追机', scout: '前哨机', wing: '侧翼机'};
  const following = {lead: '正在跟随', scout: '已到前方', wing: '侧方观察'};
  const phases = {waiting: '待入场', joining: '正在入场', approaching: '正在入场',
    returning: '正在返航', complete: '已归位'};
  const panel = document.getElementById('fleet-pursuit');
  const heading = panel.querySelector('[data-pursuit-label]');
  const image = panel.querySelector('[data-pursuit-image]');
  const box = panel.querySelector('[data-pursuit-box]');
  const caption = panel.querySelector('[data-pursuit-camera]');
  const empty = panel.querySelector('[data-pursuit-empty]');
  const viewButtons = panel.querySelectorAll('[data-pursuit-view]');

  let current = null, pollTimer = 0, polling = false;
  let frameTimer = 0, framePending = false, selectedRole = 'lead', drawnFrame = null;
  const frames = new Map();
  const active = value => value && !['complete', 'failed', 'stopped'].includes(value.status);

  function renderPursuit() {
    panel.hidden = !current;
    if (!current) return;
    let title = labels[current.status];
    if (current.status === 'tracking' && current.target?.intercept_state === 'braking') title = '车辆正在减速停车';
    if (current.outcome === 'intercepted' && current.status === 'returning') title = '车辆已截停 · 无人机正在返航';
    if (current.outcome === 'intercepted' && current.status === 'complete') title = '追踪完成 · 车辆已截停';
    if (current.outcome === 'timeout' && current.status === 'returning') title = '追踪超时 · 无人机正在返航';
    if (current.outcome === 'timeout' && current.status === 'failed') title = '追踪超时 · 车辆未截停';
    if (heading.textContent !== title) heading.textContent = title;
    heading.title = current.status === 'failed' ? current.error || '' : '';
    const frame = frames.get(selectedRole);
    const view = roleNames[selectedRole] + '视角';
    empty.hidden = !!frame;
    empty.textContent = active(current) ? `等待${view}` : `${view}未生成画面`;
    image.hidden = !frame;
    box.hidden = !frame?.bbox;
    caption.hidden = !frame;
    if (frame) {
      if (drawnFrame !== frame) {
        image.getContext('2d').drawImage(frame.pixels, 0, 0, image.width, image.height);
        drawnFrame = frame;
      }
      caption.textContent = view + (frame.visible ? '' : ' · 目标暂时遮挡');
      if (frame.bbox) {
        const [left, top, right, bottom] = frame.bbox;
        Object.assign(box.style, {left: left * 100 + '%', top: top * 100 + '%',
          width: (right - left) * 100 + '%', height: (bottom - top) * 100 + '%'});
      }
    }
    for (const button of viewButtons) {
      const role = button.dataset.pursuitView;
      const phase = current.drone_states?.find(drone => drone.role === role)?.phase;
      button.setAttribute('aria-pressed', String(role === selectedRole));
      button.nextElementSibling.textContent = phase === 'following' ? following[role] : phases[phase] || '待入场';
    }
  }

  function update(value) {
    if (!value) return;
    if (current && current.pursuit_id === value.pursuit_id &&
        ((value.target?.last_seen_s ?? -1) < (current.target?.last_seen_s ?? -1) ||
         current.outcome === 'intercepted' && value.outcome !== 'intercepted')) return;
    const previousId = current?.pursuit_id;
    const previousTitle = heading.textContent;
    if (previousId !== value.pursuit_id) {
      selectedRole = 'lead';
      frames.clear();
      drawnFrame = null;
    }
    current = value;
    renderPursuit();
    if (previousId !== value.pursuit_id || previousTitle !== heading.textContent) {
      window.dispatchEvent(new CustomEvent('workspace:pursuit', {
        detail: {pursuit: value, title: heading.textContent}
      }));
    }
    window.liveMap?.setPursuit(active(value) ? value : null);
    if (active(value) && !polling && !pollTimer) pollTimer = setTimeout(poll, 220);
    if (!frameTimer && !framePending) frameTimer = setTimeout(pollFrame, 0);
  }

  async function pollFrame() {
    frameTimer = 0;
    if (!current || framePending) return;
    const role = selectedRole;
    const camera = current.drone_states?.find(drone => drone.role === role)?.name;
    if (!camera) return;
    framePending = true;
    const id = current.pursuit_id;
    const wasActive = active(current);
    try {
      const query = new URLSearchParams({pursuit: id, camera, after: String(frames.get(role)?.sequence || 0)});
      const response = await fetch('/api/llm/pursuit/frame?' + query, {signal: AbortSignal.timeout(2500)});
      if (response.status === 200) {
        const frame = await response.json();
        const pixels = new Image();
        pixels.src = 'data:image/jpeg;base64,' + frame.image;
        await pixels.decode();
        if (current?.pursuit_id === id && frame.camera === camera) {
          frames.set(role, {...frame, pixels});
          if (selectedRole === role) renderPursuit();
        }
      }
    } catch { /* 保留最新画面，下一轮重试。 */ }
    finally {
      framePending = false;
      if (current && (current.pursuit_id !== id || selectedRole !== role || (wasActive && !active(current)))) {
        frameTimer = setTimeout(pollFrame, 0);
      } else if (active(current)) frameTimer = setTimeout(pollFrame, 130);
    }
  }

  async function poll() {
    pollTimer = 0;
    if (!active(current) || polling) return;
    polling = true;
    try {
      const response = await fetch('/api/llm/pursuit', {cache: 'no-store', signal: AbortSignal.timeout(2500)});
      if (response.ok) {
        const data = await response.json();
        if (data.pursuit) update(data.pursuit);
      }
    } catch { /* 下一轮继续读取。 */ }
    finally {
      polling = false;
      if (active(current)) pollTimer = setTimeout(poll, 220);
    }
  }

  function clear() {
    const wasVisible = !!current;
    current = drawnFrame = null;
    frames.clear();
    clearTimeout(pollTimer);
    clearTimeout(frameTimer);
    pollTimer = frameTimer = 0;
    renderPursuit();
    window.liveMap?.setPursuit(null);
    if (wasVisible) window.dispatchEvent(new CustomEvent('workspace:pursuit', {detail: null}));
  }
  for (const button of viewButtons) button.onclick = () => {
    selectedRole = button.dataset.pursuitView;
    renderPursuit();
    if (!framePending) {
      clearTimeout(frameTimer);
      frameTimer = setTimeout(pollFrame, 0);
    }
  };
  window.pursuitView = {update, clear, get current() { return current; }};
  window.addEventListener('mission:reset', clear);

  const $ = id => document.getElementById(id);
  const names = {speeding: '超速行驶', wrong_way: '逆向行驶'};
  const timeLabel = seconds => seconds == null ? '— — : — —'
    : [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, Math.floor(seconds % 60)]
      .map(value => String(value).padStart(2, '0')).join(':');

  function sourcePanel(kind, title) {
    const panel = document.createElement('section'); panel.className = 'traffic-source';
    // 固定的界面结构；记录值在下方通过 textContent 写入。
    panel.innerHTML = '<div class="traffic-source-frame"></div>' +
      '<div class="traffic-source-info"><div><h5 class="traffic-source-title"></h5>' +
      '<div class="traffic-source-meta"><span></span><time></time></div></div>' +
      '<div class="traffic-source-reading"><div><strong class="traffic-speed">—</strong><small>km/h</small></div>' +
      '<span class="traffic-source-result"></span></div></div>';
    panel.querySelector('.traffic-source-title').textContent = title;
    const frame = panel.querySelector('.traffic-source-frame');
    const content = document.createElement(kind === 'uav' ? 'button' : 'div');
    content.className = `traffic-${kind}-visual`;
    if (kind === 'uav') {
      content.type = 'button'; content.disabled = true; content.setAttribute('aria-label', '放大无人机抓拍');
      content.innerHTML = '<span class="traffic-frame-wait">等待空中发现</span>';
    } else {
      content.innerHTML = '<svg viewBox="0 0 200 130" role="img" aria-label="路口观测位置示意">' +
        '<image class="traffic-probe-base" preserveAspectRatio="none"/>' +
        '<path class="traffic-probe-road"/><path class="traffic-probe-track"/>' +
        '<g class="traffic-probe-device"><path class="traffic-probe-sector"/><circle r="14"/>' +
        '<rect x="-5" y="-4" width="8" height="8" rx="1"/><path d="M3 -2 L7 -4 L7 4 L3 2 Z"/></g>' +
        '<g class="traffic-probe-car"><circle r="9"/><rect x="-5" y="-3" width="10" height="6" rx="2"/></g></svg>';
      content.querySelector('.traffic-probe-base').setAttribute('href', new URL('./assets/city-map.png', document.baseURI).href);
    }
    frame.append(content);
    return {panel, content, speed: panel.querySelector('.traffic-speed'),
      result: panel.querySelector('.traffic-source-result'), device: panel.querySelector('.traffic-source-meta span'),
      time: panel.querySelector('time')};
  }

  function createCard(id) {
    const card = document.createElement('article'); card.className = 'traffic-target-card';
    card.innerHTML = '<header class="traffic-card-header"><div class="traffic-card-identity"><h4 class="traffic-card-plate"></h4>' +
      '<span class="traffic-card-zone"></span><span class="traffic-card-state"></span>' +
      '<span class="traffic-card-violations"></span></div></header>';
    const sources = document.createElement('div'); sources.className = 'traffic-sources';
    const uav = sourcePanel('uav', '无人机抓拍');
    const probe = sourcePanel('probe', '路口位置示意');
    const empty = document.createElement('p'); empty.className = 'traffic-sources-empty'; empty.textContent = '暂无取证记录';
    sources.append(uav.panel, probe.panel, empty);
    const actions = document.createElement('div'); actions.className = 'traffic-card-actions';
    const locate = document.createElement('button'); locate.type = 'button'; locate.textContent = '地图定位';
    const expand = document.createElement('button'); expand.type = 'button'; expand.textContent = '放大取证';
    const escape = document.createElement('button'); escape.type = 'button'; escape.textContent = '启动逃逸';
    escape.dataset.escapeTarget = id;
    escape.onclick = () => void window.vehiclePursuit?.escape(id);
    const follow = document.createElement('button'); follow.type = 'button'; follow.textContent = '开始追踪';
    follow.dataset.pursuitTarget = id;
    follow.onclick = () => void window.vehiclePursuit?.start(id);
    const menu = document.createElement('details'); menu.className = 'traffic-action-menu';
    menu.hidden = !id;
    const menuLabel = document.createElement('summary'); menuLabel.textContent = '操作';
    const operations = document.createElement('div'); operations.append(escape, follow);
    menu.append(menuLabel, operations);
    operations.addEventListener('click', event => {
      if (!event.target.closest('button')) return;
      menu.open = false;
      menuLabel.focus({preventScroll: true});
    });
    actions.append(locate, expand, menu);
    card.querySelector('.traffic-card-header').append(actions);
    card.append(sources);
    const view = {card, uav, probe, empty, locate, expand, escape, follow,
      state: card.querySelector('.traffic-card-state'), plate: card.querySelector('.traffic-card-plate'),
      zone: card.querySelector('.traffic-card-zone'), violations: card.querySelector('.traffic-card-violations'), confirmedAt: null};
    expand.onclick = () => {
      const copy = card.cloneNode(true); copy.classList.add('traffic-evidence-expanded');
      copy.querySelector('.traffic-card-actions').remove();
      if (view.confirmedAt != null) {
        const confirmation = document.createElement('time'); confirmation.className = 'traffic-confirmation';
        confirmation.textContent = `确认于 ${timeLabel(view.confirmedAt)}`;
        copy.querySelector('.traffic-card-identity').append(confirmation);
      }
      copy.querySelector('.traffic-uav-visual').onclick = uav.content.onclick;
      $('llm-image-dialog').querySelector('h2').textContent = '空地联合取证';
      $('llm-image-detail').replaceChildren(copy); $('llm-image-dialog').showModal();
    };
    return view;
  }

  function updateSource(view, event, device) {
    view.panel.hidden = !event;
    if (!event) return;
    view.speed.textContent = event.speed_kmh.toFixed(0);
    view.result.textContent = event.violations.map(v => names[v]).join(' · ');
    view.device.textContent = device;
    view.time.textContent = timeLabel(event.t);
  }

  function updateProbe(view, record, event) {
    const svg = view.content.querySelector('svg');
    const local = record.local_probes.find(p => p.id === event.probe_id);
    const origin = event.position_m || local?.xy_m;
    if (origin) {
      const trail = record.trail || [];
      const points = [...trail, origin, ...(record.position_m ? [record.position_m] : [])];
      const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
      const minX = Math.min(...xs), minY = Math.min(...ys), maxX = Math.max(...xs), maxY = Math.max(...ys);
      const scale = Math.min(150 / (maxX - minX + 30), 85 / (maxY - minY + 30));
      const project = p => [100 + (p[0] - (minX + maxX) / 2) * scale, 65 + (p[1] - (minY + maxY) / 2) * scale];
      const base = svg.querySelector('.traffic-probe-base'), corner = project([-1000, -3000]);
      for (const [key, value] of Object.entries({x: corner[0], y: corner[1], width: 4000 * scale, height: 4000 * scale})) {
        base.setAttribute(key, value);
      }
      const path = trail.map((p, i) => `${i ? 'L' : 'M'}${project(p).join(' ')}`).join(' ');
      for (const line of svg.querySelectorAll('path.traffic-probe-road, path.traffic-probe-track')) line.setAttribute('d', path);
      svg.querySelector('.traffic-probe-device').setAttribute('transform', `translate(${project(origin).join(' ')}) rotate(${local?.yaw_deg || 0})`);
      const reach = Math.min(60, (local?.far_m || 40) * scale), angle = (local?.half_fov_deg || 20) * Math.PI / 180;
      const dx = reach * Math.cos(angle), dy = reach * Math.sin(angle);
      svg.querySelector('.traffic-probe-sector').setAttribute('d', `M0 0 L${dx} ${-dy} A${reach} ${reach} 0 0 1 ${dx} ${dy} Z`);
      const car = svg.querySelector('.traffic-probe-car'); car.style.display = record.position_m ? '' : 'none';
      if (record.position_m) car.setAttribute('transform', `translate(${project(record.position_m).join(' ')})`);
    }
  }

  function updateCapture(view, proof, event) {
    if (!proof) {
      if (view.content.dataset.image) {
        delete view.content.dataset.image; view.content.disabled = true; view.content.onclick = null;
        view.content.innerHTML = '<span class="traffic-frame-wait"></span>';
      }
      const wait = view.content.querySelector('.traffic-frame-wait');
      if (wait) wait.textContent = event ? '抓拍整理中' : '等待空中发现';
      return;
    }
    if (view.content.dataset.image === proof.image_url) return;
    view.content.dataset.image = proof.image_url; view.content.disabled = false;
    const image = document.createElement('img'); image.src = proof.image_url; image.alt = '无人机超速车辆抓拍';
    const [x1, y1, x2, y2] = proof.detections[0].bbox;
    const zoom = Math.min(8, Math.max(1, 450 / Math.max(x2 - x1, y2 - y1)));
    image.style.width = `${zoom * 100}%`;
    image.style.transform = `translate(-${(x1 + x2) / 20}%, -${(y1 + y2) / 20}%)`;
    view.content.replaceChildren(image);
    view.content.onclick = () => {
      const frame = document.createElement('div'); frame.className = 'llm-anomaly-image';
      const photo = document.createElement('img'); photo.src = proof.image_url; photo.alt = '无人机车辆抓拍全景';
      const box = document.createElement('div'); box.className = 'llm-anomaly-box';
      Object.assign(box.style, {left: `${x1 / 10}%`, top: `${y1 / 10}%`, width: `${(x2 - x1) / 10}%`, height: `${(y2 - y1) / 10}%`});
      frame.append(photo, box); $('llm-image-detail').replaceChildren(frame);
      $('llm-image-dialog').querySelector('h2').textContent = '空中抓拍';
      if (!$('llm-image-dialog').open) $('llm-image-dialog').showModal();
    };
  }

  window.createTrafficEvidence = id => {
    const view = createCard(id);
    return {element: view.card, update(entry) {
      const record = entry.record;
      const uav = record.events.find(event => event.source === 'uav');
      const probe = record.events.find(event => event.source === 'probe');
      view.empty.hidden = !!(uav || probe);
      view.expand.disabled = !view.empty.hidden;
      view.confirmedAt = record.confirmed_at;
      view.card.dataset.status = record.status || '';
      view.state.textContent = entry.status;
      view.plate.textContent = entry.title;
      view.zone.textContent = entry.where;
      view.violations.textContent = [...new Set(entry.events.flatMap(event => event.violations))]
        .map(type => names[type]).filter(Boolean).join(' · ');
      view.locate.disabled = !entry.position;
      view.locate.onclick = () => {
        if (!window.liveMap?.centerTraffic(entry.target_id)) window.liveMap?.centerEvidence(entry.position);
      };
      view.escape.disabled = !window.vehiclePursuit?.online || !!window.vehiclePursuit?.busy;
      view.follow.disabled = view.escape.disabled || !!window.vehiclePursuit?.active;
      updateSource(view.uav, uav, record.drone_id || '空中巡查');
      updateSource(view.probe, probe, probe?.probe_id || '道路监控');
      if (probe) updateProbe(view.probe, record, probe);
      updateCapture(view.uav, entry.alert, uav);
    }};
  };
  $('llm-image-dialog').addEventListener('close', () => {
    $('llm-image-dialog').querySelector('h2').textContent = '异常照片';
  });
})();
