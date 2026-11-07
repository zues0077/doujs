#!/usr/bin/env node
/**
 * 产物混淆器：把明文产物转成分发用的混淆版。
 *
 *   node tools/obfuscate.cjs <输入.js> <输出.js> [--profile medium|light] [--seed N] [--json]
 *
 * 设计约束（全部来自下游真实判定，见 tests/measure-artifact.cjs）：
 *   1. 必须保留 /*! ... *\/ banner —— collector extractMeta 靠它解析 name/version。
 *   2. 不能产生空转死循环 / debugger 陷阱 / Function('return this') / 多个 ≤16ms 定时器
 *      —— host sourceGuard 判 A 级会直接隔离。
 *   3. `_0x` 标识符密度必须 < 20/KB —— collector obfuscationRisk 判 high 会在灌入前剔除。
 *      156KB 产物约 3050 个 `_0x` 就是红线，本工具跑完会自报密度。
 *   4. 必须保留 `globalThis.lx`、`EVENT_NAMES`、`lx.on(`、`lx.request` 等明文特征
 *      —— collector scoreEntry 低于 4 会判非脚本。
 *   5. 行长 ≤ 5000（服务端记 B 级 long_line），因此 compact 恒为 false。
 *   6. 固定 seed，同输入同输出，便于 diff 与审计。
 *
 * profile:
 *   light  —— 只做标识符重命名，不动字符串。体积几乎不变，混淆痕迹最轻。
 *   medium —— 标识符重命名 + 字符串数组（不编码）。体积上升，`_0x` 密度需实测。
 */
'use strict'

const fs = require('fs')
const path = require('path')

const OBFUSCATOR = 'javascript-obfuscator'

// 这些标识符/字符串必须在产物里保持明文，否则下游判定或运行时立刻出问题：
//   lx / EVENT_NAMES / request / inited / updateAlert —— 宿主协议，改了就取不到
//   musicUrl / musicInfo / source / quality / type —— 动作与参数协议
//   __lxSourceConfig —— 我们自己的双形态测试开关
const RESERVED = [
  '^lx$', '^globalThis$', '^__lxSourceConfig$',
  '^EVENT_NAMES$', '^musicUrl$', '^musicInfo$', '^updateAlert$',
  '^request$', '^inited$', '^source$', '^quality$', '^type$',
  '^name$', '^singer$', '^songmid$', '^interval$', '^result$', '^code$',
]

const PROFILES = {
  light: {
    compact: false,
    identifierNamesGenerator: 'hexadecimal',
    renameGlobals: false,
    renameProperties: false,
    stringArray: false,
    controlFlowFlattening: false,
    deadCodeInjection: false,
    debugProtection: false,
    selfDefending: false,
    disableConsoleOutput: false,
    unicodeEscapeSequence: false,
    transformObjectKeys: false,
    numbersToExpressions: false,
    splitStrings: false,
    simplify: true,
  },
  medium: {
    compact: false,
    identifierNamesGenerator: 'mangled',
    identifiersPrefix: 'd',
    renameGlobals: false,
    renameProperties: false,
    stringArray: true,
    stringArrayThreshold: 0.75,
    stringArrayEncoding: ['base64'],
    stringArrayRotate: true,
    stringArrayShuffle: true,
    stringArrayWrappersCount: 1,
    stringArrayWrappersType: 'variable',
    stringArrayWrappersChainedCalls: false,
    splitStrings: false,
    controlFlowFlattening: false,
    deadCodeInjection: false,
    debugProtection: false,
    selfDefending: false,
    disableConsoleOutput: false,
    unicodeEscapeSequence: false,
    transformObjectKeys: false,
    numbersToExpressions: false,
    simplify: true,
  },
}

function parseArgs(argv) {
  const out = { input: '', output: '', profile: 'medium', seed: 20261107, json: false }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (a === '--profile') out.profile = argv[++i] || 'medium'
    else if (a === '--seed') out.seed = Number(argv[++i]) || 20261107
    else if (a === '--json') out.json = true
    else if (!a.startsWith('--')) {
      if (!out.input) out.input = a
      else if (!out.output) out.output = a
    }
  }
  return out
}

/** 统计 `_0x` 密度，用于预判猎手判级（与 checker.js 同口径：每 KB 个数） */
function hexDensity(code) {
  const n = (code.match(/_0x[0-9a-f]{4,6}/gi) || []).length
  const kb = Math.max(1, code.length / 1024)
  return { total: n, perKb: Math.round((n / kb) * 100) / 100 }
}

function maxLineLength(code) {
  let max = 0
  let start = 0
  for (let i = 0; i < code.length; i++) {
    if (code.charCodeAt(i) === 10) {
      const len = i - start
      if (len > max) max = len
      start = i + 1
    }
  }
  const tail = code.length - start
  return tail > max ? tail : max
}

async function main() {
  const args = parseArgs(process.argv.slice(2))
  if (!args.input || !args.output) {
    console.error('用法: node tools/obfuscate.cjs <输入.js> <输出.js> [--profile medium|light] [--seed N] [--json]')
    process.exit(2)
  }
  const profile = PROFILES[args.profile]
  if (!profile) {
    console.error(`未知 profile: ${args.profile}（可选 ${Object.keys(PROFILES).join(' / ')}）`)
    process.exit(2)
  }

  const source = fs.readFileSync(args.input, 'utf8')
  const banner = source.match(/\/\*![\s\S]*?\*\//)
  if (!banner) {
    console.error('输入缺少 /*! ... */ banner：collector extractMeta 会解析不到 @name/@version，拒绝混淆')
    process.exit(4)
  }

  let JavaScriptObfuscator
  try {
    JavaScriptObfuscator = require(OBFUSCATOR)
  } catch (e) {
    console.error('未安装 javascript-obfuscator：请在仓库根执行 npm install javascript-obfuscator@4')
    process.exit(4)
  }

  const opts = Object.assign({}, profile, {
    seed: args.seed,
    reservedNames: RESERVED,
    // 保留 banner 与 @preserve 注释
    preserveAnnotations: true,
  })

  const t0 = Date.now()
  const out = JavaScriptObfuscator.obfuscate(source, opts).getObfuscatedCode()
  const elapsed = Date.now() - t0

  // 统一 LF + 末尾单换行
  let text = out.replace(/\r\n/g, '\n')
  if (!text.endsWith('\n')) text += '\n'

  // 兜底：混淆器若吞掉 banner，补回去（extractMeta 依赖它）
  if (!/\/\*![\s\S]*?\*\//.test(text)) {
    text = banner[0] + '\n' + text
  }

  fs.mkdirSync(path.dirname(path.resolve(args.output)), { recursive: true })
  fs.writeFileSync(args.output, text, { encoding: 'utf8' })

  const stats = {
    profile: args.profile,
    seed: args.seed,
    input_bytes: Buffer.byteLength(source),
    output_bytes: Buffer.byteLength(text),
    ratio: Math.round((text.length / source.length) * 100) / 100,
    max_line_length: maxLineLength(text),
    hex: hexDensity(text),
    banner_preserved: banner[0] === (text.match(/\/\*![\s\S]*?\*\//) || [''])[0],
    elapsed_ms: elapsed,
    output: path.basename(args.output),
  }
  if (args.json) process.stdout.write(JSON.stringify(stats, null, 2) + '\n')
  else {
    console.log(`profile=${stats.profile} seed=${stats.seed}`)
    console.log(`体积 ${stats.input_bytes} → ${stats.output_bytes}B (${stats.ratio}x)`)
    console.log(`最长行 ${stats.max_line_length}（限 5000）`)
    console.log(`_0x 标识符 ${stats.hex.total} 个 = ${stats.hex.perKb}/KB（猎手 high 红线 20/KB）`)
    console.log(`banner 保留 ${stats.banner_preserved ? '是' : '否'}`)
    console.log(`耗时 ${stats.elapsed_ms}ms`)
  }
}

main().catch(e => {
  console.error('混淆失败：' + String((e && e.message) || e))
  process.exit(4)
})
