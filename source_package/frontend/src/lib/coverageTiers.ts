import type { TraceMessage, TraceView } from '../types/trace'

/**
 * 优化24：按会话真实进度推导各知识点的掌握档位。
 *
 * 诊断时点的学习计划全部是"待训练"状态（起点快照，永不更新），不能作为
 * 覆盖依据；此处扫描可见消息（讲义/实操任务）中该知识点出现过的最高难度：
 * 基础=1、应用=2、进阶=3，未训练=0。当前卡随回放光标逐点点亮。
 */
const TIER_BY_DIFFICULTY: Array<[string, number]> = [
  ['advanced', 3],
  ['applied', 2],
  ['basic', 1],
]

export function tierFromDifficulty(difficulty: unknown): number {
  const text = String(difficulty ?? '')
  for (const [needle, tier] of TIER_BY_DIFFICULTY) {
    if (text.includes(needle)) return tier
  }
  return 0
}

/** 注意 TraceMessage 的形态：payloadType/content 为顶层字段（无 payload 包装）。 */
export function computeCoverageTiers(
  view: Pick<TraceView, 'visibleMessages'> | undefined,
  points: string[],
): number[] {
  const best = new Map<string, number>()
  for (const message of view?.visibleMessages ?? []) {
    const record = message as TraceMessage
    if (
      record.payloadType !== 'lecture_note'
      && record.payloadType !== 'quiz_set'
      && record.payloadType !== 'practice_guide'
    ) continue
    const point = String(record.content?.knowledge_point ?? '')
    if (!point) continue
    const tier = tierFromDifficulty(record.content?.difficulty)
    if (tier > (best.get(point) ?? 0)) best.set(point, tier)
  }
  return points.map((point) => best.get(point) ?? 0)
}
