import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  createInteractiveApi,
  InteractiveApiError,
  type InteractiveState,
} from './interactiveApi'


const state: InteractiveState = {
  session_id: 'session-1',
  trace_id: 'interactive-session-1',
  trace_path: 'traces/interactive-session-1.jsonl',
  state: 'S1_DIAGNOSIS',
  awaiting: 'pretest',
  mode: 'live',
  profile: {
    profile_id: 'planner_new',
    title: '新入职生产计划员',
  },
  messages: [],
  artifact: null,
  interaction: null,
}


describe('interactiveApi', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('creates and polls a session through the approved local HTTP contract', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(state), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    const api = createInteractiveApi('http://127.0.0.1:8765')

    const created = await api.createSession('planner_new')
    const polled = await api.getState('session-1')

    expect(created).toEqual(state)
    expect(polled).toEqual(state)
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'http://127.0.0.1:8765/api/sessions',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ profile_id: 'planner_new' }),
      }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      'http://127.0.0.1:8765/api/sessions/session-1',
      expect.objectContaining({ method: 'GET' }),
    )
  })

  it('loads and submits the approved pretest without exposing an answer key', async () => {
    const questions = [{
      question_id: 'PT-1',
      knowledge_point: '计划量与实际量口径',
      stem: '实际完成量应看哪个数？',
      options: { A: '1855.06', B: '1156.87', C: '两者之和', D: '两者平均' },
    }]
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ questions }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(state), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    const api = createInteractiveApi('http://127.0.0.1:8765/')

    const loaded = await api.getPretest('session-1')
    await api.submitPretest('session-1', { 'PT-1': 'B' })

    expect(loaded).toEqual(questions)
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      'http://127.0.0.1:8765/api/sessions/session-1/pretest',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ answers: { 'PT-1': 'B' } }),
      }),
    )
  })

  it('maps advance, curriculum continuation, and raw SQL to separate learner actions', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(state), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    const api = createInteractiveApi('http://127.0.0.1:8765')

    await api.advance('session-1')
    await api.continueLearning('session-1')
    await api.submitSql('session-1', 'SELECT plan_qty FROM fact_production_progress')

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'http://127.0.0.1:8765/api/sessions/session-1/advance',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({}) }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      'http://127.0.0.1:8765/api/sessions/session-1/continue',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({}) }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      'http://127.0.0.1:8765/api/sessions/session-1/sql',
      expect.objectContaining({
        body: JSON.stringify({ sql: 'SELECT plan_qty FROM fact_production_progress' }),
      }),
    )
  })

  it('submits a free-text follow-up with a stable client turn id', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(state), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    const api = createInteractiveApi('http://127.0.0.1:8765')

    await api.submitFollowUp(
      'session-1',
      '应以实际完成量说明真实进度。',
      'turn-client-1',
    )

    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:8765/api/sessions/session-1/follow-up',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          text: '应以实际完成量说明真实进度。',
          client_turn_id: 'turn-client-1',
        }),
      }),
    )
  })

  it.each([
    ['safe_rejected', '本题的查询未通过数据安全检查，请调整后重试。'],
    ['external_unavailable', '服务暂时不可用，请稍后再试。'],
    ['system_error', '当前步骤暂时无法继续，请稍后再试。'],
  ])('turns %s API failures into fixed public copy', async (outcome, expected) => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      error: 'no_matching_transition at S4_VERIFY; msg_id=secret',
      outcome,
    }), { status: 409 }))
    vi.stubGlobal('fetch', fetchMock)
    const api = createInteractiveApi('http://127.0.0.1:8765')

    let caught: unknown
    try {
      await api.submitSql('session-1', 'SELECT plan_qty FROM fact_production_progress')
    } catch (error) {
      caught = error
    }

    expect(caught).toBeInstanceOf(Error)
    expect(caught).toBeInstanceOf(InteractiveApiError)
    expect((caught as InteractiveApiError).outcome).toBe(outcome)
    expect((caught as Error).message).toBe(expected)
    expect(String(caught)).not.toMatch(
      /no_matching_transition|S4_VERIFY|msg_id|safe_rejected|external_unavailable|system_error/u,
    )
  })

  it('never republishes an unclassified backend error', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      error: 'orchestrator.interactive_session failed at no_matching_transition',
    }), { status: 500 }))
    vi.stubGlobal('fetch', fetchMock)
    const api = createInteractiveApi('http://127.0.0.1:8765')

    await expect(api.advance('session-1')).rejects.toThrow(
      '实操通道暂时不可用，请稍后再试。',
    )
  })

  it('turns a network exception into fixed service copy', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new TypeError('Failed to fetch http://127.0.0.1:8765/internal')
    }))
    const api = createInteractiveApi('http://127.0.0.1:8765')

    await expect(api.getState('session-1')).rejects.toThrow(
      '服务暂时不可用，请稍后再试。',
    )
  })

  it('preserves the HTTP status needed to distinguish an expired session', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      error: '会话不存在或已失效。',
    }), {
      status: 404,
      headers: { 'Content-Type': 'application/json' },
    })))
    const api = createInteractiveApi('http://127.0.0.1:8765')

    let caught: unknown
    try {
      await api.getState('missing-session')
    } catch (error) {
      caught = error
    }

    expect(caught).toBeInstanceOf(InteractiveApiError)
    expect((caught as InteractiveApiError).status).toBe(404)
  })

  it('turns an unreadable error response into fixed channel copy', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      'Traceback: no_matching_transition at S4_VERIFY',
      { status: 500, headers: { 'Content-Type': 'text/plain' } },
    )))
    const api = createInteractiveApi('http://127.0.0.1:8765')

    await expect(api.advance('session-1')).rejects.toThrow(
      '实操通道暂时不可用，请稍后再试。',
    )
  })
})
