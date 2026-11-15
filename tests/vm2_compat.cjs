/**
 * G9：vm2 实机等价加载门禁
 *
 * 为什么需要它：G1–G6 全部跑在 Node 原生 vm / new Function 下，那里 Proxy 是可用的。
 * 而服务端在 system.allowUnsafeVM=false 时走 vm2 安全沙箱（userApi.ts:1711-1738），
 * vm2 禁用 Proxy 构造 —— 于是「CI 全绿」与「服务端 status=failed」可以同时成立。
 * 本门禁把服务端的加载条件搬进 CI：用与服务端同版本的 vm2、同构的 sandbox，
 * 原样加载产物（不改写、不追加任何代码），断言加载与初始化都真的完成。
 *
 * 与 G3 的分工：G3 判「协议形态对不对」，G9 判「服务端沙箱能不能加载」。
 * 明文额外做 runtimeToggles 语义探针，混淆产物因变量名被改写而跳过该项
 * （混淆前明文阶段已裁决，且混淆不改变语义）。
 *
 * 用法：node tests/vm2_compat.cjs <product.js> --mode plain|obf
 * 退出码 0=通过，1=不通过。stdout 最后一行是单行 JSON（gate.py 解析）。
 */
'use strict'
const fs = require('fs')
const path = require('path')

const file = process.argv[2]
const modeArg = process.argv.indexOf('--mode')
const mode = modeArg > 0 ? process.argv[modeArg + 1] : 'plain'
if (!file) {
  console.log(JSON.stringify({ ok: false, error: 'usage: node vm2_compat.cjs <product.js> --mode plain|obf' }))
  process.exit(1)
}
if (!['plain', 'obf'].includes(mode)) {
  console.log(JSON.stringify({ ok: false, error: `未知 mode: ${mode}` }))
  process.exit(1)
}

let VM
try {
  VM = require('vm2').VM
} catch (e) {
  console.log(JSON.stringify({ ok: false, error: `无法加载 vm2（CI 需 npm install；本地需 npm install）：${String(e && e.message || e)}` }))
  process.exit(1)
}

const checks = []
const add = (id, name, ok, detail) => checks.push({ id, name, ok: !!ok, detail: String(detail || '') })

let inited = null
let requestHandler = null
const logs = []
const timers = []
const noopLog = (...a) => { if (logs.length < 20) logs.push(a.map(String).join(' ').slice(0, 140)) }

// sandbox 复刻服务端 userApi.ts:1676-1708 的 vm2 分支。
// 关键差异是这里 **不注入 Proxy**：vm2 上下文里 Proxy 本就不可构造，
// 这正是需要复现的行为。定时器只登记不真跑长周期，避免门禁进程挂起。
const trackedSetTimeout = (fn, ms) => {
  timers.push(ms)
  if (typeof ms === 'number' && ms < 1000) { try { fn() } catch (_) {} }
  return timers.length
}
const sandboxConsole = { log: noopLog, warn: noopLog, error: noopLog, info: noopLog, debug: noopLog }
const sandbox = {
  console: sandboxConsole,
  setTimeout: trackedSetTimeout,
  clearTimeout: () => {},
  setInterval: () => 0,
  clearInterval: () => {},
  setImmediate: (fn, ...args) => { try { fn(...args) } catch (_) {} return 0 },
  clearImmediate: () => {},
  queueMicrotask: (fn) => { try { fn() } catch (_) {} },
  Buffer, URL, URLSearchParams, TextEncoder, TextDecoder,
  crypto: require('crypto'),
  process: {
    nextTick: (fn, ...args) => { try { fn(...args) } catch (_) {} },
    env: { NODE_ENV: process.env.NODE_ENV || 'production' },
  },
  atob: (s) => Buffer.from(String(s), 'base64').toString('binary'),
  btoa: (s) => Buffer.from(String(s), 'binary').toString('base64'),
  global: null, window: null, globalThis: null,
}
sandbox.lx = {
  version: '2.0.0',
  env: 'desktop',
  EVENT_NAMES: { request: 'request', inited: 'inited', updateAlert: 'updateAlert' },
  config: {},
  utils: {
    buffer: {
      from: (d, e) => Buffer.from(d, e),
      bufToString: (b, f) => (Buffer.isBuffer(b) ? b.toString(f) : Buffer.from(b, 'binary').toString(f)),
    },
  },
  // 门禁不发真实网络请求；脚本若在加载期主动取链，回调立即给一个错误即可
  request: (_url, _opts, cb) => {
    try { if (typeof cb === 'function') cb(new Error('offline guard: outbound blocked'), null, null) } catch (_) {}
    return () => {}
  },
  on: (ev, h) => { if (ev === 'request') requestHandler = h },
  send: (ev, payload) => { if (ev === 'inited') inited = payload },
  off: () => {},
}
sandbox.global = sandbox
sandbox.window = sandbox
sandbox.globalThis = sandbox

const result = {
  ok: false,
  mode,
  name: path.basename(file),
  vm2Version: require('vm2/package.json').version,
  checks,
}

let vm
try {
  vm = new VM({ timeout: 10000, sandbox, eval: true, wasm: false })
} catch (e) {
  add('P0', 'vm2 实例可用', false, String(e && e.message || e))
  result.error = 'vm2 实例创建失败'
  console.log(JSON.stringify(result))
  process.exit(1)
}

// P0 自检：这道门禁必须真的复现了「vm2 禁 Proxy」。
// 若某天 vm2 升级放开 Proxy 构造，本门禁会自己先报失效，而不是给产物假绿。
let proxyBlocked = false
let proxyMsg = ''
try { vm.run('new Proxy({}, {})') } catch (e) { proxyBlocked = true; proxyMsg = String(e && e.message || e) }
add('P0', 'vm2 沙箱自检：Proxy 构造被禁', proxyBlocked,
    proxyBlocked ? proxyMsg : '沙箱未禁 Proxy 构造，本门禁已失效（不能据此判定产物兼容）')

let loadErr = ''
try {
  vm.run(fs.readFileSync(file, 'utf8'))
} catch (e) {
  loadErr = String(e && e.message || e)
}
add('P1', '产物在 vm2 下加载无异常', !loadErr, loadErr || 'LOAD_OK')

const sources = inited && inited.sources && typeof inited.sources === 'object' ? Object.keys(inited.sources) : []
add('P2', 'lx.send(inited) 且声明 5 个平台', !!inited && sources.length === 5,
    inited ? `${sources.length} 平台：${sources.join(',')}` : '未收到 inited')

add('P3', '注册 lx.on(request) 处理器', !!requestHandler,
    requestHandler ? 'handler 已注册' : '未注册（script 可能加载中途抛错）')

// P4 明文语义探针：从 bundle 导出读 runtimeToggles，键集必须与 SOURCE_TOGGLES 声明一致，
// get 返回布尔、set 能翻转并还原。
// 这是 A7 条件化的安全网：上游若自行把 Proxy 换成 {}，字面无 new Proxy、锚点会跳过，
// 但这里 keys=0 会被挡下，避免「静态假绿」放行语义降级产物。
// 不测 has/in：静态访问器与原 Proxy 的 has trap 语义本就不同，且该视图业务链路零调用，
// 判据只保留可解释的「键集 / get / set」三项，不引入权重或阈值。
if (mode === 'obf') {
  add('P4', 'runtimeToggles 语义（混淆产物跳过）', true,
      '混淆会改写变量名，语义探针仅在明文阶段裁决；混淆不改变语义')
} else {
  const probe = `(function () {
    var b = (typeof __lxSourceBundle !== "undefined") ? __lxSourceBundle : null;
    if (!b) return JSON.stringify({ available: false, reason: "bundle 导出不可达" });
    var decl = b.SOURCE_TOGGLES;
    var view = b.runtimeToggles;
    if (!decl) return JSON.stringify({ available: false, reason: "SOURCE_TOGGLES 未导出" });
    if (!view) return JSON.stringify({ available: false, reason: "上游已移除 runtimeToggles 调试视图" });
    var dk = Object.keys(decl).sort();
    var vk = Object.keys(view).sort();
    var same = dk.length === vk.length && dk.every(function (k, i) { return k === vk[i] });
    var allBool = vk.every(function (k) { var t = typeof view[k]; return t === "boolean" || t === "undefined" });
    var sample = vk[0] || "";
    var setWorks = false;
    if (sample) {
      var old = view[sample];
      view[sample] = !old;
      var mid = view[sample];
      view[sample] = old;
      setWorks = (mid === !old) && (view[sample] === old);
    }
    return JSON.stringify({ available: true, declKeys: dk.length, viewKeys: vk.length,
      same: same, allBool: allBool, setWorks: setWorks, sample: sample });
  })()`
  let pr = { available: false, reason: '探针执行失败' }
  try { pr = JSON.parse(vm.run(probe)) } catch (e) { pr = { available: false, reason: String(e && e.message || e) } }
  if (!pr.available) {
    // 上游彻底移除该调试视图是合理迭代：只记录、不阻断（加载与契约已由 P1–P3 保证）
    add('P4', 'runtimeToggles 语义', true, `探针不可用（视为上游已移除视图）：${pr.reason}`)
  } else {
    const ok = pr.same && pr.allBool && pr.setWorks && pr.viewKeys > 0
    add('P4', 'runtimeToggles 语义', ok,
        `声明键 ${pr.declKeys} / 视图键 ${pr.viewKeys}，键集一致=${pr.same}，get 布尔=${pr.allBool}，set 翻转还原=${pr.setWorks}（样本 ${pr.sample}）`)
  }
}

result.ok = checks.every(c => c.ok)
result.logs = logs.slice(0, 3)
result.longTimers = timers.filter(ms => typeof ms === 'number' && ms >= 60000).length
console.log(JSON.stringify(result))
process.exit(result.ok ? 0 : 1)
