'use strict';

const fs = require('node:fs');
const path = require('node:path');
const {randomUUID} = require('node:crypto');

const sessionName = /^[a-f0-9]{32}$/;
const reportName = /^\d{6,}\.json$/;
const filenameFor = sequence => String(sequence).padStart(6, '0');

function entries(directory) {
  try { return fs.readdirSync(directory, {withFileTypes: true}); }
  catch (error) { if (error.code === 'ENOENT') return []; throw error; }
}

function writeNewFile(filename, content) {
  if (fs.existsSync(filename)) return;
  fs.mkdirSync(path.dirname(filename), {recursive: true});
  const temporary = filename + '.' + randomUUID() + '.tmp';
  try {
    fs.writeFileSync(temporary, content, {encoding: 'utf8', flag: 'wx'});
    fs.renameSync(temporary, filename);
  } finally {
    fs.rmSync(temporary, {force: true});
  }
}

function markdown(report, missionId) {
  const title = report.kind === 'clarification' ? '补充问题' : '巡查研判';
  const lines = [`# ${title} · ${filenameFor(report.sequence)}`, '',
    `生成时间 ${new Date(report.at).toISOString()}`, `会话 ${report.session_id}`];
  if (missionId) lines.push(`任务 ${missionId}`);
  if (report.model) lines.push(`模型 ${report.model}`);
  if (report.instruction) lines.push('', '## 任务指令', '', report.instruction);
  lines.push('', '## 助手输出', '', report.summary);
  if (report.next_focus) lines.push('', '## 建议', '', report.next_focus);
  if (report.evidence?.length) {
    lines.push('', '## 引用依据', '');
    for (const event of report.evidence) {
      lines.push(`- #${event.id} ${[event.title, event.meta, event.detail].filter(Boolean).join(' · ')}`);
    }
  }
  const notes = Object.entries(report.case_notes || {});
  if (notes.length) {
    lines.push('', '## 补充判断', '');
    for (const [subject, note] of notes) {
      const label = report.evidence?.find(event => event.subject === subject)?.title || subject;
      lines.push(`- ${label} · ${note}`);
    }
  }
  return lines.join('\n') + '\n';
}

class BriefingHistory {
  constructor(root) {
    this.root = root;
    this.directory = path.join(root, 'output', 'mission-briefings');
  }

  regionDirectories(tasks) {
    return tasks.map(task => task.output_directory)
      .filter(directory => typeof directory === 'string' && /^output[\\/]\d{6,}$/.test(directory))
      .map(directory => path.join(this.root, directory, 'briefings'));
  }

  save(session) {
    const primary = path.join(this.directory, session.id);
    const failed = [];
    for (const directory of [primary, ...session.reportDirectories]) {
      const structured = directory === primary;
      let saved = session.savedReports.get(directory) || 0;
      try {
        for (; saved < session.reports.length; saved++) {
          const report = session.reports[saved];
          const name = filenameFor(report.sequence) + (structured ? '.json' : '.md');
          const content = structured ? JSON.stringify(report, null, 2) + '\n' : markdown(report, session.missionId);
          writeNewFile(path.join(directory, name), content);
        }
      } catch (error) {
        failed.push(error.code || error.name);
      }
      session.savedReports.set(directory, saved);
    }
    if (failed.length && !session.historyError) console.warn(`无法保存助手输出 (${failed.join(', ')})`);
    session.historyError = failed.length > 0;
  }

  files(id) {
    if (!sessionName.test(id)) return [];
    const directory = path.join(this.directory, id);
    if (!fs.lstatSync(directory, {throwIfNoEntry: false})?.isDirectory()) return [];
    return entries(directory).filter(entry => entry.isFile() && reportName.test(entry.name))
      .map(entry => path.join(directory, entry.name));
  }

  list(active) {
    const sessions = [];
    for (const entry of entries(this.directory)) {
      if (!entry.isDirectory() || !sessionName.test(entry.name) || entry.name === active?.id) continue;
      const files = this.files(entry.name);
      if (!files.length) continue;
      const firstFile = files.reduce((first, file) => path.basename(file).localeCompare(path.basename(first), undefined, {numeric: true}) < 0 ? file : first);
      const first = JSON.parse(fs.readFileSync(firstFile, 'utf8'));
      sessions.push({id: entry.name, at: first.at, instruction: first.instruction, count: files.length});
    }
    if (active?.reports.length) sessions.push({id: active.id, at: active.reports[0].at,
      instruction: active.instruction, count: active.reports.length});
    return sessions.sort((left, right) => right.at - left.at);
  }

  read(id, active) {
    if (active?.id === id) return active.reports;
    return this.files(id).map(file => JSON.parse(fs.readFileSync(file, 'utf8')))
      .sort((left, right) => left.sequence - right.sequence);
  }
}

module.exports = BriefingHistory;
