'use strict';
// 仅接受本机监控进程的完整新鲜快照；不采信浏览器提交的无人机状态。
module.exports = function planningFleet(state, now = Date.now()) {
  if (!state.connected || !Array.isArray(state.drones) || !state.drones.length ||
      state.drones.some(d => !Number.isFinite(d.updated_at) || now - d.updated_at > 1000 ||
        d.updated_at > now || typeof d.available !== 'boolean' ||
        !Array.isArray(d.position_m) || d.position_m.length !== 3 || !d.position_m.every(Number.isFinite))) return null;
  return {sampled_at: Math.min(...state.drones.map(d => d.updated_at)),
    drones: state.drones.map(({name, position_m, available}) => ({name, position_m, available}))};
};
