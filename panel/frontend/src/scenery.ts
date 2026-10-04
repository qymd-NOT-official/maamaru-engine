export const sceneryOptions = [
  { value: 'spring', label: '春日庭院', image: 'honmaru_garden_stage.png' },
  { value: 'autumn', label: '秋日庭院', image: 'honmaru_garden_autumn.png' },
] as const

let randomScene: typeof sceneryOptions[number] | undefined
export function applyScenery(value?: string) {
  const scene = value === 'random'
    ? (randomScene ??= sceneryOptions[Math.floor(Math.random() * sceneryOptions.length)])
    : sceneryOptions.find(option => option.value === value) ?? sceneryOptions[0]
  document.body.style.setProperty('--stage-scenery', `url('/static/img/${scene.image}')`)
}
