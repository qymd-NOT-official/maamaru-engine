export const sceneryOptions = [
  { value: 'spring', label: '春日庭院', image: 'honmaru_garden_stage.png' },
  { value: 'autumn', label: '秋日庭院', image: 'honmaru_garden_autumn.png' },
  { value: 'moonview', label: '秋日庭院 · 十五夜', image: 'honmaru_garden_moonview.png' },
  { value: 'winter', label: '冬日庭院', image: 'honmaru_garden_winter.png' },
  { value: 'after_rain', label: '梅雨庭院 · 雨过天晴', image: 'honmaru_garden_after_rain.png' },
  { value: 'seaside_day', label: '展望之间 · 海边', image: 'honmaru_seaside_day.png' },
  { value: 'seaside_sunset', label: '展望之间 · 海边夕阳', image: 'honmaru_seaside_sunset.png' },
  { value: 'wisteria', label: '立夏 · 藤', image: 'honmaru_wisteria.png' },
  { value: 'osaka_hall', label: '大阪城风 · 大广间', image: 'honmaru_osaka_hall.png' },
] as const

let randomScene: typeof sceneryOptions[number] | undefined
export function applyScenery(value?: string) {
  const scene = value === 'random'
    ? (randomScene ??= sceneryOptions[Math.floor(Math.random() * sceneryOptions.length)])
    : sceneryOptions.find(option => option.value === value) ?? sceneryOptions[0]
  document.body.style.setProperty('--stage-scenery', `url('/static/img/${scene.image}')`)
}
