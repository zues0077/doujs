# doujs

一条无人值守的上游追新流水线：拉取上游脚本 → 套补丁契约 → 双层门禁 → 混淆 → 以 Release 附件分发。

它不是脚本的 fork，而是**补丁 + 门禁 + 分发**三件套。上游本体始终取原件，本仓库只维护那几处差异。

## 设计取向

| 问题 | 做法 |
| --- | --- |
| 上游迭代极快（一天多版） | 判据用**文件 blob sha**，不是仓库 head。head 一天前进多次，多数只改文档，脚本一字未变 |
| 上游一改，补丁就失效 | 差异全部外置到 `anchors.json`；锚点失配立刻阻断并生成修复工单，绝不产出半兼容产物 |
| 产物被代码搜索索引 | 产物**只随 Release 附件分发**，仓库树内不放任何 `.js`。GitHub 代码搜索只索引仓库内文件 |
| 产物被二次分发 | 分发前做中度混淆（短名 + 字符串数组编码），同时卡住下游判定的红线 |
| 上游来源被反查 | owner/repo 不写进代码，走仓库 Variables 注入（Variables 不参与代码索引） |

## 目录

```
anchors.json              补丁契约：锚点 + 全局哨兵（改这里，不改代码）
tools/pipeline.py         主编排：拉取 → 判据 → 补丁 → 门禁 → 混淆 → 复验 → 原子发布
tools/fetch_upstream.py   上游获取（curl；本地文件优先）
tools/patch.py            锚点匹配与替换，MATCH / DRIFT / SEMANTIC_DRIFT 三级判定
tools/gate.py             明文 6 项 + 混淆后 8 项门禁
tools/obfuscate.cjs       混淆器（profile: light / medium）
tools/obfuscate_step.py   混淆阶段封装（含三条硬约束自检）
tools/heartbeat.py        月度心跳：每月一次空提交，防止定时任务被自动禁用
tools/drift_report.py     RUN 报告 / 漂移工单 / CI Summary
tests/verify_compat.cjs   协议形态离线探针（与宿主服务端同语义）
tests/measure-artifact.cjs 产物判定（直接调用下游真实实现，不写等效复现）
tests/drift-drill.py      漂移演练（固化，可重复跑）
```

`dist/` 与 `state/build/` 不入库；产物只出现在 Release。

## 必要配置

仓库 Settings → Secrets and variables → Actions → **Variables**：

| 名称 | 说明 |
| --- | --- |
| `UPSTREAM_REPO` | 上游 `owner/repo`（必填） |
| `UPSTREAM_BRANCH` | 上游分支，默认 `master` |

上游标识刻意不写进代码：写进去就等于把来源公开在搜索结果里。

## 门禁（14 项，全过才发布）

**明文阶段（6 项）**

| 门 | 判据 |
| --- | --- |
| G1 语法 | `node --check` |
| G2 形态 | LF 行尾 / 头部元数据可解析 / 体积合理 |
| G3 协议探针 | 返回值必须是字符串；无长定时器；不声明非协议 action |
| G4 冒烟回归 | 上游 55 组用例全跑完、零未知断言失败 |
| G5 采集闸门 | 采集器 `scoreEntry ≥ 4` 且混淆风险为空 |
| G6 版本一致 | `@version` / manifest / 文件名对齐 |

**混淆后复验（8 项）** —— 混淆是不可逆步骤，必须整套重验

| 门 | 判据 |
| --- | --- |
| G1 / G2 / G6 | 同上，混淆后仍要成立 |
| G8 混淆风险 | `_0x` 密度 < 20/KB、无空转死循环、无自调度、无混淆器标识 |
| G7 混淆产物体检 | 直接调用下游 `checker.js` 与 `sourceGuard.js`：风险级别、评分、A/B 级判定 |
| G3 协议探针 | 混淆不得改变返回值形态 |
| G4 冒烟回归 | 混淆不得改变行为 |
| G5 采集闸门 | 混淆后评分仍达标、风险仍为空 |

任一失败：**不替换 `dist/`、不推进 `state.json`、必定写报告**。漂移期间采集器仍拿到上一版可用产物。

## 混淆红线（为什么是 medium 而不是更强）

下游对混淆有硬阈值，超了直接拒收：

| 判据 | 阈值 | 后果 |
| --- | --- | --- |
| `_0x` 标识符密度 | ≥ 20/KB | 判 high，**灌入前剔除** |
| `\uXXXX` 密度 | ≥ 8/KB | 判 high，剔除 |
| 空转死循环 / 混淆自调度 | ≥ 1 | 判 high，剔除 |
| 服务端 A 级特征 | 命中即隔离 | 隔离 |

所以 `hexadecimal` 生成器不可用：2600 行脚本会生成 4500+ 个 `_0x`，
实测密度 29.7/KB，直接越线。`medium` 档改用 `mangled` 短名 + 字符串数组 base64，
`_0x` 归零、体积反而缩小、行为等价。

必须关闭：`controlFlowFlattening` / `deadCodeInjection` / `debugProtection` / `selfDefending` /
`unicodeEscapeSequence` / `transformObjectKeys` / `renameProperties`（后者会破坏宿主协议属性名）。

必须保留：`/*! ... */` banner（下游靠它解析 `@name` / `@version`）、`globalThis.lx`、
`EVENT_NAMES`、`musicUrl` 等协议字面量、`__lxSourceConfig`（双形态测试开关）。

`seed` 固定，同输入同输出，产物可 diff。

## 锚点失配时会发生什么

```
上游改动 → patch.py 逐锚点判定
  MATCH            → 继续
  DRIFT            → 文本不匹配，difflib 找最相似位置（含相似度）→ 退出码 2
  SEMANTIC_DRIFT   → 文本匹配了但 post_checks 语义断言失败 → 退出码 2
```

产出 `reports/ANCHOR-DRIFT-<blob>.md`：期望文本、上游实际代码 ±8 行（带相似度）、
影响的是哪一层、六步修复清单。

五路告警：`latest.json` + `RUN-*.md` + 漂移工单 + Actions Job Summary + 自动开/追加 Issue
（label `anchor-drift`）。构建成功后自动关闭遗留 Issue。

## 运行历史活过 30 天（`state/history.jsonl`）

artifact 只保留 30 天，`RUN-*.md` 又被 gitignore —— 40 天后想回溯「那天为什么挂」就没有证据了；
`latest.json` 是覆盖写，只有"这一刻"，看不出趋势。这条时间线补上：

- **稀疏写入**：只在**判定结果发生变化**时追加一行（几十字节）。连续 `NO_CHANGE` 的日子
  只在进入那天记一行，期间不产生 diff，**不会每天都冒出一个提交**；
- **每次都记**：`BUILD_OK` 与任何失败（连续失败的每一次都记，用来算连续失败计数）；
- **只保留最近 200 条**，一行紧凑 JSON，与 `git log` 同寿命；
- Job Summary 首屏会显示「连续无变化自 X（已 N 天）｜上次构建成功 Y（N 天前）」——
  这是区分「跑了但上游一直没变」和「根本没跑」的唯一依据；
- **脱敏**：只含 blob 短 sha / 版本号 / 产物 sha256 短值 / 状态与退出码，
  **不含上游 owner/repo/URL**（本仓库 public，这条是硬约束）；
- 本地复现默认不写（`GITHUB_ACTIONS` 未设置），要本地验证用 `DOUJS_HISTORY=on`。

## 定时任务不会自己停摆（月度心跳）

GitHub 有一条容易忽略的规则：**public 仓库连续 60 天没有仓库活动，`schedule` 会被自动禁用**。
一旦被禁用，上游就算更新了也不会有任何任务去跑门禁、发 Release，表现就是"静默停更"，
而且不会收到任何通知——只有你主动翻 Actions 页面才会发现它已经不跑了。

`tools/heartbeat.py` 把空窗压到 31 天以内：每月第一次运行时写入 `state/heartbeat.json`，
当月后续运行全部跳过。正常情况下，每月恰好产生一次
`chore: monthly heartbeat YYYY-MM` 提交，其余运行不产生任何提交。

- 不依赖构建结果：步骤条件是 `!cancelled()`，锚点漂移导致 job 标红时照样提交；
- `dry_run` 时不写、不提交；
- 本地验证：`python tools/heartbeat.py --dry-run`，跨月行为可加
  `--now 2027-01-01T00:00:00+00:00`。

判据：如果连续两个月连这条提交都没有，说明定时已经不可靠了，去 Actions 页面手动
`Run workflow` 一次即可恢复。

## 本地复现

```bash
# 1) 依赖
npm install                 # 锁 javascript-obfuscator 4.2.2

# 2) 离线全链路（不联网、不落盘）
python tools/pipeline.py \
  --upstream-file /path/to/lx-source.js \
  --smoke-test   /path/to/lx-source-smoke-test.js \
  --dry-run

# 3) 正式落盘
python tools/pipeline.py --upstream-file ... --smoke-test ...

# 4) 只看协议形态
node tests/verify_compat.cjs dist/doujs.js --expect server

# 5) 只看产物判定（需要本机能找到下游判据源码）
COLLECTOR_CHECKER=/path/to/checker.js \
HOST_GUARD=/path/to/sourceGuard.js \
node tests/measure-artifact.cjs dist/doujs.js --name doujs.js

# 6) 漂移演练（验证「上游改了会拦住」这件事本身没坏）
python tests/drift-drill.py --upstream /path/to/lx-source.js --smoke-test /path/to/smoke.js
```

退出码：`0` 成功/无需重建 · `2` 锚点漂移 · `3` 门禁失败 · `4` 拉取失败。

## 采集器接入

产物已适配采集器的两道闸门（`scoreEntry ≥ 4`、混淆风险为空），实测 `scoreEntry = 8`。

接入方式：把 `<owner>/<repo>` 加进采集器仓库列表。树内没有 `.js`，会走「Release 资产分发」分支判定。
不提供网页入口 —— 单一入口少一处会腐化的东西，也不用额外维护页面与版本号的一致性。

硬约束：**`@name` 与文件名必须长期固定**，改名会被当成新条目重复入库；文件名不带版本号，
版本只放 `@version` 与 `manifest.json`。Release tag 用 `v<version>`，版本变化即触发更新。

## 合规

上游无 LICENSE 文件且声明禁止商用。本仓库只保存补丁契约与构建流程，不复制、不分发上游原件；
产物由 CI 从上游原件现场生成，本仓库不主张任何权利。

公开分发前请自行确认授权边界 —— 这一条不做技术兜底，属于使用者的判断。

## 刻意不做的

- **不做上游副本**：不 fork 脚本本体，避免上游修复被挡在副本外；
- **不做服务端改造**：宿主侧通用解包层是另一条独立线，不属于本仓库；
- **不自动推广**：构建成功只更新产物与 Release，不会自动部署到任何运行环境；
- **不隐藏仓库存在**：公开仓库的 workflow 与注释仍可被人读到，只能做到「不含用途词、不含上游标识」。
  要做到完全不被发现，唯一办法是私有仓库。
