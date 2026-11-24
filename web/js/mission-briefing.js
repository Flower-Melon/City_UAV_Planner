'use strict';

(() => {
  const phaseCopy = {
    idle: '输入巡查指令后，我会根据执行记录说明现场进展，并指出需要核验的情况。',
    planning: '正在核对机队状态和地图范围。',
    dispatching: '巡查计划已生成，正在等待执行端确认。',
    clarification: '请补充下方计划中缺少的信息。',
    planning_failed: '任务未能启动，请查看下方的状态说明。',
    cancelled: '本轮规划已取消。',
    resetting: '机队正在复位。',
    reset_failed: '机队复位尚未确认，请查看下方的状态说明。'
  };
  const statusCopy = {
    generating: '正在核对最新执行记录…',
    waiting: '正在等待第一批执行记录。',
    pending: '执行记录有更新，正在等待下一次归纳。',
    retrying: '助手暂时无法更新，稍后会重试。',
    log_error: '执行记录暂时无法写入。',
    data_error: '执行状态暂未确认，正在重新读取。'
  };
  const time = new Intl.DateTimeFormat('zh-CN', {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
  const get = id => document.getElementById(id);

  class MissionBriefingView {
    constructor() {
      this.current = get('briefing-current');
      this.status = get('briefing-state');
      this.history = get('briefing-history');
      this.timeline = get('briefing-events');
      this.previous = get('briefing-previous');
      this.updates = get('briefing-updates');
      this.eventItems = new Map();
    }

    setStatus(message, warning = false) {
      if (this.status.textContent !== message) this.status.textContent = message;
      this.status.hidden = !message;
      this.status.classList.toggle('briefing-warning', warning);
    }

    render(feed) {
      if (this.session !== feed.session_id) {
        this.session = feed.session_id;
        this.timestamp = undefined;
        this.history.open = this.previous.open = false;
        this.eventItems.clear();
        this.timeline.replaceChildren();
      }
      const report = ['resetting', 'reset_failed'].includes(feed.phase) ? null : feed.report;
      const earlier = report ? feed.updates || [] : [];
      const timestamp = report?.at ?? null;
      if (timestamp !== this.timestamp) {
        this.timestamp = timestamp;
        this.current.replaceChildren(...(report ? [this.updateArticle(report)] : []));
        this.updates.replaceChildren(...earlier.map(update => this.updateArticle(update)));
        this.previous.hidden = !earlier.length;
      }

      const warning = ['log_error', 'data_error', 'retrying'].includes(feed.status);
      const status = warning ? statusCopy[feed.status] : phaseCopy[feed.phase] || statusCopy[feed.status] || '';
      this.setStatus(status, warning || ['planning_failed', 'reset_failed'].includes(feed.phase));

      const events = feed.events || [];
      const references = [report, ...earlier].flatMap(update => update?.evidence || []);
      this.history.hidden = !events.length && !references.length;
      this.renderEvents([...events, ...references]);
    }

    updateArticle(report) {
      const article = document.createElement('article');
      article.className = 'briefing-update';
      const stamp = document.createElement('time');
      stamp.dateTime = new Date(report.at).toISOString();
      stamp.textContent = time.format(report.at);
      const summary = document.createElement('p');
      summary.textContent = report.summary;
      const focus = document.createElement('p');
      focus.textContent = report.next_focus;
      article.append(stamp, summary, focus);

      const sources = document.createElement('details');
      sources.className = 'briefing-sources';
      const label = document.createElement('summary');
      label.textContent = '查看依据';
      const list = document.createElement('ul');
      for (const event of report.evidence) {
        const row = document.createElement('li');
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = event.title;
        button.addEventListener('click', () => {
          const item = this.eventItems.get(event.id);
          if (!item) return;
          this.history.open = true;
          item.focus({preventScroll: true});
          item.scrollIntoView({block: 'nearest'});
        });
        row.append(button);
        list.append(row);
      }
      sources.append(label, list);
      article.append(sources);
      return article;
    }

    renderEvents(events) {
      const retained = new Map(events.map(event => [event.id, event]));
      for (const [id, item] of this.eventItems) {
        if (retained.has(id)) continue;
        item.remove();
        this.eventItems.delete(id);
      }
      for (const event of [...retained.values()].sort((a, b) => a.id - b.id)) {
        if (this.eventItems.has(event.id)) continue;
        const item = this.eventItem(event);
        this.eventItems.set(event.id, item);
        const next = [...this.timeline.children].find(child => Number(child.dataset.eventId) < event.id);
        this.timeline.insertBefore(item, next || null);
      }
    }

    eventItem(event) {
      const item = document.createElement('li');
      item.dataset.eventId = event.id;
      item.tabIndex = -1;
      const stamp = document.createElement('time');
      stamp.dateTime = new Date(event.at).toISOString();
      stamp.textContent = time.format(event.at);
      const title = document.createElement('strong');
      title.textContent = event.title;
      item.append(stamp, title);
      if (event.detail) {
        const detail = document.createElement('small');
        detail.textContent = event.detail;
        item.append(detail);
      }
      if (event.zone) {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = '在地图查看';
        button.addEventListener('click', () => {
          window.liveMap.selectZone(event.zone, true);
          window.dispatchEvent(new Event('workspace:show-map'));
        });
        item.append(button);
      }
      return item;
    }

    async poll() {
      if (location.protocol === 'file:') {
        this.setStatus('请通过本地网页服务打开巡查助手。');
        return;
      }
      let interval = 3000;
      try {
        const response = await fetch('/api/llm/briefing', {cache: 'no-store', signal: AbortSignal.timeout(6000)});
        if (!response.ok) throw Error('无法读取任务更新');
        const feed = await response.json();
        this.render(feed);
        if (feed.phase !== 'idle' && !document.hidden) interval = 1000;
      } catch {
        this.setStatus('当前无法连接巡查助手，稍后会重试。', true);
      }
      setTimeout(() => void this.poll(), interval);
    }
  }

  void new MissionBriefingView().poll();
})();
