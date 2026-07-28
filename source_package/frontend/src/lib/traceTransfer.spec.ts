import { describe, expect, it } from 'vitest'

import { demoTrace } from '../test/traceFixtures'
import { parseTraceJsonl } from './traceParser'
import { parseImportedTrace, serializeTraceJsonl } from './traceTransfer'


describe('trace transfer', () => {
  it('exports every original JSONL message losslessly enough for a replay round trip', () => {
    const source = demoTrace(
      'transfer-trace',
      'planner_new',
      '新入职生产计划员',
      '计划量与实际量必须分开理解。',
      '1156.87',
    )
    const document = parseTraceJsonl(source, 'source.jsonl')

    const exported = serializeTraceJsonl(document)
    const roundTrip = parseTraceJsonl(exported, 'roundtrip.jsonl')

    expect(exported.trim().split('\n')).toHaveLength(document.messages.length)
    expect(roundTrip.traceId).toBe(document.traceId)
    expect(roundTrip.messages.map((message) => message.raw))
      .toEqual(document.messages.map((message) => message.raw))
  })

  it('imports a valid JSONL file and rejects other file types before rendering', async () => {
    const source = demoTrace(
      'import-trace',
      'planner_new',
      '新入职生产计划员',
      '计划量与实际量必须分开理解。',
      '1156.87',
    )
    const document = await parseImportedTrace(new File([source], 'training.jsonl'))

    expect(document.fileName).toBe('training.jsonl')
    await expect(parseImportedTrace(new File([source], 'training.txt')))
      .rejects.toThrow('请选择 JSONL 会话记录')
  })
})
