import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { TraceMessage } from '../types/trace'
import SqlResultTable from './SqlResultTable.vue'


function resultMessage(content: Record<string, unknown>): TraceMessage {
  return {
    msgId: 'interactive-001',
    traceId: 'interactive',
    step: 1,
    agent: 'verification',
    role: 'produce',
    payloadType: 'sql_result',
    content,
    evidence: [],
    claims: [],
    timestamp: '2026-07-16T02:00:00+00:00',
    rejectedByBus: false,
    busErrors: [],
  }
}

describe('SqlResultTable', () => {
  it('shows learner-facing column names while preserving the original row keys', () => {
    const wrapper = mount(SqlResultTable, {
      props: {
        message: resultMessage({
          question: '对照计划量与实际完成量',
          columns: ['plan_qty', 'actual_qty', 'complete_rate'],
          rows: [{ plan_qty: '1855.06', actual_qty: '1156.87', complete_rate: '62.36%' }],
        }),
      },
    })

    expect(wrapper.findAll('th').map((cell) => cell.text())).toEqual([
      '计划量', '实际完成量', '完成率',
    ])
    expect(wrapper.findAll('td').map((cell) => cell.text())).toEqual([
      '1855.06', '1156.87', '62.36%',
    ])
    expect(wrapper.text()).not.toContain('plan_qty')
    expect(wrapper.text()).not.toContain('actual_qty')
  })

  it('labels raw learner SQL honestly instead of calling it generated SQL', async () => {
    const wrapper = mount(SqlResultTable, {
      props: {
        message: resultMessage({
          columns: ['plan_qty'],
          rows: [{ plan_qty: '1855.06' }],
          generated_sql: 'SELECT SUM(plan_qty) AS plan_qty FROM fact_production_progress',
          sql_source: 'student',
        }),
      },
    })

    expect(wrapper.get('.sql-toggle').text()).toBe('查看我提交的查询')
    await wrapper.get('.sql-toggle').trigger('click')
    expect(wrapper.get('.sql-toggle').text()).toBe('收起我提交的查询')
    expect(wrapper.get('pre').text()).toBe(
      'SELECT SUM(plan_qty) AS plan_qty FROM fact_production_progress',
    )
  })

  it('fails closed when imported SQL contains internal implementation markers', async () => {
    const wrapper = mount(SqlResultTable, {
      props: {
        message: resultMessage({
          columns: ['plan_qty'],
          rows: [{ plan_qty: '1855.06' }],
          generated_sql: 'SELECT safe_rejected_flag, msg_id_copy, T22_FUTURE, _S42_FUTURE FROM trace',
        }),
      },
    })

    await wrapper.get('.sql-toggle').trigger('click')

    expect(wrapper.get('pre').text()).toBe('查询内容已隐藏')
    expect(wrapper.text()).not.toMatch(
      /safe_rejected|msg_id|T22_FUTURE|S42_FUTURE/iu,
    )
  })

  it('fails closed for malicious imported headers and scalar or object cells', () => {
    const columns = [
      'Q4',
      'safe_rejected',
      'no_matching_transition',
      'msg_id',
      'custom_internal_name',
      'object_payload',
    ]
    const wrapper = mount(SqlResultTable, {
      props: {
        message: resultMessage({
          event: 'query_completed',
          question: '查看核对结果',
          columns,
          rows: [{
            Q4: 'safe_rejected',
            safe_rejected: 'no_matching_transition',
            no_matching_transition: 'msg_id',
            msg_id: 'rule_hits',
            custom_internal_name: 'routing_final_family',
            object_payload: {
              verdict: 'reject',
              rule_hits: ['R-04'],
            },
          }],
        }),
      },
    })

    expect(wrapper.findAll('th').map((cell) => cell.text()))
      .toEqual(columns.map(() => '数据字段'))
    expect(wrapper.findAll('td').map((cell) => cell.text()))
      .toEqual(columns.map(() => '内容已隐藏'))
    expect(wrapper.text()).not.toMatch(
      /Q4|safe_rejected|no_matching_transition|msg_id|rule_hits|verdict|routing_|custom_internal_name|object_payload/iu,
    )
  })

  it('opens a focused result view and restores it with Escape', async () => {
    const wrapper = mount(SqlResultTable, {
      props: {
        message: resultMessage({
          question: '按船号比较计划兑现情况',
          columns: ['ship_no', 'complete_rate'],
          rows: [{ ship_no: 'H2601', complete_rate: '62.36%' }],
        }),
      },
    })

    await wrapper.get('.content-focus-toggle').trigger('click')
    expect(wrapper.get('.sql-result').classes()).toContain('is-focus-mode')
    expect(wrapper.get('.content-focus-toggle').attributes('aria-label'))
      .toBe('退出查询结果专注模式')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await wrapper.vm.$nextTick()

    expect(wrapper.get('.sql-result').classes()).not.toContain('is-focus-mode')
    wrapper.unmount()
  })

  it('keeps approved production and second-domain result columns in business language', () => {
    const columns = [
      'month_label',
      'workshop_code',
      'high_risk_rows',
      'DEPT',
      'PROCESS',
      'YEARNUM',
      'MONTHNUM',
      'ACTUALNUM',
      'FINISHRATE',
      'ONTIMERATE',
      'SERWARNNUM',
      'WARNNUM',
      'FINISHNUM',
      'ONTIMENUM',
      'PLANNUM',
      'ndjhs',
      'JHYLJ',
      'NDSJS',
      'TRENDMONTH',
      'TRENDYEAR',
      'QG_NUM',
      'XZL_NUM',
    ]
    const wrapper = mount(SqlResultTable, {
      props: {
        message: resultMessage({
          event: 'query_completed',
          columns,
          rows: [Object.fromEntries(columns.map((column) => [column, '1']))],
        }),
      },
    })

    expect(wrapper.findAll('th').map((cell) => cell.text())).toEqual([
      '月份',
      '责任单元',
      '高风险记录数',
      '部门',
      '工序',
      '年总量',
      '月计划数',
      '实际数',
      '完成率',
      '准时率',
      '严重预警数',
      '预警数',
      '完成数',
      '准时数',
      '计划数',
      '年度计划数',
      '年计划累计数',
      '年度实际数',
      '月份',
      '年份',
      '切割实际值',
      '小组立实际值',
    ])
    expect(wrapper.findAll('td').map((cell) => cell.text()))
      .toEqual(columns.map(() => '1'))
  })

  it.each([
    {
      event: 'refuse_out_of_scope',
      title: '问题不在本次训练范围',
      message: '这个问题不在本次训练的数据范围内，请换一个与岗位任务相关的问题。',
    },
    {
      event: 'sandbox_rejected',
      title: '只能查授权的字段 · 查询被拦下',
      message: '本题的查询未通过数据安全检查，请调整后重试。',
    },
    {
      event: 'template_authority_rejected',
      title: '查询内容需要调整',
      message: '本题的查询未通过数据安全检查，请调整后重试。',
    },
    {
      event: 'query_empty',
      title: '本次查询没有返回数据',
      message: '本次查询没有返回数据，请调整查询条件后重试。',
    },
    {
      event: 'query_timeout',
      title: '查询服务暂时不可用',
      message: '服务暂时不可用，请稍后再试。',
    },
    {
      event: 'query_failed',
      title: '查询服务暂时不可用',
      message: '服务暂时不可用，请稍后再试。',
    },
  ])('renders $event replay failures with shared teaching copy', ({
    event,
    title,
    message,
  }) => {
    const wrapper = mount(SqlResultTable, {
      props: {
        message: resultMessage({
          event,
          question: '运行岗位查询',
          row_count: 0,
          columns: [],
          rows: [],
          rule_id: 'S-04',
          student_message: 'no_matching_transition at S4_VERIFY; msg_id=secret',
        }),
      },
    })

    expect(wrapper.text()).toContain(title)
    expect(wrapper.text()).toContain(message)
    expect(wrapper.text()).not.toContain('本次查询没有返回可展示的列')
    expect(wrapper.text()).not.toContain('0 行')
    expect(wrapper.find('table').exists()).toBe(false)
    expect(wrapper.text()).not.toMatch(/no_matching_transition|S4_VERIFY|msg_id/iu)
  })
})
