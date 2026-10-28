# web 项目结构

`web/` 是城市仿真的操作界面。在项目根目录运行 `node web/serve.cjs` 启动。

```text
web/
├─ README.md                  目录与文件职责说明
├─ index.html                 页面结构
├─ serve.cjs                  网页服务入口
├─ js/                        浏览器脚本
│  ├─ map.js                  地图交互
│  ├─ airsim.js               无人机实时监控
│  ├─ llm.js                  自然语言任务交互
│  └─ abnormal.js             异常场景交互
├─ css/                       页面样式
│  └─ styles.css              页面外观
├─ server/                    Node.js 后端
│  ├─ airsim.cjs              无人机监控接口
│  ├─ llm.cjs                 任务规划与搜索调度
│  ├─ abnormal.cjs            异常场景接口
│  └─ python-runtime.cjs      启动 Python 进程并收发请求
├─ utils/                     Python 桥接工具
│  ├─ python_bridge.py        Python 功能调用入口
│  ├─ airsim_bridge.py        从 AirSim 采集无人机状态和画面
│  └─ unreal_bridge.py        UE 内的异常目标观测与跟随取景
└─ assets/                    静态素材
   └─ town12.png              地图实景底图
```

`js/` 与 `server/` 的同名文件按功能对应。`map.js` 直接读取静态地图数据。

## 子文件夹分工

| 目录 | 功能划分 |
|---|---|
| `js/` | 前端部分，实现页面交互。 |
| `css/` | 定义页面外观。 |
| `server/` | 后端部分，处理网页请求。 |
| `utils/` | 脚本工具，连接 Python 功能与仿真环境。 |
| `assets/` | 存放页面素材。 |
