import { shallowMount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { TraceView } from '../types/trace'
import CollaborationWorkspace from './CollaborationWorkspace.vue'


const view: TraceView = {
  visibleMessages: [],
  currentState: 'S2_KNOWLEDGE',
  knowledgeDimensions: [],
  debateGroups: [],
}

describe('CollaborationWorkspace', () => {
  it('places the runtime topology beside collaboration, evidence and records', async () => {
    const wrapper = shallowMount(CollaborationWorkspace, { props: { view } })
    const tabs = wrapper.findAll('[role="tab"]')

    expect(tabs.map((tab) => tab.text())).toEqual(expect.arrayContaining([
      expect.stringContaining('实时协同'),
      expect.stringContaining('运行拓扑'),
      expect.stringContaining('效能证据'),
      expect.stringContaining('协作记录'),
    ]))

    const topologyTab = tabs.find((tab) => tab.text().includes('运行拓扑'))
    expect(topologyTab).toBeDefined()
    await topologyTab?.trigger('click')
    expect(topologyTab?.attributes('aria-selected')).toBe('true')
    expect(wrapper.get('agent-topology-stub').attributes('style')).not.toContain('display: none')
  })

  it('links live collaboration to the shared learner operation workspace', () => {
    const wrapper = shallowMount(CollaborationWorkspace, {
      props: { view, learnerWorkspaceTarget: '#learner-workspace' },
    })

    const jump = wrapper.get('.collaboration-learner-jump')
    expect(jump.attributes('href')).toBe('#learner-workspace')
    expect(jump.text()).toContain('操作会实时驱动拓扑')
  })
})
