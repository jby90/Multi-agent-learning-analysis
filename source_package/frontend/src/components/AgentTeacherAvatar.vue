<script setup lang="ts">
import { computed } from 'vue'

import diagnosisCollaborating from '../assets/agent-teachers/diagnosis-collaborating-v1.png'
import diagnosisDone from '../assets/agent-teachers/diagnosis-done-v1.png'
import diagnosisIdle from '../assets/agent-teachers/diagnosis-idle-v1.png'
import diagnosisWorking from '../assets/agent-teachers/diagnosis-working-v1.png'
import knowledgeCollaborating from '../assets/agent-teachers/knowledge-collaborating-v1.png'
import knowledgeDone from '../assets/agent-teachers/knowledge-done-v1.png'
import knowledgeIdle from '../assets/agent-teachers/knowledge-idle-v1.png'
import knowledgeWorking from '../assets/agent-teachers/knowledge-working-v1.png'
import reviewCollaborating from '../assets/agent-teachers/review-collaborating-v1.png'
import reviewDone from '../assets/agent-teachers/review-done-v1.png'
import reviewIdle from '../assets/agent-teachers/review-idle-v1.png'
import reviewWorking from '../assets/agent-teachers/review-working-v1.png'
import taskCollaborating from '../assets/agent-teachers/task-collaborating-v1.png'
import taskDone from '../assets/agent-teachers/task-done-v1.png'
import taskIdle from '../assets/agent-teachers/task-idle-v1.png'
import taskWorking from '../assets/agent-teachers/task-working-v1.png'
import verificationCollaborating from '../assets/agent-teachers/verification-collaborating-v1.png'
import verificationDone from '../assets/agent-teachers/verification-done-v1.png'
import verificationIdle from '../assets/agent-teachers/verification-idle-v1.png'
import verificationWorking from '../assets/agent-teachers/verification-working-v1.png'
import type { AgentActivityId, AgentActivityStatus } from '../lib/interactiveApi'


type TeacherState = 'idle' | 'working' | 'collaborating' | 'done'

const props = withDefaults(defineProps<{
  agent: AgentActivityId
  size?: number
  status: AgentActivityStatus
}>(), {
  size: 58,
})

const portraits: Record<AgentActivityId, Record<TeacherState, string>> = {
  diagnosis: {
    idle: diagnosisIdle,
    working: diagnosisWorking,
    collaborating: diagnosisCollaborating,
    done: diagnosisDone,
  },
  knowledge: {
    idle: knowledgeIdle,
    working: knowledgeWorking,
    collaborating: knowledgeCollaborating,
    done: knowledgeDone,
  },
  review: {
    idle: reviewIdle,
    working: reviewWorking,
    collaborating: reviewCollaborating,
    done: reviewDone,
  },
  verification: {
    idle: verificationIdle,
    working: verificationWorking,
    collaborating: verificationCollaborating,
    done: verificationDone,
  },
  task: {
    idle: taskIdle,
    working: taskWorking,
    collaborating: taskCollaborating,
    done: taskDone,
  },
  evidence_review: {
    idle: verificationIdle,
    working: verificationWorking,
    collaborating: verificationCollaborating,
    done: verificationDone,
  },
  pedagogy_review: {
    idle: knowledgeIdle,
    working: knowledgeWorking,
    collaborating: knowledgeCollaborating,
    done: knowledgeDone,
  },
}

const teacherState = computed<TeacherState>(() => {
  if (['approved', 'done'].includes(props.status)) return 'done'
  if (['collaborating', 'reviewing', 'debating'].includes(props.status)) return 'collaborating'
  if (props.status === 'working') return 'working'
  return 'idle'
})

const portrait = computed(() => portraits[props.agent][teacherState.value])
</script>

<template>
  <img
    class="agent-teacher-avatar"
    :class="[`is-${teacherState}`, { 'is-blocked': status === 'blocked' }]"
    :data-teacher-agent="agent"
    :data-teacher-state="teacherState"
    :src="portrait"
    :width="size"
    :height="size"
    alt=""
    aria-hidden="true"
    draggable="false"
  />
</template>

<style scoped>
.agent-teacher-avatar {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
  filter: drop-shadow(0 4px 5px rgba(0,0,0,.38));
  user-select: none;
}
.agent-teacher-avatar.is-working { filter: drop-shadow(0 0 7px rgba(74,220,255,.62)); }
.agent-teacher-avatar.is-collaborating { filter: drop-shadow(0 0 8px rgba(187,147,255,.58)); }
.agent-teacher-avatar.is-done { filter: drop-shadow(0 0 6px rgba(83,224,158,.42)); }
.agent-teacher-avatar.is-blocked { filter: grayscale(.8) drop-shadow(0 0 6px rgba(255,94,94,.5)); }
</style>
