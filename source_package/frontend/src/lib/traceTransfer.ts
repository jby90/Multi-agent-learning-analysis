import type { TraceDocument } from '../types/trace'
import { parseTraceJsonl, TraceParseError } from './traceParser'


export function serializeTraceJsonl(document: TraceDocument): string {
  const lines = document.messages.map((message) => {
    if (!message.raw) throw new TraceParseError('当前会话缺少可导出的原始记录')
    return JSON.stringify(message.raw)
  })
  return `${lines.join('\n')}\n`
}


export async function parseImportedTrace(file: File): Promise<TraceDocument> {
  if (!file.name.toLowerCase().endsWith('.jsonl')) {
    throw new TraceParseError('请选择 JSONL 会话记录')
  }
  return parseTraceJsonl(await file.text(), file.name)
}
