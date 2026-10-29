'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const tabs = [...document.querySelectorAll('.sidebar-tabs [role="tab"]')];
  const form = $('llm-form'), input = $('llm-instruction'), submit = $('llm-submit');
  const followupForm = $('llm-followup-form'), followupInput = $('llm-followup-input');
  const followupSubmit = $('llm-followup-submit'), newTask = $('llm-new-task');
  const status = $('llm-status'), resultPanel = $('llm-result');
  let pending = false, conversation = [], previousTurn = null;
  let checking = true, flying = false, mission = null, pollTimer;
  let visionMission = null;
  const shownAnomalies = new Set();

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
      const label = document.createElement('span'); label.textContent = detection.label;
      box.title = detection.label; box.append(label); view.append(box);
    }
    return view;
  }

  function showVision(execution) {
    const vision = execution?.vision;
    $('llm-vision').hidden = !vision;
    if (!vision) return;
    if (visionMission !== execution.mission_id) {
      visionMission = execution.mission_id; shownAnomalies.clear();
      $('llm-anomalies').replaceChildren();
    }
    $('llm-anomaly-count').textContent = vision.abnormal;
    const phase = vision.status === 'running' ? '识别中' : '识别结束';
    $('llm-vision-status').textContent = `${phase} · 已分析 ${vision.analyzed}/${vision.total} 张 · 异常 ${vision.abnormal} 张 · 无法判断 ${vision.uncertain} 张 · 失败 ${vision.failed} 张`;
    const error = vision.errors?.[0]?.error;
    $('llm-vision-error').hidden = !error;
    $('llm-vision-error').textContent = error ? `部分照片未完成识别：${error}` : '';
    $('llm-vision-empty').hidden = vision.anomalies.length > 0;
    $('llm-vision-empty').textContent = vision.status === 'running' ?
      '正在巡查和分析照片，发现异常后将在此显示。' :
      vision.failed || error ? '部分照片识别失败，请查看错误提示。' :
      vision.uncertain ? '尚未发现明确异常，部分照片无法可靠判断。' : '本次照片中未发现目标异常。';
    for (const record of vision.anomalies) {
      const key = `${record.task_id}:${record.image}`;
      if (shownAnomalies.has(key)) continue;
      shownAnomalies.add(key);
      const card = document.createElement('article'); card.className = 'llm-anomaly-card';
      const heading = document.createElement('p'); heading.className = 'llm-anomaly-heading';
      heading.textContent = `${record.drone_id} · ${record.image} · ${record.zone_id}`;
      const button = document.createElement('button'); button.type = 'button';
      button.className = 'llm-anomaly-open'; button.setAttribute('aria-label', `放大异常照片 ${record.image}`);
      button.append(annotatedImage(record));
      const description = document.createElement('p'); description.textContent = record.description;
      button.onclick = () => {
        $('llm-image-detail').replaceChildren(annotatedImage(record));
        const caption = document.createElement('p'); caption.textContent = record.description;
        $('llm-image-detail').append(caption); $('llm-image-dialog').showModal();
      };
      card.append(heading, button, description); $('llm-anomalies').append(card);
    }
  }
  $('llm-image-close').onclick = () => $('llm-image-dialog').close();

  // 根据规划与飞行状态锁定输入，防止刷新或重复点击再次派发。
  function controls() {
    for (const element of [submit, input, followupSubmit, followupInput, newTask])
      element.disabled = pending || checking || flying;
    followupForm.hidden = flying;
  }

  // 读取任务使用短请求；超时只停止等待，不终止无人机控制进程。
  async function missionRequest(url, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(url, {...options, cache: 'no-store', signal: controller.signal});
      const data = await response.json();
      if (!response.ok) throw Error(data.error || '无法读取执行进度。');
      return data;
    } finally { clearTimeout(timer); }
  }

  // 展示真实巡查任务状态。
  function showMission(data) {
    mission = data.execution;
    showVision(mission);
    flying = !!data.starting || !!data.uncertain || !!mission && !['completed', 'failed'].includes(mission.status);
    if (mission) {
      const messages = {running: mission.tasks.some(task => task.status === 'analyzing') ?
        '无人机已返航降落，正在分析剩余巡查照片。' : '搜索执行中 · 沿路拍照并识别异常，结束后返航降落。',
        completed: '搜索已完成 · 已返航降落，照片与识别结果已保存。',
        failed: '搜索任务失败。' + (mission.tasks.find(task => task.error)?.error || '')};
      setStatus(mission.status === 'failed' ? 'error' : flying ? 'busy' : 'ready', messages[mission.status] || '正在确认执行状态…');
      newTask.hidden = false;
      if (resultPanel.hidden && data.plan) {
        resultPanel.hidden = false; form.hidden = true;
        $('llm-submitted').textContent = '已恢复最近搜索任务';
        $('llm-answer').textContent = data.plan.message;
        $('llm-commands').textContent = JSON.stringify(data.plan.commands, null, 2);
        $('llm-command-panel').hidden = false;
      }
    } else if (flying) setStatus('busy', '正在确认任务派发状态，请勿重复提交。');
    if (data.uncertain) setStatus('error', '执行状态尚未确认，正在重新查询；请勿重复提交。');
    controls();
  }

  // 页面打开或刷新时恢复任务，运行期间每秒轮询；查询失败保持锁定并重试。
  async function pollMission(resetIdle = true) {
    clearTimeout(pollTimer);
    try {
      const data = await missionRequest('/api/llm/mission');
      checking = false; showMission(data);
      if (resetIdle && !mission && !flying && !pending) setStatus('idle', '输入任务后自动规划并开始搜索。');
    } catch (error) {
      checking = true; controls(); setStatus('error', `无法确认执行状态：${error.message} 正在重试。`);
    }
    if (checking || flying) pollTimer = setTimeout(pollMission, 1000);
  }

  function selectTab(selected) {
    for (const tab of tabs) {
      const active = tab === selected;
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      $(tab.getAttribute('aria-controls')).hidden = !active;
    }
    const abnormal = selected.id === 'abnormal-tab';
    $('uav-camera-panel').hidden = abnormal;
    $('abnormal-camera-panel').hidden = !abnormal;
    window.dispatchEvent(new CustomEvent('sidebar:change', {detail: {panel: selected.getAttribute('aria-controls')}}));
  }
  for (const [index, tab] of tabs.entries()) {
    tab.onclick = () => selectTab(tab);
    tab.onkeydown = event => {
      const next = {ArrowLeft: (index + tabs.length - 1) % tabs.length, ArrowRight: (index + 1) % tabs.length, Home: 0, End: tabs.length - 1}[event.key];
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
    if (pending || checking || flying) return;
    const instruction = source.value.trim();
    if (!instruction) { setStatus('error', '请先输入任务描述。'); source.focus(); return; }
    if (conversation.length > 12 || instruction.length + conversation.reduce((n, m) => n + m.content.length, 0) > 16000) {
      setStatus('error', '当前对话已达到长度上限，请点击「新任务」后描述完整要求。'); return;
    }
    if (location.protocol === 'file:') { setStatus('error', '请通过本地 HTTP 服务打开页面。'); return; }
    pending = true;
    controls();
    const buttonText = button.textContent;
    button.textContent = '正在规划…'; $('llm-panel').setAttribute('aria-busy', 'true');
    setStatus('busy', '正在读取无人机状态并查询地图，请稍候…');
    const controller = new AbortController();
    const started = performance.now();
    const elapsed = () => ((performance.now() - started) / 1000).toFixed(1);
    const ticker = setInterval(() => {
      status.textContent = `正在查询地图并等待模型判断 · 已用 ${Math.floor((performance.now() - started) / 1000)} 秒`;
    }, 1000);
    const timeout = setTimeout(() => controller.abort(), 675000);
    try {
      const response = await fetch('/api/llm/plan', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({instruction, conversation}), signal: controller.signal
      });
      if (!response.headers.get('Content-Type')?.includes('application/json')) {
        throw new Error('网页服务未返回有效数据，请重启 node web/serve.cjs 后刷新页面。');
      }
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || '规划请求失败，请重试。');
      if (!['ready', 'needs_clarification'].includes(result.status) ||
          typeof result.message !== 'string' || !result.message.trim() || !Array.isArray(result.commands)) {
        throw new Error('服务返回格式异常，请重试。');
      }
      const ready = result.status === 'ready';
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
      $('llm-submitted').textContent = instruction;
      $('llm-answer-heading').textContent = ready ? '规划说明' : '需要你补充';
      $('llm-answer').textContent = answer;
      $('llm-elapsed').textContent = `${elapsed()} 秒`;
      $('llm-command-panel').hidden = !ready;
      $('llm-queue').hidden = !ready;
      if (ready) {
        const queues = new Map();
        for (const command of result.commands) {
          if (!queues.has(command.drone_id)) queues.set(command.drone_id, []);
          queues.get(command.drone_id).push(command);
        }
        $('llm-queue').textContent = `共 ${commands.length} 个区域 · ${queues.size} 架无人机，分别按顺序搜索：\n` + [...queues].map(([drone, tasks]) =>
          `${drone}：${tasks.sort((a, b) => a.sequence - b.sequence).map(c =>
            c.target.zone_id + ' 完整街区').join(' → ')}`).join('\n');
        $('llm-commands').textContent = JSON.stringify(commands.length === 1 ? commands[0] : {tasks: commands}, null, 2);
      } else $('llm-commands').textContent = '';
      form.hidden = true; newTask.hidden = false;
      followupInput.value = '';
      resultPanel.hidden = false;
      setStatus(ready ? 'busy' : 'question', ready ? '规划完成 · 正在确认执行状态。' : '等待补充信息 · 未生成指令。');
      if (ready) {
        if (!result.execution) throw Error('执行服务没有返回任务状态，请刷新页面核实。');
        showMission({execution: result.execution, plan: result});
        if (flying) { clearTimeout(pollTimer); pollTimer = setTimeout(pollMission, 1000); }
      }
      if (!$('llm-panel').hidden) resultPanel.scrollIntoView({block: 'nearest'});
    } catch (error) {
      const message = error.name === 'AbortError' ? '规划请求超时，正在核实是否已开始执行。' : error.message;
      setStatus('error', message);
      // 派发可能已成功但 HTTP 响应丢失；先查后台，避免盲目再次提交。
      checking = true; await pollMission(false);
      if (!checking && !flying) setStatus('error', message);
    } finally {
      clearTimeout(timeout); clearInterval(ticker); pending = false;
      controls();
      button.textContent = buttonText;
      $('llm-panel').removeAttribute('aria-busy');
    }
  };
  form.onsubmit = event => { event.preventDefault(); void plan(input, submit); };
  followupForm.onsubmit = event => { event.preventDefault(); void plan(followupInput, followupSubmit); };
  newTask.onclick = () => {
    if (pending || checking || flying) return;
    conversation = []; previousTurn = null;
    $('llm-messages').replaceChildren(); $('llm-history').hidden = true;
    $('llm-history').open = false; resultPanel.hidden = true;
    $('llm-commands').textContent = ''; $('llm-answer').textContent = ''; $('llm-queue').textContent = '';
    $('llm-vision').hidden = true; $('llm-anomalies').replaceChildren();
    shownAnomalies.clear(); visionMission = null;
    input.value = ''; followupInput.value = ''; form.hidden = false; newTask.hidden = true;
    setStatus('idle', '输入任务后自动规划并开始搜索。'); input.focus();
  };
  controls(); void pollMission();
})();
