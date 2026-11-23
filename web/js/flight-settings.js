'use strict';

(() => {
  const speed = defaultValue => ({
    label: '飞行速度上限', unit: '米/秒', defaultValue,
    description: '起降、转弯和现场对位按航段限速。'
  });
  const navigation = {
    max_height: {
      label: '导航高度上限', unit: '米', min: 15, defaultValue: 60,
      description: '相对当地地面，用于巡查、送药和处置航线。'
    },
    flight_speed: speed(30)
  };
  const tasks = {
    search: {...navigation,
      flight_height: {label: '低空巡查高度', unit: '米', defaultValue: 15},
      capture_interval_m: {label: '拍照间距', unit: '米', defaultValue: 15}
    },
    monitor: {
      flight_height: {label: '监控高度下限', unit: '米', defaultValue: 200},
      flight_speed: speed(25)
    },
    pursuit: {flight_speed: navigation.flight_speed},
    delivery: navigation,
    response: navigation
  };
  if (typeof module === 'object' && module.exports) module.exports = tasks;
  else window.fleetFlightFields = navigation;
})();
