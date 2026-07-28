import { onScopeDispose, ref, type Ref } from 'vue'

import type { AgentActivityEvent } from '../lib/interactiveApi'


const REVIEW_GATE_HOLD_MS = 650
const PARALLEL_RUNNING_HOLD_MS = 1800
const PARALLEL_JOIN_HOLD_MS = 1100
const DEBATE_HOLD_MS = 900
const REGENERATION_ROUTE_HOLD_MS = 1200
const RESOURCE_BRANCH_HOLD_MS = 700
const RESOURCE_JOIN_HOLD_MS = 850

function detailString(event: AgentActivityEvent, key: string): string | undefined {
  const value = event.details?.[key]
  return typeof value === 'string' ? value : undefined
}

export function agentEventHoldMs(event: AgentActivityEvent): number {
  if (
    event.activity === 'parallel_resource_generation'
    || event.activity === 'parallel_evidence_retrieval'
  ) {
    if (detailString(event, 'aggregation') === 'deterministic') {
      return RESOURCE_JOIN_HOLD_MS
    }
    if (event.status === 'working' || event.status === 'collaborating') {
      return RESOURCE_BRANCH_HOLD_MS
    }
    return 0
  }
  if (event.activity === 'parallel_quality_review') {
    return detailString(event, 'aggregation') === 'pending'
      ? PARALLEL_RUNNING_HOLD_MS
      : PARALLEL_JOIN_HOLD_MS
  }
  if (
    event.agent === 'review'
    && event.activity === 'quality_gate'
    && event.status === 'reviewing'
  ) return REVIEW_GATE_HOLD_MS
  if (event.activity === 'deterministic_rejection_route') {
    return REGENERATION_ROUTE_HOLD_MS
  }
  if (event.status === 'debating') return DEBATE_HOLD_MS
  return 0
}

export interface AgentEventPlayback {
  events: Ref<AgentActivityEvent[]>
  receive: (event: AgentActivityEvent) => void
  reset: () => void
}

/**
 * Preserve short-lived authoritative Agent states long enough to be perceived.
 * The queue changes presentation timing only; event order and backend metrics
 * remain untouched.
 */
export function useAgentEventPlayback(maxEvents = 160): AgentEventPlayback {
  const events = ref<AgentActivityEvent[]>([])
  const pending: AgentActivityEvent[] = []
  const known = new Set<string>()
  let timer: number | undefined

  function eventKey(event: AgentActivityEvent): string {
    return `${event.trace_id}:${event.sequence}`
  }

  function append(event: AgentActivityEvent): void {
    events.value = [...events.value, event].slice(-maxEvents)
  }

  function drain(): void {
    timer = undefined
    while (pending.length) {
      const next = pending.shift()
      if (!next) return
      append(next)
      const holdMs = agentEventHoldMs(next)
      if (holdMs > 0) {
        timer = window.setTimeout(drain, holdMs)
        return
      }
    }
  }

  function receive(event: AgentActivityEvent): void {
    const key = eventKey(event)
    if (known.has(key)) return
    known.add(key)
    pending.push(event)
    if (timer === undefined) drain()
  }

  function reset(): void {
    if (timer !== undefined) window.clearTimeout(timer)
    timer = undefined
    pending.splice(0, pending.length)
    known.clear()
    events.value = []
  }

  onScopeDispose(() => {
    if (timer !== undefined) window.clearTimeout(timer)
  })

  return { events, receive, reset }
}
