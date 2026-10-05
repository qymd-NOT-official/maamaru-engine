import { ref } from 'vue'

export const companion = ref('kogitsune')
export const companionOptions = [
  { value: 'kogitsune', label: '小狐丸' },
  { value: 'hasebe', label: '压切长谷部' },
] as const
export function applyCompanion(value?: string) {
  companion.value = value === 'hasebe' ? 'hasebe' : 'kogitsune'
}
