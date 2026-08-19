import { describe, expect, it } from 'vitest'
import { deriveTrainingScope } from './tracePresentation'

describe('deriveTrainingScope', () => {
  it('extracts scope from single-value results via the SQL text', () => {
    const view = {
      sqlResult: {
        content: {
          event: 'query_completed',
          columns: ['complete_rate'],
          rows: [{ complete_rate: '0.6236' }],
          generated_sql: "SELECT ROUND(SUM(actual_qty)/SUM(plan_qty),4) AS complete_rate FROM fact_production_progress WHERE ship_no='H2601' AND process_code='YCL' AND period_date>='2025-05-01' AND period_date<'2025-06-01'",
        },
      },
    }
    expect(deriveTrainingScope(view)).toEqual({
      ship: 'H2601',
      processes: ['YCL'],
      cutoff: '2025-05 月末',
    })
  })

  it('extracts multi-process windows from wide rows and SQL IN lists', () => {
    const view = {
      sqlResult: {
        content: {
          event: 'query_completed',
          columns: ['process_code', 'month_label', 'complete_rate'],
          rows: [
            { process_code: 'AZTP', month_label: '2025-02', complete_rate: '0.9789' },
            { process_code: 'ZZTP', month_label: '2025-07', complete_rate: '0.9981' },
          ],
          executed_sql: "SELECT ... FROM f WHERE ship_no='H2601' AND period_date>='2025-02-01' AND period_date<'2025-08-01' GROUP BY process_code",
        },
      },
    }
    expect(deriveTrainingScope(view)).toEqual({
      ship: undefined,
      processes: ['AZTP', 'ZZTP'],
      cutoff: '2025-07 月末',
    })
  })

  it('returns empty scope when no query has run yet', () => {
    expect(deriveTrainingScope(undefined)).toEqual({ ship: undefined, processes: [], cutoff: undefined })
  })
})
