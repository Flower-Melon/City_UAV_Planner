'use strict';

(() => {
  const phases = {planning: '正在规划', dispatching: '正在派发', clarification: '等待补充',
    planning_failed: '启动失败', running: '巡查中', completed: '巡查结束', failed: '部分任务未完成',
    uncertain: '执行状态待确认', observation: '场景观测', cancelled: '已取消',
    resetting: '正在复位', reset_failed: '复位待确认'};
  const clock = new Intl.DateTimeFormat('zh-CN', {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
  const dateTime = new Intl.DateTimeFormat('zh-CN', {dateStyle: 'medium', timeStyle: 'medium', hour12: false});
  const $ = id => document.getElementById(id);
  const items = new Map();
  const recent = $('briefing-events'), earlier = $('briefing-earlier'), archive = $('briefing-archive');
  const historyDialog = $('briefing-history-dialog'), historySelect = $('briefing-history-session');
  const historyReports = $('briefing-history-reports'), historyStatus = $('briefing-history-status');
  const historyCards = new Map();
  let historyRequest = 0, shownHistory, outputVersion;
  let session;
  let reportRevision;
  let cases = [];
  let aliases = {};

  function element(tag, className = '', text = '') {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function action(label, handler) {
    const button = element('button', '', label);
    button.type = 'button';
    button.onclick = handler;
    return button;
  }

  function showItem(key) {
    const view = items.get(aliases[key] || key);
    if (!view) return false;
    const item = view.card;
    view.manual = true;
    if (earlier.contains(item)) archive.open = true;
    if (view.expandable) item.open = true;
    view.heading.focus({preventScroll: true});
    item.scrollIntoView({block: 'nearest'});
    return true;
  }

  function photo(entry, lazy = false) {
    const frame = element('div', 'llm-anomaly-image');
    const image = element('img');
    image.alt = entry.description || entry.title;
    if (lazy) image.loading = 'lazy';
    image.src = entry.image.url;
    frame.append(image);
    if (entry.image.bbox) {
      const [left, top, right, bottom] = entry.image.bbox;
      const box = element('div', 'llm-anomaly-box');
      Object.assign(box.style, {left: left / 10 + '%', top: top / 10 + '%',
        width: (right - left) / 10 + '%', height: (bottom - top) / 10 + '%'});
      frame.append(box);
    }
    return frame;
  }

  function personEvidence(entry) {
    const body = element('div', 'briefing-person');
    if (entry.image?.url) {
      const preview = action('查看原图', () => {
        $('llm-image-dialog').querySelector('h2').textContent = entry.title;
        $('llm-image-detail').replaceChildren(photo(entry), element('p', '', entry.description));
        $('llm-image-dialog').showModal();
      });
      preview.className = 'briefing-photo';
      preview.replaceChildren(photo(entry, true), element('span', '', '查看原图'));
      body.append(preview);
    }
    const source = [entry.image?.drone, entry.image?.at && '发现于 ' + clock.format(entry.image.at)].filter(Boolean).join(' · ');
    if (source) body.append(element('p', 'briefing-meta', source));
    if (entry.description) body.append(element('p', '', entry.description));
    if (entry.location_error) body.append(element('p', 'briefing-warning', entry.location_error));
    if (entry.action?.drone) body.append(element('p', 'briefing-meta', `执行无人机 ${entry.action.drone}`));
    if (entry.action?.error) body.append(element('p', 'briefing-warning', entry.action.error));
    const actions = element('div', 'briefing-actions');
    if (entry.position)
      actions.append(action('地图定位', () => window.liveMap.centerEvidence(entry.position)));
    else if (entry.zone)
      actions.append(action('查看巡查区域', () => {
        window.liveMap.selectZone(entry.zone, true);
        window.dispatchEvent(new Event('workspace:show-map'));
      }));
    body.append(actions);
    return body;
  }

  function makeItem(item) {
    const expandable = !!(item.isCase || item.detail || item.zone);
    const card = element(expandable ? 'details' : 'article', 'briefing-item');
    const heading = element(expandable ? 'summary' : 'div', 'briefing-item-heading');
    if (!expandable) heading.tabIndex = -1;
    const name = element('span', 'briefing-item-title');
    const meta = element('span', 'briefing-item-meta');
    const time = element('time');
    const subject = element('span', 'briefing-item-subject');
    subject.append(name, meta);
    heading.append(subject, time);
    const body = element('div', 'briefing-item-body');
    const evidence = element('div', 'briefing-item-evidence');
    const note = element('p', 'briefing-case-note');
    body.append(evidence, note);
    card.append(heading);
    if (expandable) card.append(body);
    card.dataset.kind = item.kind;
    const view = {card, heading, expandable, name, meta, time, evidence, note, manual: false};
    if (expandable) heading.addEventListener('click', () => { view.manual = true; });
    return view;
  }

  function updateItem(view, item) {
    view.name.textContent = item.title;
    view.time.dateTime = new Date(item.at).toISOString();
    view.time.textContent = clock.format(item.at);
    view.meta.textContent = item.isCase ? [item.where, item.action?.label, item.pursuit?.title || item.status]
      .filter(Boolean).join(' · ') : item.meta || '';
    view.meta.hidden = !view.meta.textContent;
    const evidence = view.evidence;
    if (!item.isCase) {
      evidence.replaceChildren(...(item.detail ? [element('p', '', item.detail)] : []));
      if (item.zone) {
        const actions = element('div', 'briefing-actions');
        actions.append(action('地图定位', () => {
          window.liveMap.selectZone(item.zone, true);
          window.dispatchEvent(new Event('workspace:show-map'));
        }));
        evidence.append(actions);
      }
      return;
    }
    if (item.kind === 'person') {
      evidence.replaceChildren(personEvidence(item));
    } else {
      const contents = [];
      if (item.record) {
        if (!view.traffic || view.trafficId !== item.target_id) {
          view.traffic = window.createTrafficEvidence(item.target_id);
          view.trafficId = item.target_id;
        }
        view.traffic.update(item);
        contents.push(view.traffic.element);
      }
      const actions = element('div', 'briefing-actions');
      if (item.pursuit) {
        contents.push(element('p', item.pursuit.error ? 'briefing-warning' : 'briefing-pursuit',
          [item.pursuit.title, item.pursuit.error].filter(Boolean).join(' · ')));
        if (item.pursuit.latest) {
          const camera = action('查看追踪镜头', null);
          camera.dataset.openPursuit = '';
          actions.append(camera);
        }
      } else if (item.escaped_at && item.record) {
        contents.push(element('p', 'briefing-pursuit', '已启动逃逸演示'));
      }
      if (!item.record && item.position)
        actions.append(action('地图定位', () => window.liveMap.centerEvidence(item.position)));
      if (!item.record && item.target_id)
        actions.append(action('查看场景目标', () => window.dispatchEvent(
          new CustomEvent('abnormal:select', {detail: {id: item.target_id}}))));
      if (actions.children.length) contents.push(actions);
      evidence.replaceChildren(...contents);
    }
    if (item.history.length > 1) {
      const history = element('details', 'briefing-case-history');
      history.append(element('summary', '', '状态记录'));
      const list = element('ol');
      for (const event of item.history) {
        const row = element('li');
        row.append(element('time', '', clock.format(event.at)), element('span', '', event.title));
        list.append(row);
      }
      history.append(list);
      evidence.append(history);
    }
  }

  function place(container, rows) {
    const wanted = new Set(rows);
    for (const child of [...container.children]) if (!wanted.has(child)) child.remove();
    rows.forEach((row, index) => {
      if (container.children[index] !== row) container.insertBefore(row, container.children[index] || null);
    });
  }

  function renderTimeline(feed) {
    cases = feed.cases || [];
    aliases = feed.aliases || {};
    for (const [original, canonical] of Object.entries(aliases)) {
      if (items.has(original) && !items.has(canonical)) {
        items.set(canonical, items.get(original));
        items.delete(original);
      }
    }
    const flow = (feed.events || []).filter(event => !event.subject)
      .map(event => ({...event, key: 'event:' + event.id, sequence: event.id}));
    const active = item => item.pursuit?.latest && item.pursuit.active ? 1 : 0;
    const records = [...cases.map(entry => ({...entry, key: entry.id, sequence: entry.event_id, isCase: true})), ...flow]
      .sort((a, b) => active(b) - active(a) || b.sequence - a.sequence);
    const keys = new Set(records.map(item => item.key));
    for (const [key, view] of items) {
      if (!keys.has(key)) { view.card.remove(); items.delete(key); }
    }
    const latestCase = records.find(item => item.isCase && !active(item));
    const rows = records.map(item => {
      let view = items.get(item.key);
      if (!view) {
        view = makeItem(item);
        items.set(item.key, view);
      }
      if (view.sequence !== item.sequence || view.latestPursuit !== item.pursuit?.latest || view.captureUrl !== item.alert?.image_url) {
        view.sequence = item.sequence;
        view.latestPursuit = item.pursuit?.latest;
        view.captureUrl = item.alert?.image_url;
        updateItem(view, item);
      }
      const expanded = item === latestCase || !!active(item);
      if (view.expandable && !view.manual && view.card.open !== expanded) view.card.open = expanded;
      const note = item.note || '';
      if (view.note.textContent !== note) view.note.textContent = note;
      view.note.hidden = !note;
      return view.card;
    });
    place(recent, rows.slice(0, 6));
    place(earlier, rows.slice(6));
    archive.hidden = rows.length <= 6;
    $('briefing-feed').hidden = !rows.length;
  }

  function setText(id, message) {
    const node = $(id);
    if (node.textContent !== message) node.textContent = message;
  }

  function historyCard(report) {
    const card = element('details', 'briefing-item');
    const heading = element('summary', 'briefing-item-heading');
    const stamp = element('time', '', dateTime.format(report.at));
    stamp.dateTime = new Date(report.at).toISOString();
    const title = report.kind === 'clarification' ? '补充问题' : '巡查研判';
    heading.append(element('span', 'briefing-item-subject briefing-item-title', title), stamp);
    const body = element('div', 'briefing-item-body');
    body.append(element('p', 'briefing-history-copy', report.summary));
    if (report.next_focus) body.append(element('h3', '', '建议'), element('p', 'briefing-history-copy', report.next_focus));
    if (report.evidence?.length) {
      const list = element('ul');
      for (const event of report.evidence) {
        const details = [event.title, event.meta, event.detail].filter(Boolean).join(' · ');
        list.append(element('li', '', details));
      }
      body.append(element('h3', '', '引用依据'), list);
    }
    const notes = Object.values(report.case_notes || {});
    if (notes.length) {
      const list = element('ul');
      list.append(...notes.map(note => element('li', '', note)));
      body.append(element('h3', '', '补充判断'), list);
    }
    card.append(heading, body);
    return card;
  }

  function renderHistory(id, reports) {
    if (shownHistory !== id) {
      shownHistory = id;
      historyCards.clear();
    }
    const rows = [...reports].reverse().map((report, index) => {
      let card = historyCards.get(report.sequence);
      if (!card) {
        card = historyCard(report);
        card.open = index === 0;
        historyCards.set(report.sequence, card);
      }
      return card;
    });
    place(historyReports, rows);
    historyStatus.textContent = rows.length ? '' : '暂无助手输出。';
    historyStatus.hidden = !!rows.length;
  }

  async function fetchHistory(id) {
    const query = id ? '?' + new URLSearchParams({session_id: id}) : '';
    const response = await fetch('/api/llm/briefing/history' + query, {cache: 'no-store', signal: AbortSignal.timeout(6000)});
    if (!response.ok) throw Error('暂时无法读取助手输出历史。');
    return response.json();
  }

  async function loadHistory(id) {
    const request = ++historyRequest;
    if (!id || id !== shownHistory) {
      historyReports.hidden = true;
      historyStatus.textContent = '正在读取历史输出…';
      historyStatus.hidden = false;
    }
    try {
      if (!id) {
        const {sessions} = await fetchHistory();
        if (request !== historyRequest) return;
        const chosen = sessions.find(entry => entry.id === historySelect.value) ||
          sessions.find(entry => entry.id === session) || sessions[0];
        historySelect.replaceChildren(...sessions.map(entry => {
          const instruction = entry.instruction?.trim().replace(/\s+/g, ' ').slice(0, 72) || '场景观测';
          return new Option(`${dateTime.format(entry.at)} · ${instruction}`, entry.id);
        }));
        historySelect.disabled = !chosen;
        id = chosen?.id;
        if (id) historySelect.value = id;
      }
      const {reports} = id ? await fetchHistory(id) : {reports: []};
      if (request !== historyRequest) return;
      renderHistory(id, reports);
      historyReports.hidden = false;
    } catch (error) {
      if (request !== historyRequest) return;
      historyStatus.textContent = error.message;
      historyStatus.hidden = false;
    }
  }

  $('briefing-history-open').onclick = () => { historyDialog.showModal(); void loadHistory(); };
  $('briefing-history-close').onclick = () => historyDialog.close();
  historySelect.onchange = () => { void loadHistory(historySelect.value); };
  historyDialog.addEventListener('close', () => { historyRequest++; });

  function render(feed) {
    if (session !== feed.session_id) {
      session = feed.session_id;
      items.clear();
      recent.replaceChildren();
      earlier.replaceChildren();
      archive.open = false;
      reportRevision = undefined;
    }
    renderTimeline(feed);
    const version = `${session || ''}:${feed.report_count || 0}`;
    if (version !== outputVersion) {
      outputVersion = version;
      if (historyDialog.open && (!historySelect.value || historySelect.value === session))
        void loadHistory(historySelect.value);
    }
    const current = $('briefing-current');
    current.hidden = feed.phase === 'idle';
    if (current.hidden) return;
    const report = feed.report;
    const latest = feed.events?.at(-1);
    const phase = phases[feed.phase] || '巡查助手';
    setText('briefing-phase', phase);
    setText('briefing-summary', report?.summary || latest?.title || phase);
    $('briefing-advice').hidden = !report?.next_focus;
    setText('briefing-focus', report?.next_focus || '');
    const status = $('briefing-state');
    const statusText = feed.history_error ? '部分助手输出尚未写入文件。' :
      {generating: '正在研判最新变化…', unavailable: '助手暂未生成建议，证据可直接查看。',
        data_error: '最新执行状态暂不可读。'}[feed.status] || '';
    setText('briefing-state', statusText);
    status.hidden = !statusText;
    status.classList.toggle('briefing-warning', !!feed.history_error || !!statusText && feed.status !== 'generating');
    const sources = $('briefing-sources');
    sources.hidden = !report?.evidence.length;
    if (reportRevision !== (report?.revision ?? null)) {
      reportRevision = report?.revision ?? null;
      sources.replaceChildren(...(report?.evidence || []).map(event =>
        action(event.title, () => showItem(event.subject || 'event:' + event.id))));
    }
  }

  window.missionBriefingView = {focusTarget(id) {
    if (!id) return false;
    const entry = cases.find(item => item.target_id === id || item.person_id === id);
    return !!entry && showItem(entry.id);
  }};

  let requestGeneration = 0;
  let pollTimer;
  async function poll() {
    const generation = requestGeneration;
    let interval = 1800;
    try {
      const response = await fetch('/api/llm/briefing', {cache: 'no-store', signal: AbortSignal.timeout(6000)});
      if (!response.ok) throw Error('无法读取巡查通报');
      const feed = await response.json();
      if (generation !== requestGeneration) return;
      render(feed);
      if (!document.hidden && feed.phase !== 'idle') interval = feed.phase === 'running' ? 300 : 800;
    } catch {
      if (generation !== requestGeneration) return;
      const status = $('briefing-state');
      status.textContent = '暂时无法获取新的巡查通报。';
      status.classList.add('briefing-warning');
      status.hidden = false;
    } finally {
      if (generation === requestGeneration) pollTimer = setTimeout(poll, interval);
    }
  }

  window.addEventListener('mission:reset', () => {
    requestGeneration++;
    clearTimeout(pollTimer);
    render({phase: 'idle'});
    if (location.protocol !== 'file:') void poll();
  });
  if (location.protocol !== 'file:') void poll();
})();
