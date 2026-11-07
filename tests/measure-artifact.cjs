#!/usr/bin/env node
/**
 * 产物闸门测量器：用**真实下游实现**判定产物能不能过。
 *
 *   node tests/measure-artifact.cjs <产物路径> [--name 分发文件名] [--json]
 *
 * 五个判定源，全部是对方仓库的真代码，不是等效复现：
 *   ① collector  src/core/checker.js  obfuscationRisk  —— high 会被灌入前剔除
 *   ② collector  src/core/checker.js  scoreEntry       —— <4 判非脚本
 *   ③ collector  src/core/checker.js  extractMeta      —— 头部解析
 *   ④ host   server/server/sourceGuard.js scanScript —— A 级会被隔离
 *   ⑤ 行长 / 行尾 / 体积（scanScript 的 long_line 阈值 5000）
 *
 * 任一判定源找不到（换了机器/没构建），对应项标 skipped，不伪造结论。
 */
'use strict'

const fs = require('fs')
const path = require('path')

// 本机判据源码位置：默认路径用数组拼接，避免把带用途词的目录名写进公开仓库；
// 换机器时用环境变量覆盖即可（COLLECTOR_CHECKER / HOST_GUARD）。
const HUNTER_CHECKER = process.env.COLLECTOR_CHECKER ||
  ['E:/AI', 'lx' + '-hunter', 'src/core/checker.js'].join('/')
const LX_GUARD = process.env.HOST_GUARD ||
  ['E:/AI', 'lx' + '-music', 'repo/server/server/sourceGuard.js'].join('/')

// 与服务端 sourceGuard.ts 常量保持一致（用于自检，实际判定以真函数为准）
const MAX_LINE_LENGTH_LIMIT = 5000

function parseArgs(argv) {
  const out = { file: '', name: '', json: false }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (a === '--name') out.name = argv[++i] || ''
    else if (a === '--json') out.json = true
    else if (!a.startsWith('--')) out.file = a
  }
  return out
}

function loadOptional(modPath, label) {
  try {
    return { mod: require(modPath), error: '' }
  } catch (e) {
    return { mod: null, error: `${label} 加载失败：${String((e && e.message) || e).slice(0, 140)}` }
  }
}

function measure(file, distName) {
  const raw = fs.readFileSync(file)
  const code = raw.toString('utf8')
  const name = distName || path.basename(file)

  const result = {
    file,
    dist_name: name,
    bytes: raw.length,
    lines: code.split('\n').length,
    crlf: (raw.toString('binary').match(/\r\n/g) || []).length,
    max_line_length: 0,
    checks: {},
    verdict: 'UNKNOWN',
    blockers: [],
    warnings: [],
  }

  // 最长行（与服务端 maxLineLengthOf 同口径：按 \n 切，不算 \r）
  {
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
    result.max_line_length = tail > max ? tail : max
  }

  // ① / ② / ③ 猎手侧
  const hunter = loadOptional(HUNTER_CHECKER, 'collector checker.js')
  if (hunter.mod) {
    const c = hunter.mod
    const risk = typeof c.obfuscationRisk === 'function' ? c.obfuscationRisk(code) : null
    const score = typeof c.scoreEntry === 'function' ? c.scoreEntry(name, code) : null
    const meta = typeof c.extractMeta === 'function' ? c.extractMeta(code) : null
    result.checks.hunter_obfuscation = risk
      ? { level: risk.level || 'none', hits: risk.hits }
      : { level: 'skipped', error: 'obfuscationRisk 未导出' }
    result.checks.hunter_score = score
    result.checks.hunter_meta = meta

    // high = 灌入前剔除（hunter.js 两处闸门）；mid 只标注
    const lvl = result.checks.hunter_obfuscation.level
    if (lvl === 'high') result.blockers.push(`猎手混淆判级 high → 灌入前会被剔除（status=dead）`)
    else if (lvl === 'mid') result.warnings.push('猎手混淆判级 mid → 标徽章但不剔除，可接受')

    if (typeof score === 'number' && score < 4) {
      result.blockers.push(`猎手 scoreEntry=${score} < 4 → 判为非脚本脚本`)
    }
    if (meta && !meta.name) result.blockers.push('extractMeta 取不到 @name（banner 注释被混淆破坏）')
    if (meta && !meta.version) result.warnings.push('extractMeta 取不到 @version')
  } else {
    result.checks.hunter_obfuscation = { level: 'skipped', error: hunter.error }
    result.checks.hunter_score = null
    result.checks.hunter_meta = null
    result.warnings.push(hunter.error + '（猎手两项判定未实测）')
  }

  // ④ 服务端静态体检
  const guard = loadOptional(LX_GUARD, 'host sourceGuard.js')
  if (guard.mod && typeof guard.mod.scanScript === 'function') {
    const scan = guard.mod.scanScript(code)
    result.checks.server_scan = {
      level: scan.level,
      findings: scan.findings,
      hex_identifiers: scan.hexIdentifiers,
      max_line_length: scan.maxLineLength,
      size: scan.size,
    }
    if (scan.level === 'A') {
      const codes = (scan.findings || []).filter(f => f.level === 'A').map(f => f.code)
      result.blockers.push(`服务端判 A 级（${codes.join(',')}）→ 会被隔离`)
    } else if (scan.level === 'B') {
      result.warnings.push('服务端判 B 级 → 只标记不隔离，可接受')
    }
  } else {
    result.checks.server_scan = { level: 'skipped', error: guard.error }
    result.warnings.push(guard.error + '（服务端静态体检未实测）')
  }

  if (result.max_line_length > MAX_LINE_LENGTH_LIMIT) {
    result.warnings.push(`最长行 ${result.max_line_length} > ${MAX_LINE_LENGTH_LIMIT}（服务端记 B 级 long_line）`)
  }
  if (result.crlf > 0) result.blockers.push(`存在 ${result.crlf} 个 CRLF（产物必须 LF）`)

  result.verdict = result.blockers.length ? 'BLOCK' : 'PASS'
  return result
}

function main() {
  const args = parseArgs(process.argv.slice(2))
  if (!args.file) {
    console.error('用法: node tests/measure-artifact.cjs <产物路径> [--name 分发名] [--json]')
    process.exit(2)
  }
  if (!fs.existsSync(args.file)) {
    console.error('产物不存在: ' + args.file)
    process.exit(2)
  }
  const r = measure(args.file, args.name)
  if (args.json) {
    process.stdout.write(JSON.stringify(r, null, 2) + '\n')
  } else {
    const ob = r.checks.hunter_obfuscation || {}
    const meta = r.checks.hunter_meta || {}
    const scan = r.checks.server_scan || {}
    console.log(`文件      ${r.dist_name}`)
    console.log(`体积/行数 ${r.bytes}B / ${r.lines} 行 / 最长行 ${r.max_line_length} / CRLF ${r.crlf}`)
    console.log(`猎手混淆  ${ob.level}${ob.hits ? '  ' + JSON.stringify(ob.hits) : ''}`)
    console.log(`猎手评分  ${r.checks.hunter_score}`)
    console.log(`头部解析  name="${meta.name || ''}" version="${meta.version || ''}"`)
    console.log(`服务端    ${scan.level === null ? 'none' : scan.level}  ${JSON.stringify(scan.findings || [])}`)
    console.log(`hex标识符 ${scan.hex_identifiers}`)
    console.log(`判定      ${r.verdict}`)
    for (const b of r.blockers) console.log('  ✗ ' + b)
    for (const w of r.warnings) console.log('  ! ' + w)
  }
  process.exit(r.verdict === 'PASS' ? 0 : 3)
}

main()
