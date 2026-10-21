# LLM：区域任务规划与图片分析

使用基础 OpenAI SDK，不引入 Agent 框架。三个模块分别负责：

- `Planner.plan()`：自然语言 → 查询本地地图 → 模型确认 → 生成 `SearchArea` 指令，只规划。
- `MissionExecutor`：接收通过校验的规划，核实无人机状态后调用 `UAV.API.search.search()` 实际搜索。
- `VisionAnalyzer`：图片 → 三类异常判断，不控制无人机。飞行和图片积压由 `UAV/` 管理。

## 运行

在项目根目录执行：

```powershell
python -m pip install -r llm/requirements.txt
python -m llm "让 UAV_01 搜索地图东边的学校"
```

配置放在 `llm/.env`：`OPENAI_API_KEY`、`OPENAI_MODEL`、可选 `OPENAI_BASE_URL`。已有环境变量优先，空地址使用官方服务。视觉可用 `OPENAI_VISION_MODEL` 单独指定模型，否则使用 `OPENAI_MODEL`。服务需要支持 Responses API、function calling 和 JSON 模式。

规划与视觉共用 `functions/client.py` 创建SDK连接、读取配置并转换错误提示。规划请求超时60秒、SDK最多重试1次；视觉超时25秒、SDK不重试，识别重试由UAV后台管理。两种请求都保持 `temperature=0`、`reasoning.effort=minimal`。这些是网络请求配置，不保证固定的端到端耗时。

## 文件职责

```text
llm/
├── __main__.py             命令行入口、参数与JSON读取
├── planner.py              工具定义与执行、最多5轮的查询判断循环
├── executor.py             规划执行层、无人机队列、飞行状态与取消
├── vision.py               可复用的图片分析器
├── prompts/
│   ├── search_area.txt     区域任务提示词
│   └── search_vision.txt   三类异常提示词
└── functions/
    ├── client.py           配置、SDK连接、错误提示
    ├── worker.py           网页常驻进程入口
    ├── fleet.py            无人机快照校验、来源选择、实时读取
    ├── catalog.py          地图索引、区域查询与空间计算
    ├── commands.py         任务格式、输入与指令校验、资源分配
    └── trace.py            按需保存逐轮输入输出
```

运行入口、流程代码和提示词各自独立；`functions/` 仅保留6个辅助模块。命令行参数归入口、模型工具归规划循环、几何计算归地图查询、任务格式归指令模块，不保留旧模块转发包装。

## 规划流程

```text
命令行 / 网页请求
  → 校验输入与对话，读取本次无人机快照
  → 第1轮模型调用 query_map
  → Python查询真实地图，返回候选区域和距离
  → 第2轮模型阅读结果：确认、继续查询或提出必要的问题
  → 本地核实区域来源、搜索范围、完整性和资源，返回指令
```

最多5次模型调用，包含最终确认；第5轮禁止继续查询。校验失败的反馈也计入轮数。每次用户补充重新规划，不能使用历史目标绕过本次地图查询。

模型通常只需确认最新成功结果：

```json
{"use_query_result":true,"reply":"已确认第一学校周边50米。"}
```

本地展开为 `SearchArea` 指令；`target`保留真实 `zone_id`，周边任务带 `scope=nearby`、`buffer_m`。模型也可通过 `tasks` 组合多个查询。区域与范围必须已查询确认；`selection=all`的结果不能漏项。无人机可顺序搜索多个区域，区域数大于无人机数无需追问，`sequence`由本地生成。规划结果保持 `executed=false`；显式执行时另返回 `execution`，由执行层记录实际状态。

## 从自然语言到实际搜索

网页直接输入即可运行：启动 `node web/serve.cjs`，打开右侧「LLM任务」，确认飞行高度（默认40米），点击「规划并开始搜索」。服务器将通过校验的规划自动交给共享执行器，无需再执行终端命令，也不额外调用模型。AirSim/Unreal 及 Python 环境需要已经可用。

页面显示逐区域执行状态，运行时禁止重复提交；「停止任务」请求停止后继续显示真实状态，直到飞行线程退出。刷新页面会恢复当前任务，不重新派发。飞行完成不代表图片分析已完成，识别仍在搜索后台独立进行。服务重启不恢复任务；正常关闭服务会请求取消当前飞行并等待停止。

在项目根目录执行（会真实控制 AirSim 无人机）：

```powershell
python -m llm "现在是学生放学时间，需要重点关注学校附近的情况" --execute --height 40
# 可选的本地飞行设置，默认5米/秒、每3秒拍照
python -m llm "搜索第一学校附近" --execute --height 40 --speed 5 --interval 3
```

`--height` 是相对起飞地面的高度，由调用方指定；示例40米需要符合当前场景建筑净空，执行时仍由现有搜索接口检查。实际执行依赖 AirSim、Unreal 和 UAV 模块的运行环境，不接受手工 `--drones` 或离线快照。省略 `--execute` 仍然只规划。

流程为：真实无人机快照 → 最多5轮规划 → 本地校验 → 执行前重新核实真实状态 → 不同无人机并行、同一架按 `sequence` 顺序调用 `search()` → 拍照和识别由现有搜索流程自动启动。一次区域失败会跳过该机后续区域，其他无人机继续；取消请求会阻止剩余任务并通知当前搜索在检查点停止。后续区域从该机当前悬停位置继续，不重复要求落地。未生成有效规划则不启动执行器。

控制台最终输出包含 `plan` 和 `execution`。`execution.status` 为 `running/cancelling/completed/failed/cancelled`，`tasks` 记录逐区域状态（包括排队与跳过），`executed` 表示已调用搜索接口，不代表每个区域都已完成。按 Ctrl+C 请求取消后会等待现有搜索函数停止。完成退出码为0，飞行失败或取消为1。

执行器只等待飞行队列，不等待图片分析。`analysis_status=managed_by_search` 表示识别由 UAV 搜索后台管理，不代表已识别完成；图片、逐帧结果和报告仍在 `UAV/search_results/`。底层非守护识别线程可能使 Python 进程在飞行结束并输出结果后继续存活，直到积压图片处理完成。

程序调用时复用一个执行器实例：

```python
from llm.executor import MissionExecutor

executor = MissionExecutor()
mission = executor.start(plan, flight_height=40)  # plan 是 Planner.plan() 的 ready 结果
state = executor.status(mission["mission_id"])
# executor.cancel(mission["mission_id"])         # 请求取消，不等于已经停止
# state = executor.wait()                      # 显式等待飞行队列结束
```

每个实例一次只接收一批任务；状态保存在进程内，新任务会替换已完成任务的状态，重启后不恢复，也不自动重试飞行。应用应集中使用一个执行器实例管理派发；当前不是跨进程任务调度器。

`python -m llm.functions.worker` 同时提供同进程后台协议（每行一个 JSON）：

```json
{"id":1,"operation":"execute","instruction":"放学时间关注学校附近","flight":{"flight_height":40}}
```

规划成功后返回 `result.plan` 和 `result.execution`，后者含 `mission_id`。随后使用相同常驻进程查询或取消，不再调用模型：

```json
{"id":2,"operation":"status","mission_id":"替换成实际返回的ID"}
{"id":3,"operation":"cancel","mission_id":"替换成实际返回的ID"}
```

未带 `operation` 的请求仍只规划。网页先复用规划进程得到结果，再通过独立的 `--execution-only` 常驻进程发送 `dispatch`（包含 `plan` 和 `flight`），因此不重复规划。`status/cancel` 不调用模型；执行进程不会随网页请求断开或超时而被杀掉。执行进程标准输入关闭时，会请求取消并等待飞行队列停止。

网页的「继续补充」通过 `conversation` 携带历史，最新修改优先；最多6次完整历史问答、总文本16000字符。历史助手误加的条件不能替代用户条件。详见 [模型输入说明](PROMPT_GUIDE.md)。

## 地图与无人机

完整地图有375个对象。初始输入只发20类定义、13个设施及用户明确提到的区域摘要；全部对象可通过 `query_map` 查询。坐标和边界留在本地，方向约定为右东 +X、上北 -Y，不代表地理真北。区域间距离按真实多边形计算，无人机距离按当前位置到区域计算。

- “搜索学校附近”：两所学校边界外50米环带；指定第一学校则只查询该校。明确100米时以100米为准。
- “重点关注无人机保障基地”等设施关注任务：未明确内部时巡查设施周边50米，并在回答中说明范围；明确内部则不自动扩大。沿道路搜索要求范围内存在可巡查道路，设施内部可能没有道路。
- “搜索图书馆附近的居住区”：查询距图书馆边界50米以内的居住区；明确200米时使用200米。
- `buffer_m`定义原目标的周边宽度；`radius_m`筛选其他目标地块，二者不能混用。
- 目标不存在、名称有歧义、最近并列、缺少必要参照时，模型依据工具结果提出问题。

CLI默认复用 `UAV/utils/airsim_client.py` 读取真实无人机和UE世界米制当前位置；需要AirSim与Unreal编辑器运行。落地且速度不超过0.1米/秒才作为可用候选，不把出生点当当前位置，也不提供跨进程任务预留。执行器自行从 `Map/functional_zones.json` 读取边界并处理搜索范围。

## 命令行与过程记录

```powershell
# 查看实时无人机，不调用模型
python -m llm --show-fleet
# 显式指定可用ID，离线预览完整模型输入
python -m llm "搜索学校附近" --drones UAV_01 --show-prompt
# 只验证本地查询与指令生成，不调用模型或AirSim
python -m llm --intent-file "你的任务.json" --drones UAV_01
python -m llm --inspect education
# 使用手工提供的UE世界米制快照
python -m llm "搜索离 UAV_01 最近的学校" --fleet-file "你的无人机快照.json"
# 调用模型，并保存每轮实际系统提示词、输入、原始输出及工具返回
python -m llm "搜索第一学校附近" --trace-file llm/traces/my-run.md
```

`--trace-file`按需开启，路径必须位于 `llm/` 内且扩展名为 `.md`。默认不保存模型请求，失败时也会写出已有过程。记录可按需生成到 `llm/traces/`，不提交Git。

## 网页与视觉调用

网页使用 `python -m llm.functions.worker` 常驻进程，复用SDK连接与只读地图；每行一个独立JSON请求，任务上下文不跨请求缓存。该入口及协议保持不变。`python -m llm --request-stdin`仍支持单次标准输入请求。

网页可传入本机监控的实时无人机快照，超过时效则重新读取。修改Python代码或地图后需重启网页服务；`.env`变更会在下次网页提交前重建后台进程。

UAV通过 `from llm.vision import VisionAnalyzer` 调用图片分析器；同一实例复用连接，用完调用 `close()`。每张图片仅判断：需要紧急医疗救援的病人、疑似持刀危险人员、可疑（超速）车辆。返回 `status=normal/abnormal/uncertain` 和 `description`；单张图片无法确认车速或病情，判断证据要求见提示词。

## 单独测试图片识别

在项目根目录执行，图片路径替换成实际的本地 JPEG/PNG：

```powershell
python -m llm.vision "G:\图片目录\test.jpg"
# 也支持直接运行文件
python llm/vision.py "G:\图片目录\test.jpg"
# 可选：只在本次测试中切换模型，不修改 .env
python -m llm.vision "G:\图片目录\test.jpg" --model "你的视觉模型名称"
```

默认读取 `llm/.env`，模型选择与 `VisionAnalyzer` 一致。每次分析一张图片，控制台输出图片路径、实际模型、`status`、`description` 和 `elapsed_s`（含初始化、请求和连接关闭的耗时，不含 Python 启动）。这是直接调用真实模型的测试，不需要 AirSim，不启动无人机，不修改图片，也不保存结果文件。失败时向标准错误输出错误 JSON，退出码为1；成功为0。
