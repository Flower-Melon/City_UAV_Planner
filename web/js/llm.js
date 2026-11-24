'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const tabs = [...document.querySelectorAll('.sidebar-tabs [role="tab"]')];
  const form = $('llm-form'), input = $('llm-instruction'), submit = $('llm-submit');
  const followupForm = $('llm-followup-form'), followupInput = $('llm-followup-input');
  const followupSubmit = $('llm-followup-submit'), newTask = $('llm-new-task');
  const status = $('llm-status'), resultPanel = $('llm-result');
  const planZones = $('llm-queue'), planNote = $('llm-model-note'), planActions = $('llm-plan-actions');
  const taskLabels = {queued: '待派发', running: '执行中', analyzing: '分析中',
    completed: '已完成', failed: '执行失败', skipped: '未执行'};
  const monitorLabels = {queued: '等待分配', flying: '飞往区域中心', hovering: '高空悬停',
    relocated: '已转往后续区域', failed: '监控失败', skipped: '未执行'};
  const visionKinds = {patient: '医疗求助', armed_person: '持刀人员'};
  let displayedZones = '';
  const modelSelect = $('llm-model-select'), modelInfo = $('llm-model-info');
  const modelRetry = $('llm-model-retry');
  let modelBackend = null, modelPending = false;
  let pending = false, conversation = [], previousTurn = null;
  let needsClarification = false;
  let checking = true, flying = false, mission = null, pollTimer;
  let resetting = false, resetFailed = false, generation = 0, planningController = null;
  let visionMission = null;
  const shownAnomalies = new Map();
  let trafficMission = null;

  function annotatedImage(record) {
    const view = document.createElement('div'); view.className = 'llm-anomaly-image';
    const image = document.createElement('img');
    image.src = record.image_url; image.alt = record.description;
    view.append(image);
    for (const detection of record.detections) {
      const [x1, y1, x2, y2] = detection.bbox;
      const box = document.createElement('div'); box.className = 'llm-anomaly-box';
      Object.assign(box.style, {left: `${x1 / 10}%`, top: `${y1 / 10}%`,
        width: `${(x2 - x1) / 10}%`, height: `${(y2 - y1) / 10}%`});
      const label = document.createElement('span');
      label.textContent = visionKinds[detection.status] || '识别目标';
      box.dataset.align = x1 > 650 ? 'end' : 'start';
      box.dataset.labelPosition = y1 < 150 ? 'below' : 'above';
      box.title = detection.label || label.textContent;
      box.append(label); view.append(box);
    }
    return view;
  }

  function showVision(execution) {
    const vision = execution?.vision;
    const missionId = vision ? execution.mission_id : null;
    const container = $('llm-anomalies');
    if (visionMission !== missionId) {
      visionMission = missionId; shownAnomalies.clear();
      container.replaceChildren();
    }
    const error = vision?.errors?.[0]?.error;
    $('llm-vision-error').hidden = !error;
    $('llm-vision-error').textContent = error ? `部分目标识别或定位未完成：${error}` : '';
    if (!vision) return;
    const targets = vision.targets ?? (vision.anomalies || []).map(record =>
      ({target_id: `${record.task_id}:${record.image}`, first_observation: record, sightings: 1}));
    const targetIds = new Set(targets.map(target => target.target_id));
    for (const [key, entry] of shownAnomalies) {
      if (!targetIds.has(key)) { entry.card.remove(); shownAnomalies.delete(key); }
    }
    for (const [index, target] of targets.entries()) {
      const record = target.first_observation;
      const key = target.target_id;
      const zone = window.liveMap?.getZone(record.zone_id)?.properties.name || record.zone_id;
      const signature = JSON.stringify([record.task_id, record.image, record.detections,
        zone, target.person_id, target.sightings, target.location_error]);
      let entry = shownAnomalies.get(key);
      if (!entry) {
        const card = document.createElement('article'); card.className = 'llm-anomaly-card';
        entry = {card, signature: null}; shownAnomalies.set(key, entry);
      }
      const card = entry.card;
      if (container.children[index] !== card) container.insertBefore(card, container.children[index] || null);
      if (entry.signature === signature) continue;
      entry.signature = signature;
      const categories = [...new Set(record.detections.map(detection => visionKinds[detection.status]).filter(Boolean))];
      const title = categories.join('、') || '人员异常';
      const heading = document.createElement('div'); heading.className = 'event-person-heading';
      const label = document.createElement('h4'); label.textContent = title;
      const metadata = document.createElement('p');
      metadata.textContent = [zone, record.drone_id, target.person_id].filter(Boolean).join(' · ');
      heading.append(label, metadata);
      const button = document.createElement('button'); button.type = 'button';
      button.className = 'llm-anomaly-open'; button.setAttribute('aria-label', `放大异常照片 ${record.image}`);
      const cue = document.createElement('span'); cue.className = 'event-photo-cue'; cue.textContent = '放大截图';
      button.append(annotatedImage(record), cue);
      const description = document.createElement('p'); description.className = 'event-person-description';
      description.textContent = record.description;
      const footer = document.createElement('p'); footer.className = 'event-person-meta';
      footer.textContent = [`首次图像 ${record.image}`, target.sightings > 1 ? `合并 ${target.sightings} 帧识别` : '',
        target.location_error].filter(Boolean).join(' · ');
      button.onclick = () => {
        $('llm-image-dialog').querySelector('h2').textContent = '人员异常照片';
        $('llm-image-detail').replaceChildren(annotatedImage(record));
        const caption = document.createElement('p'); caption.textContent = [title, record.description, target.location_error].filter(Boolean).join(' · ');
        $('llm-image-detail').append(caption); $('llm-image-dialog').showModal();
      };
      card.replaceChildren(heading, button, description, footer);
    }
  }
  $('llm-image-close').onclick = () => $('llm-image-dialog').close();

  function showTraffic(execution) {
    const traffic = execution?.traffic;
    const missionId = traffic ? execution.mission_id : null;
    if (trafficMission !== missionId) {
      trafficMission = missionId;
      window.trafficView.clear();
    }
    if (traffic) window.trafficView.render(execution);
    const error = traffic?.errors?.[0]?.error || execution?.tasks.find(t => t.monitor?.traffic?.fusion?.error)?.monitor.traffic.fusion.error;
    $('llm-traffic-error').hidden = !error;
    $('llm-traffic-error').textContent = error ? '部分监控暂不可用' : '';
  }

  // 根据规划与飞行状态锁定输入，防止刷新或重复点击再次派发。
  function controls() {
    for (const element of [submit, input, followupSubmit, followupInput])
      element.disabled = pending || checking || flying || resetting || resetFailed || modelPending || !modelBackend;
    $('task-presets').hidden = form.hidden;
    for (const button of $('task-presets').querySelectorAll('button')) button.disabled = input.disabled;
    const modelLocked = modelPending || pending || checking || flying || resetting || resetFailed;
    modelSelect.disabled = modelLocked || !modelBackend;
    modelSelect.setAttribute('aria-busy', String(modelPending));
    modelRetry.disabled = modelLocked;
    newTask.hidden = false;
    newTask.disabled = resetting || modelPending;
    newTask.textContent = resetting ? '正在复位…' : pending || flying ? '结束并复位' : '全局复位';
    followupForm.hidden = !needsClarification || flying || resetting || resetFailed;
  }

  // 读取任务使用短请求；超时只停止等待，不终止无人机控制进程。
  async function missionRequest(url, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(url, {...options, cache: 'no-store', signal: controller.signal});
      const data = await response.json();
      if (!response.ok) throw Object.assign(Error(data.error || '无法读取执行进度。'), {statusCode: response.status});
      return data;
    } finally { clearTimeout(timer); }
  }

  function showBackend(data) {
    modelBackend = data.backend;
    modelInfo.textContent = data.model +
      (data.vision_model !== data.model ? `；视觉：${data.vision_model}` : '');
    modelInfo.dataset.error = 'false';
  }

  async function updateBackend(backend) {
    modelPending = true; controls();
    modelInfo.textContent = backend ? '正在切换模型…' : '正在读取模型…';
    modelInfo.dataset.error = 'false';
    modelRetry.hidden = true;
    try {
      showBackend(await missionRequest('/api/llm/backend', backend ? {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({backend})
      } : {}));
    } catch (error) {
      // 切换可能已写入但响应丢失，重新读取实际选择后再允许提交任务。
      if (backend) {
        try { showBackend(await missionRequest('/api/llm/backend')); }
        catch { modelBackend = null; }
      }
      const current = modelBackend ? `当前${modelBackend === 'ollama' ? '本地' : '云端'}模型；` : '';
      modelInfo.textContent = current + (error.statusCode === 404 ? '请重启网页服务并刷新，以加载模型切换功能。' : error.message);
      modelInfo.dataset.error = 'true';
    } finally {
      modelPending = false;
      modelSelect.value = modelBackend || '';
      modelRetry.hidden = modelInfo.dataset.error !== 'true';
      controls();
    }
  }
  modelSelect.onchange = () => {
    if (modelSelect.disabled || modelSelect.value === modelBackend) return;
    void updateBackend(modelSelect.value);
  };
  modelRetry.onclick = () => { if (!modelRetry.disabled) void updateBackend(); };

  function renderPlanZones(tasks) {
    const zones = tasks.map(task => {
      const id = task.target.zone_id;
      return {id, name: window.liveMap?.getZone(id)?.properties.name || id, status: task.status || 'queued',
        search: task.drone_id, monitor: task.monitor?.drone_id,
        monitorStatus: monitorLabels[task.monitor?.status] || task.monitor?.status, error: task.error};
    });
    const signature = JSON.stringify(zones);
    if (signature === displayedZones) return;
    displayedZones = signature;
    $('llm-plan-dispatch').hidden = !zones.length;
    $('llm-plan-count').textContent = `${zones.length} 个巡查区域`;
    const template = $('llm-plan-zone');
    const cards = zones.map(zone => {
      const card = template.content.cloneNode(true);
      const field = name => card.querySelector(`[data-plan-${name}]`);
      field('zone').textContent = zone.name;
      field('code').textContent = zone.id;
      field('code').hidden = zone.id === zone.name;
      field('state').textContent = taskLabels[zone.status] || '状态待确认';
      field('state').dataset.state = zone.status;
      field('search').textContent = zone.search || '待分配';
      field('monitor').textContent = zone.monitor || '待分配';
      field('monitor-state').textContent = zone.monitorStatus || '';
      field('monitor-state').hidden = !zone.monitorStatus || !zone.monitor && zone.monitorStatus === '等待分配';
      field('error').textContent = zone.error || '';
      field('error').hidden = !zone.error;
      return card;
    });
    planZones.replaceChildren(...cards);
  }
  window.addEventListener('map:ready', () => {
    if (mission) {
      renderPlanZones(mission.tasks);
      showVision(mission);
      showTraffic(mission);
    }
  });

  function showPlanResult(plan, instruction, duration = '') {
    const clarification = plan.status === 'needs_clarification';
    $('llm-answer-heading').textContent = clarification ? '需要你补充' : '巡查计划';
    $('llm-submitted').textContent = instruction;
    $('llm-answer').textContent = plan.message;
    $('llm-elapsed').textContent = duration ? `规划耗时 ${duration} 秒` : '';
    $('llm-elapsed').hidden = !duration;
    planNote.open = clarification;
    $('llm-command-panel').hidden = clarification;
    const commands = plan.commands;
    $('llm-commands').textContent = clarification ? '' :
      JSON.stringify(commands.length === 1 ? commands[0] : {tasks: commands}, null, 2);
    resultPanel.hidden = false;
    form.hidden = true;
  }

  // 展示真实巡查任务状态。
  function showMission(data) {
    const wasResetting = resetting;
    resetting = !!data.resetting;
    resetFailed = !!data.reset_error;
    if (resetting) {
      setStatus('busy', '正在终止后台任务并复位全部无人机，请稍候…'); controls(); return;
    }
    if (resetFailed) {
      flying = false;
      setStatus('error', `复位失败：${data.reset_error} 请重试复位。`); controls(); return;
    }
    if (data.reset_completed && !data.execution && (wasResetting || (!pending && form.hidden))) {
      clearTask();
      window.dispatchEvent(new Event('mission:reset'));
      setStatus('idle', '后台任务已终止，无人机已复位，可以输入新任务。');
      controls();
      if (!$('llm-panel').hidden) input.focus();
      return;
    }
    mission = data.execution;
    showVision(mission);
    showTraffic(mission);
    window.dispatchEvent(new CustomEvent('workspace:mission', {detail: mission}));
    flying = !!data.starting || !!data.uncertain || !!mission?.traffic?.active ||
      !!mission?.traffic?.pursuit && !['complete', 'failed', 'stopped'].includes(mission.traffic.pursuit.status) ||
      !!mission && !['completed', 'failed'].includes(mission.status);
    if (mission || flying) needsClarification = false;
    if (mission) {
      renderPlanZones(mission.tasks);
      const actionLines = [];
      const rescueLabels = {locating: '定位地面落点', dispatching: '正在派机', delivering: '执行药品投递',
        returning: '投递完成，正在返航', delivered: '投递完成，已返航降落', failed: '失败', uncertain: '执行状态未确认', stopped: '已停止'};
      const rescues = mission.rescues || [];
      if (rescues.length) actionLines.push('药品救援', ...rescues.map(rescue =>
        `${rescue.rescue_id} · ${rescue.zone_id} · ${rescue.image} → ${rescue.drone_id || '尚未分配'}（${rescueLabels[rescue.status] || rescue.status}）` +
        (rescue.target_position ? `；落点 [${rescue.target_position.map(value => value.toFixed(1)).join(', ')}] 米` : '') +
        (rescue.error ? `：${rescue.error}` : '')));
      const delivering = rescues.some(rescue => ['dispatching', 'delivering'].includes(rescue.status));
      const responseLabels = {queued: '等待前一目标处置', dispatching: '正在派机', responding: '抵近并执行投网',
        resolved: '处置完成，已返航降落', failed: '失败', uncertain: '执行状态未确认', stopped: '已停止'};
      const responses = mission.responses || [];
      const phaseLabels = {approaching: '抵近目标', threat: '持刀威胁', following: '跑开与无人机跟随',
        confronting: '转身对抗', netting: '投网与倒地', returning: '处置完成，正在返航', resolved: '处置完成'};
      if (responses.length) actionLines.push('持刀人员处置', ...responses.map(response =>
        `${response.response_id} · ${response.zone_id} · ${response.image} → ${response.drone_id || '尚未分配'}（${response.status === 'responding' && phaseLabels[response.phase] || responseLabels[response.status] || response.status}）` +
        (response.person_id ? `；人物 ${response.person_id}` : '') +
        (response.error ? `：${response.error}` : '')));
      const actionText = actionLines.join('\n');
      if ($('llm-plan-action-log').textContent !== actionText) $('llm-plan-action-log').textContent = actionText;
      planActions.hidden = !actionLines.length;
      const responding = responses.some(response => ['queued', 'dispatching', 'responding'].includes(response.status));
      const messages = {running: mission.tasks.some(task => task.status === 'analyzing') ?
        '巡查机已返航，正在完成照片分析。' : mission.tasks.some(task => task.monitor.status === 'flying')
          ? '人员巡查进行中 · 车辆监测等待监控机就位。' : '人员巡查与车辆监测正在进行。',
        completed: '搜索已完成 · 巡查机已返航降落，监控机保持高空悬停。',
        failed: '任务执行失败。' + (mission.tasks.find(task => task.error)?.error || rescues.find(rescue => rescue.error)?.error ||
          responses.find(response => response.error)?.error || '')};
      if (mission.status === 'running' && delivering)
        messages.running = '任务执行中 · 已发现病人，救援无人机正在执行药品投递。';
      if (mission.status === 'completed' && rescues.length)
        messages.completed = '巡查及药品救援已完成 · 救援机已返航降落。';
      if (mission.status === 'running' && responding)
        messages.running = delivering ? '任务执行中 · 药品投递与持刀人员投网处置正在进行。' :
          '任务执行中 · 已发现持刀人员，处置无人机正在抵近并投网。';
      if (mission.status === 'completed' && responses.length)
        messages.completed = '巡查及异常目标处置已完成 · 处置机已返航降落。';
      if (mission.status === 'running' && (rescues.some(rescue => rescue.status === 'returning') ||
          responses.some(response => response.status === 'responding' && response.phase === 'returning')))
        messages.running = '任务执行中 · 无人机正在返航降落。';
      if (mission.traffic?.active && ['completed', 'failed'].includes(mission.status))
        messages[mission.status] += ' 空地联合车辆监测继续运行，可结束并复位。';
      const pursuit = mission.traffic?.pursuit;
      const pursuitMessage = {preparing: '正在调度追踪机队。',
        tracking: pursuit?.target?.intercept_state === 'braking' ? '异常车辆正在减速停车。' : '无人机正在协同追踪车辆。',
        returning: pursuit?.outcome === 'intercepted' ? '车辆已截停，无人机正在归位。' : '无人机正在归位。',
        complete: '车辆追踪任务已完成。',
        failed: '车辆追踪未完成。', stopped: '车辆追踪已结束。'}[pursuit?.status];
      setStatus(pursuit?.status === 'failed' || !pursuit && mission.status === 'failed' ? 'error' : flying ? 'busy' : 'ready',
        pursuitMessage || messages[mission.status] || '正在确认执行状态…');
      newTask.hidden = false;
      if (resultPanel.hidden && data.plan) showPlanResult(data.plan, '已恢复最近巡查任务');
    } else if (flying) setStatus('busy', '正在确认任务派发状态，请勿重复提交。');
    if (data.uncertain) setStatus('error', '执行状态尚未确认，正在重新查询；请勿重复提交。');
    controls();
  }

  // 页面打开或刷新时恢复任务，运行期间每秒轮询；查询失败保持锁定并重试。
  async function pollMission(resetIdle = true) {
    clearTimeout(pollTimer);
    const current = generation;
    let snapshot = null;
    try {
      const data = await missionRequest('/api/llm/mission');
      if (current !== generation) return;
      snapshot = data;
      checking = false; showMission(data);
      if (resetIdle && !mission && !flying && !pending && !resetting && !resetFailed)
        setStatus('idle', data.reset_completed ? '无人机已复位，可以创建巡查任务。' : '等待创建巡查任务');
    } catch (error) {
      if (current !== generation) return;
      checking = true; controls(); setStatus('error', `无法确认执行状态：${error.message} 正在重试。`);
    }
    if (checking || flying || resetting) pollTimer = setTimeout(pollMission, 1000);
    return snapshot;
  }

  function selectTab(selected) {
    for (const tab of tabs) {
      const active = tab === selected;
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      $(tab.getAttribute('aria-controls')).hidden = !active;
    }
    window.dispatchEvent(new CustomEvent('sidebar:change', {detail: {panel: selected.getAttribute('aria-controls')}}));
  }
  for (const [index, tab] of tabs.entries()) {
    tab.onclick = () => selectTab(tab);
    tab.onkeydown = event => {
      const vertical = tab.closest('[role="tablist"]').getAttribute('aria-orientation') === 'vertical';
      const previousKey = vertical ? 'ArrowUp' : 'ArrowLeft';
      const nextKey = vertical ? 'ArrowDown' : 'ArrowRight';
      const next = {[previousKey]: (index + tabs.length - 1) % tabs.length,
        [nextKey]: (index + 1) % tabs.length, Home: 0, End: tabs.length - 1}[event.key];
      if (next === undefined) return;
      event.preventDefault(); selectTab(tabs[next]); tabs[next].focus();
    };
  }

  function setStatus(state, message) {
    const changed = status.textContent !== message;
    status.dataset.state = state;
    status.textContent = message;
    if (changed && state === 'error' && !$('llm-panel').hidden) status.scrollIntoView({block: 'nearest'});
  }

  async function plan(source, button) {
    if (pending || checking || flying || resetting || resetFailed || modelPending || !modelBackend) return;
    const current = generation;
    const instruction = source.value.trim();
    if (!instruction) { setStatus('error', '请先输入任务描述。'); source.focus(); return; }
    if (conversation.length > 12 || instruction.length + conversation.reduce((n, m) => n + m.content.length, 0) > 16000) {
      setStatus('error', '当前对话已达到长度上限，请复位后描述完整要求。'); return;
    }
    if (location.protocol === 'file:') { setStatus('error', '请通过本地 HTTP 服务打开页面。'); return; }
    pending = true;
    controls();
    const buttonLabel = button === submit ? submit.querySelector('span') : button;
    const buttonText = buttonLabel.textContent;
    buttonLabel.textContent = '正在规划…'; $('llm-panel').setAttribute('aria-busy', 'true');
    setStatus('busy', '正在读取无人机状态并查询地图，请稍候…');
    const controller = new AbortController();
    planningController = controller;
    const started = performance.now();
    const elapsed = () => ((performance.now() - started) / 1000).toFixed(1);
    const ticker = setInterval(() => {
      if (current !== generation || resetting) return;
      status.textContent = `正在查询地图并等待模型判断 · 已用 ${Math.floor((performance.now() - started) / 1000)} 秒`;
    }, 1000);
    const timeout = setTimeout(() => controller.abort(), 675000);
    try {
      const response = await fetch('/api/llm/plan', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({instruction, conversation, flight: window.getMissionFlightSettings()}), signal: controller.signal
      });
      if (!response.headers.get('Content-Type')?.includes('application/json')) {
        throw new Error('网页服务未返回有效数据，请重启 node web/serve.cjs 后刷新页面。');
      }
      const result = await response.json();
      if (current !== generation) return;
      if (!response.ok) throw new Error(result.error || '规划请求失败，请重试。');
      if (!['ready', 'needs_clarification'].includes(result.status) ||
          typeof result.message !== 'string' || !result.message.trim() || !Array.isArray(result.commands)) {
        throw new Error('服务返回格式异常，请重试。');
      }
      const ready = result.status === 'ready';
      needsClarification = result.status === 'needs_clarification';
      const answer = result.message;
      if (previousTurn) {
        for (const [label, text] of [['你', previousTurn.instruction], ['助手', previousTurn.answer]]) {
          const message = document.createElement('p');
          message.textContent = `${label}：${text}`; $('llm-messages').append(message);
        }
        $('llm-history').hidden = false;
      }
      const commands = result.commands;
      conversation.push({role: 'user', content: instruction}, {role: 'assistant', content: JSON.stringify({
        status: result.status, message: answer, tasks: commands.map(({sequence, ...task}) => task)})});
      previousTurn = {instruction, answer};
      showPlanResult(result, instruction, elapsed());
      renderPlanZones(ready ? commands : []);
      newTask.hidden = false;
      followupInput.value = '';
      setStatus(ready ? 'busy' : 'question', ready ? '规划完成 · 正在确认执行状态。' : '等待补充信息 · 未生成指令。');
      if (ready) {
        if (!result.execution) throw Error('执行服务没有返回任务状态，请刷新页面核实。');
        showMission({execution: result.execution, plan: result});
        if (flying) { clearTimeout(pollTimer); pollTimer = setTimeout(pollMission, 1000); }
      }
      if (!$('llm-panel').hidden) resultPanel.scrollIntoView({block: 'nearest'});
    } catch (error) {
      if (current !== generation) return;
      const message = error.name === 'AbortError' ? '规划请求超时，正在核实是否已开始执行。' : error.message;
      setStatus('error', message);
      // 派发可能已成功但 HTTP 响应丢失；先查后台，避免盲目再次提交。
      checking = true; await pollMission(false);
      if (!checking && !flying) setStatus('error', message);
    } finally {
      clearTimeout(timeout); clearInterval(ticker);
      if (current === generation) {
        pending = false; planningController = null;
        controls(); buttonLabel.textContent = buttonText;
        $('llm-panel').removeAttribute('aria-busy');
      }
    }
  };
  form.onsubmit = event => { event.preventDefault(); void plan(input, submit); };
  followupForm.onsubmit = event => {
    event.preventDefault();
    if (needsClarification) void plan(followupInput, followupSubmit);
  };
  function clearTask() {
    mission = null; flying = false; pending = false; checking = false;
    conversation = []; previousTurn = null;
    needsClarification = false;
    input.value = ''; followupInput.value = ''; form.hidden = false;
    $('llm-messages').replaceChildren(); $('llm-history').hidden = true;
    $('llm-history').open = false; resultPanel.hidden = true;
    renderPlanZones([]);
    planNote.open = false;
    planActions.hidden = true; planActions.open = false;
    $('llm-command-panel').open = false;
    $('llm-plan-action-log').textContent = '';
    $('llm-commands').textContent = ''; $('llm-answer').textContent = '';
    $('llm-submitted').textContent = ''; $('llm-elapsed').textContent = '';
    $('llm-answer-heading').textContent = '巡查计划';
    showVision(null); showTraffic(null);
    window.dispatchEvent(new CustomEvent('workspace:mission', {detail: null}));
    if ($('llm-image-dialog').open) $('llm-image-dialog').close();
  }

  newTask.onclick = async () => {
    if (resetting) return;
    generation++;
    const current = generation;
    planningController?.abort(); planningController = null;
    clearTimeout(pollTimer);
    pending = false; checking = false; resetting = true; resetFailed = false;
    submit.querySelector('span').textContent = '规划并开始巡查'; followupSubmit.textContent = '发送补充 →';
    $('llm-panel').removeAttribute('aria-busy');
    controls(); setStatus('busy', '正在终止后台任务并复位全部无人机，请稍候…');
    try {
      const data = await missionRequest('/api/llm/reset', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
      });
      if (current !== generation) return;
      showMission(data);
      if (resetting) pollTimer = setTimeout(pollMission, 500);
    } catch (error) {
      if (current !== generation) return;
      if (error.statusCode === 404) {
        resetting = false; resetFailed = true;
        setStatus('error', '当前后台仍是旧版本，没有复位接口。请停止旧网页服务，重新运行 node web/serve.cjs 后刷新页面。');
        return;
      }
      setStatus('error', `无法确认复位请求：${error.message} 正在查询后台状态。`);
      const data = await pollMission(false);
      if (data && !data.resetting && !data.reset_error && !data.reset_completed) {
        resetting = false; resetFailed = true;
        setStatus('error', '未收到复位完成确认，请重试复位。');
      }
    } finally { controls(); }
  };
  window.addEventListener('vehicle-pursuit:change', () => {
    if (!resetting) void pollMission();
  });
  controls(); void pollMission(); void updateBackend();
})();
