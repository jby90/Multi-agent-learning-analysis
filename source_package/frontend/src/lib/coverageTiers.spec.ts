import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

import { computeCoverageTiers } from './coverageTiers'
import { buildTraceView } from './traceModel'
import { parseTraceJsonl } from './traceParser'


function fullView(fileName: string) {
  const dir = path.resolve(process.cwd(), 'src', 'test', 'fixtures', 'traces')
  const document = parseTraceJsonl(
    readFileSync(path.join(dir, fileName), 'utf8'),
    fileName,
  )
  return buildTraceView(document, document.messages.length)
}

describe('coverageTiers（优化24：蛛网档位按真实进度推导）', () => {
  it('derives non-zero tiers from real session messages (payloadType top-level)', () => {
    const view = fullView('demo-craft_engineer-20260716133542.jsonl')
    // 该夹具会话实际训练了"跨工序归因方法"（应用档）
    const points = ['跨工序归因方法', '三道工序与传导关系', '偏差率与风险等级']
    const tiers = computeCoverageTiers(view, points)
    // 训练过的点达到进阶档(3)、未训练点为 0 —— 覆盖差异可见
    expect(tiers[0]).toBe(3)
    expect(tiers.slice(1)).toEqual([0, 0])
  })

  it('returns zeros for an empty view', () => {
    expect(computeCoverageTiers(undefined, ['任意点'])).toEqual([0])
  })
})
