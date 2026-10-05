# 吉他旋律指法规划后端209

上传参考与实奏两个 Standard MIDI File，提取指定轨道/通道的单旋律，
按各自速度图换算毫秒时间轴，再用动态规划做全局顺序对齐，返回总成本、
正确数与错音/漏奏/多奏三类错误明细，以及每对音符的起音与时长偏差。

## 运行

```bash
.venv/bin/python examples/make_examples.py   # 生成示例 MIDI
.venv/bin/python -m uvicorn guitar_route209.main:app --port 8000
```

测试与编译检查：

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/python -m compileall -q guitar_route209 tests examples
```

## 接口

### POST /compare

multipart/form-data：

| 字段 | 说明 |
| --- | --- |
| `reference` | 参考 SMF 文件 |
| `performance` | 实奏 SMF 文件 |
| `ref_track` / `perf_track` | 0 起轨号 |
| `ref_channel` / `perf_channel` | 通道 0–15 |

```bash
curl -s -F reference=@examples/reference.mid -F performance=@examples/performance.mid \
     -F ref_track=1 -F ref_channel=0 -F perf_track=1 -F perf_channel=0 \
     http://127.0.0.1:8000/compare
```

响应（200）要点：

- `total_cost`：对齐总成本；`correct_count`：同音高配对数。
- `pairs[]`：每个配对含 `ref_index`/`perf_index`、双方音高、
  `onset_ms_deviation` 与 `duration_ms_deviation`（实奏减参考，毫秒，
  均以各自文件 tick 0 为起点，不做自动平移）。
- `errors.wrong_pitch / missed / extra`：三类错误的 `count` 与 `items` 明细。
- `reference.notes[]` / `performance.notes[]`：每音的原事件序号
  （`event_index`，轨道内 0 起）、音高、起止 tick 与起止毫秒。

非法材料整次拒绝，返回 422，`detail` 定位 `file`
（reference/performance）、`track` 与 `event`：

```bash
curl -s -F reference=@examples/bad_overlap.mid -F performance=@examples/performance.mid \
     -F ref_track=0 -F ref_channel=0 -F perf_track=1 -F perf_channel=0 \
     http://127.0.0.1:8000/compare
# {"detail":{"message":"overlapping note for pitch 60","file":"reference","track":0,"event":1}}
```

请求之间相互独立，不保存任何状态。

### POST /revise

在 /compare 的同一对齐结果上做选择性修订，返回
`application/zip`（含 `revised.mid` 与 `audit.json`）；任何一步非法
则整次拒绝（422），不交付文件。

multipart/form-data 在 /compare 字段之外增加：

| 字段 | 说明 |
| --- | --- |
| `actions` | JSON 动作数组（1–1024 条），可直接给字符串或用 `-F actions=@file` 上传 |

动作按错误类别与参考/实奏音索引定位（索引来自 /compare 的
`errors` 明细），对齐只算一次，不随动作顺序重排：

- `{"op": "fix_pitch", "perf_index": N}`：把错音的实奏音开/关事件
  音高改为配对参考音高，保留起止 tick 与力度。
- `{"op": "delete_extra", "perf_index": N}`：删除多奏音的开/关事件。
- `{"op": "insert_missed", "ref_index": N}`：补漏奏。取参考音高与
  起止绝对时间（以 tick 0 计时，不平移），经实奏 tempo 分段用有理数
  精确反解 tick，四舍五入、半 tick 向上，开音力度 80。

拒绝不存在、重复或矛盾（同一实奏音既改又删）的动作；统一规划后，
取整得到零时长音、负时间或所选通道最终音符重叠均整次拒绝并在
`detail.action` 中定位动作序号。

输出保留实奏 Type、PPQN、全部轨道、tempo 及未修改事件的内容、绝对
tick 与相对顺序；同 tick 新增关音先于开音，插入越过轨尾时顺延
`end_of_track`；重建非负 delta；成品同样受 2 MiB / 40000 事件限制。
`audit.json` 记录两个源文件与成品的 SHA256 及每个动作的前后音高
与 tick。

```bash
curl -s -F reference=@examples/reference.mid -F performance=@examples/performance.mid \
     -F ref_track=1 -F ref_channel=0 -F perf_track=1 -F perf_channel=0 \
     -F actions=@examples/actions.json \
     http://127.0.0.1:8000/revise -o revision.zip

# 冲突动作整次拒绝（detail.action 定位）：
curl -s -F reference=@examples/reference.mid -F performance=@examples/performance.mid \
     -F ref_track=1 -F ref_channel=0 -F perf_track=1 -F perf_channel=0 \
     -F actions=@examples/actions_conflict.json \
     http://127.0.0.1:8000/revise
# {"detail":{"message":"conflicting actions for performance note 5",...,"action":1}}
```

### POST /plan

为单个 SMF 的指定轨道/通道单旋律做全曲指法规划，返回
`application/zip`（含 `annotated.mid` 与 `plan.json`）；无解整次拒绝
（422），不交付部分方案。

multipart/form-data：

| 字段 | 说明 |
| --- | --- |
| `file` | SMF 文件 |
| `track` / `channel` | 0 起轨号 / 通道 0–15 |
| `open_strings` | 可选，JSON 数组，六个 0–127 整数，覆盖 1–6 弦空弦音高（默认 `[64,59,55,50,45,40]`） |

```bash
curl -s -F file=@examples/plan_song.mid -F track=1 -F channel=0 \
     http://127.0.0.1:8000/plan -o plan.zip
```

规则：

- 品位 = 音高 − 空弦音高，仅 0–20 有效；把位 1–17，按弦品位须在
  把位至把位加 3 内，空弦适用任意把位。
- 每移动一品把位需 20ms。前音按弦时只能用其结束到后音开始的间隔
  换把；前音为空弦时可从其开始换把。时间用精确有理数 tempo 换算，
  等于所需时间即可达；首音把位自由。
- 全局动态规划依次最小化总把位移动、换弦次数；仍同值时按
  （弦号， 品位， 把位）三元组序列字典序取唯一解，不逐音贪心。
- 无解时 `detail.note` 定位首个无候选或无可达状态的音符，
  `detail.message` 说明原因。

`plan.json` 含源文件与成品 SHA256、每音的索引、`start_tick`、
弦/品/把位，以及 `total_position_movement` 与 `string_changes`
总代价。`annotated.mid` 在所选轨每个原开音前同 tick 插入 `text`
元事件（`plan: index=N string=S fret=F position=P`），保留原
Type、PPQN、事件字段、绝对 tick 与相对顺序，成品同样受
2 MiB / 40000 事件限制。

## 规则与边界

- 文件：仅 SMF Type 0/1 且正 PPQN；拒绝 Type 2 与 SMPTE 时间格式。
  每文件 ≤ 2 MiB、≤ 32 轨、≤ 40000 事件；旋律 1–1024 音。
- 单个 MIDI 消息长度上限与文件限制一致（2 MiB），合法长文本元事件不误拒。
- 音符：累积 delta tick；选定通道正力度 `note_on` 开音，`note_off` 与
  零力度 `note_on` 关音；同 tick 先关再开。拒绝孤立关音、未闭合音、
  零时长音与任意重叠音（同音高或异音高）。其他通道与踏板等事件忽略。
- 表单：非文件字段（如 `actions` 长文本）允许至 2 MiB，与文件限制一致。
- 时间：Type 1 从轨 0 取 tempo，Type 0 取唯一轨；缺省 500000 微秒/四分
  音符。拒绝零 tempo 与同 tick 重复 tempo。按速度段以有理数
  （Fraction）累加微秒再换算毫秒。
- 对齐：DP 全局顺序对齐。同音高配对代价 0、异音高配对 2、漏奏与多奏
  各 1；回溯同成本时优先配对，其次漏奏，最后多奏。每个音在配对、
  漏奏、多奏中恰出现一次。

## 结构

- `guitar_route209/midi_loader.py`：SMF 解析、限量校验、tempo 时间线、旋律提取。
- `guitar_route209/align.py`：动态规划对齐与错误分类。
- `guitar_route209/revise.py`：动作解析校验、修订规划、SMF 改写与 ZIP/审计输出。
- `guitar_route209/fingering.py`：全曲指法全局规划、注释 MIDI 与 ZIP 输出。
- `guitar_route209/main.py`：FastAPI 路由与 422 定位响应。
- `guitar_route209/errors.py`：携带文件/轨道/事件位置的拒绝异常。
- `examples/make_examples.py`：生成 `reference.mid`、`performance.mid`
  及两个反例文件。
- `tests/test_compare.py`：20 个用例覆盖对齐、时间换算与各类拒绝。

当前项目仓库：https://github.com/huangjie666777-ux/midi-guitar-positioning-209
