<script setup lang="ts">
import { Eye, EyeOff } from '@lucide/vue'
import { ref } from 'vue'

withDefaults(defineProps<{
  modelValue: string
  autocomplete?: string
  placeholder?: string
}>(), {
  autocomplete: undefined,
  placeholder: undefined,
})

const emit = defineEmits<{
  'update:modelValue': [value: string]
}>()

const visible = ref(false)
</script>

<template>
  <span class="pwd-field">
    <input
      :type="visible ? 'text' : 'password'"
      :value="modelValue"
      :autocomplete="autocomplete"
      :placeholder="placeholder"
      @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)"
    >
    <button
      type="button"
      class="pwd-toggle"
      :aria-label="visible ? '隐藏密码' : '显示密码'"
      :aria-pressed="visible"
      @click="visible = !visible"
    >
      <EyeOff v-if="visible" :size="15" aria-hidden="true" />
      <Eye v-else :size="15" aria-hidden="true" />
    </button>
  </span>
</template>
