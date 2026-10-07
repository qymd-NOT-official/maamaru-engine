// 面板所属服务器：启动器两个入口（国服/日服）经 URL ?server=jp 决定，
// 启动时读一次，运行期不变；切服回启动器换入口，面板内不提供切换。
export const appServer: 'cn' | 'jp' =
  new URLSearchParams(window.location.search).get('server') === 'jp' ? 'jp' : 'cn'
