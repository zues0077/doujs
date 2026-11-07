/**
 * 离线协议探针（B2 门禁 G3）
 *
 * 不发任何真实网络请求：替换 SOURCE_HANDLERS 的 fn，长定时器只登记不执行。
 * 判据与 host 服务端完全同语义：
 *   - 官方协议：非 local 平台 actions 固定 ['musicUrl']
 *   - validateReturnedMusicUrl / normalizeProbeUrl：只接受字符串 URL
 *   - sourceGuard：≥60s 长定时器视为自排探测（P-0830 卡顿根因）
 *
 * 用法：
 *   node verify_compat.cjs <lx-source.js>               仅输出 JSON
 *   node verify_compat.cjs <lx-source.js> --expect server   断言并设置退出码
 */
const fs = require('fs')
const vm = require('vm')

const file = process.argv[2]
const expect = process.argv.includes('--expect') ? process.argv[process.argv.indexOf('--expect') + 1] : ''
if (!file) { console.error('usage: node verify_compat.cjs <lx-source.js> [--expect server]'); process.exit(64) }

const script = fs.readFileSync(file, 'utf8') +
  '\n;globalThis.__probe = { getUrlWithFallback, SOURCE_HANDLERS, runtimeToggles, sourceConfig };'

let inited = null
let handler = null
const timers = []
const logs = []

const noopLog = (...a) => logs.push(a.map(String).join(' ').slice(0, 160))
const sandbox = {
  console: { log: noopLog, warn: noopLog, error: noopLog, info: noopLog, debug: noopLog },
  setTimeout: (fn, ms) => {
    timers.push(ms)
    if (typeof ms === 'number' && ms < 1000) { try { fn() } catch (_) {} }
    return timers.length
  },
  clearTimeout: () => {},
  setInterval: () => 0,
  clearInterval: () => {},
  Buffer, URL, URLSearchParams, TextEncoder, TextDecoder,
  btoa: (s) => Buffer.from(String(s), 'binary').toString('base64'),
  atob: (s) => Buffer.from(String(s), 'base64').toString('binary'),
  module: { exports: {} },
}
sandbox.lx = {
  version: '2.0.0',
  env: 'desktop',
  EVENT_NAMES: { request: 'request', inited: 'inited', updateAlert: 'updateAlert' },
  utils: {
    buffer: {
      from: (d, e) => Buffer.from(d, e),
      bufToString: (b, f) => (Buffer.isBuffer(b) ? b.toString(f) : Buffer.from(b, 'binary').toString(f)),
    },
  },
  request: (_url, _opts, cb) => { setImmediate(() => cb(new Error('offline mock: outbound blocked'), null, null)); return () => {} },
  on: (ev, h) => { if (ev === 'request') handler = h },
  send: (ev, payload) => { if (ev === 'inited') inited = payload },
}
sandbox.global = sandbox
sandbox.window = sandbox
sandbox.globalThis = sandbox

const out = { file, expect }
try {
  vm.createContext(sandbox)
  vm.runInContext(script, sandbox, { timeout: 8000 })
} catch (e) {
  out.loadError = String((e && e.message) || e)
  console.log(JSON.stringify(out, null, 2))
  process.exit(1)
}

const p = sandbox.__probe
out.loaded = !!inited && !!handler
out.sources = inited ? Object.fromEntries(Object.entries(inited.sources).map(([k, v]) => [k, {
  actions: v.actions, qualitys: v.qualitys, type: v.type,
}])) : null
out.longTimers = timers.filter(ms => typeof ms === 'number' && ms >= 60000)
out.healthActionDeclared = inited ? Object.values(inited.sources).some(s => s.actions.includes('health')) : null
out.timerSamples = timers.slice(0, 20)

const serverValidate = (raw) => {
  if (!raw || typeof raw !== 'string') throw new Error('脚本未返回有效的链接字符串 / 脚本未返回有效播放链接')
  return raw.trim()
}

async function main() {
  const ids = Object.keys(p.SOURCE_HANDLERS)
  const preferred = ['suyinKuwo', 'kuwoOfficial', 'xinghaiBackend', 'ikun'].find(id => p.SOURCE_HANDLERS[id]?.fn)
  const replacedId = preferred || ids[0]
  for (const id of ids) {
    const h = p.SOURCE_HANDLERS[id]
    if (id === replacedId) h.fn = async () => 'https://cdn.example.com/fixture.mp3'
    else if (h && typeof h.fn === 'function') h.fn = async () => { throw new Error('offline mock: disabled') }
  }
  out.replacedHandler = replacedId
  out.handlerCount = ids.length

  try {
    const r = await p.getUrlWithFallback('kw', { name: '晴天', singer: '周杰伦', songmid: '228908' }, '128k')
    out.innerReturnType = typeof r
    out.innerReturnKeys = r && typeof r === 'object' ? Object.keys(r) : null
    out.innerUrl = r && r.url
  } catch (e) { out.innerError = String((e && e.message) || e) }

  try {
    const r = await handler({ action: 'musicUrl', source: 'kw', info: {
      musicInfo: { name: '海阔天空', singer: 'Beyond', songmid: '228909' }, type: '128k', quality: '128k',
    } })
    out.protocolReturnType = typeof r
    out.protocolReturnKeys = r && typeof r === 'object' ? Object.keys(r) : null
    try { out.serverWouldAccept = serverValidate(r) } catch (e) { out.serverVerdict = e.message }
  } catch (e) { out.protocolError = String((e && e.message) || e) }

  if (expect === 'server') {
    const fails = []
    if (!out.loaded) fails.push('未完成加载（inited + request handler 缺失）')
    if (out.longTimers && out.longTimers.length) fails.push(`存在 ≥60s 长定时器: ${JSON.stringify(out.longTimers)}`)
    if (out.healthActionDeclared) fails.push('仍对外声明 health action')
    const badActions = Object.entries(out.sources || {}).filter(([, v]) => v.type !== 'local' &&
      !(Array.isArray(v.actions) && v.actions.length === 1 && v.actions[0] === 'musicUrl'))
    if (badActions.length) fails.push(`非 local 平台 actions 不合规: ${badActions.map(([k]) => k).join(',')}`)
    if (out.protocolReturnType !== 'string') fails.push(`musicUrl 返回 ${out.protocolReturnType}，服务端只接受 string`)
    if (!out.serverWouldAccept) fails.push(`服务端校验不通过: ${out.serverVerdict || '无返回值'}`)
    out.assert = { ok: fails.length === 0, fails }
    console.log(JSON.stringify(out, null, 2))
    process.exit(fails.length === 0 ? 0 : 2)
  }

  console.log(JSON.stringify(out, null, 2))
}
main().catch(e => { console.error('FATAL', e); process.exit(1) })
