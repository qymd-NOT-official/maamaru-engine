"""Friendly WebView launcher for non-technical users."""

import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

import webview

from touken.diagnostics import create_diagnostic_bundle
from touken.emulator_discovery import auto_configure_emulator, configure_mumu_from_folder
from touken.data_relocation import (
    cleanup_previous_data,
    pending_relocation_cleanup,
    relocate_user_data,
    suggested_data_root,
)
from touken.runtime_paths import DATA_ROOT, LOG_DIR, UPDATES_DIR, ensure_runtime_data
from .health import has_blocker, run_checks
from .update_apply import consume_result, prepare_apply
from .updater import UpdateError, download_installer, select_update
from .version import CURRENT_VERSION


HTML = r"""
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--ink:#332b23;--muted:#82766a;--paper:#f3ecdf;--card:#fffaf1;--line:#ddd0bb;--gold:#d99d08;--gold-deep:#a96f00;--gold-pale:#fff0bd;--green:#3f8f69;--red:#b74b44;--orange:#c77a1e}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:"Microsoft YaHei UI","Segoe UI",sans-serif;user-select:none;overflow-x:hidden}button{font:inherit;cursor:pointer}
header{height:84px;padding:11px max(28px,calc((100% - 1120px)/2));background:linear-gradient(115deg,#29231f 0,#44352a 64%,#5f4020 100%);color:white;border-bottom:4px solid var(--gold);box-shadow:0 3px 12px #4b321c33}
.brand{display:flex;height:60px;align-items:center;gap:14px}.brand img{width:56px;height:56px;object-fit:contain;image-rendering:auto}.brand-copy{min-width:0}.brand h1{margin:0;font-size:23px;letter-spacing:.04em}.brand p{margin:2px 0 0;color:#eadfcf;font-size:12px}.version{margin-left:auto;padding:6px 10px;color:#f7df9c;background:#ffffff12;border:1px solid #ffffff28;border-radius:999px;font-size:12px}
main{width:min(1120px,calc(100% - 44px));margin:0 auto;padding:14px 0 12px}
.board{display:grid;grid-template-columns:320px minmax(0,1fr);gap:18px}
.garden{position:relative;border:1px solid #dac9ab;border-left:5px solid var(--gold);border-radius:15px;overflow:hidden;background:#39434f;box-shadow:0 7px 20px #705a3314;min-height:0}
.garden-scene{position:absolute;inset:0;display:block;width:100%;height:100%;object-fit:cover;object-position:40% center;image-rendering:pixelated}
.garden-fox{position:absolute;bottom:12px;left:calc(50% - 48px);width:96px;height:96px;object-fit:contain;image-rendering:pixelated;filter:drop-shadow(0 4px 2px #302b2930)}
.panel{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.checks{padding:14px 16px;border-right:1px dashed #e2d7c5;min-width:0}
.checks h3,.tools h3{margin:0 0 9px;font-size:16px}
.check-list{display:grid;gap:7px}
.check-row{display:grid;grid-template-columns:20px minmax(0,1fr);gap:8px;align-items:center;padding:6px 9px;border-radius:8px;background:#f7f2e8}
.check-row .mark{display:grid;width:18px;height:18px;place-items:center;color:white;background:var(--green);border-radius:50%;font-size:11px;font-weight:800}
.check-row.warn{background:#fff8e7;border:1px solid #ecd4a8}.check-row.warn .mark{background:var(--orange)}
.check-row.error{background:#fff0ee;border:1px solid #e5b6b2}.check-row.error .mark{background:var(--red)}
.check-row b{font-size:13px}.check-row small{margin-left:7px;color:var(--muted);font-size:11px}.check-row div{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.checks-count{display:block;margin-top:9px;color:var(--muted);font-size:11px}
.loading{padding:30px 0;color:var(--muted);text-align:center;font-size:13px}
.tools{padding:14px 16px;display:flex;flex-direction:column;gap:7px;background:#fdf6e7}
/* The tools column adds a 7px flex gap below its heading: 2 + 7 matches the checks' 9px. */
.tools h3{margin-bottom:2px}
.check-row,.tools button{min-height:36px}
.tools button{display:flex;align-items:center;gap:10px;padding:4px 10px;text-align:left;color:#554b40;background:var(--card);border:1px solid #d3c5af;border-radius:8px;font-size:13px}
.tools button:hover{background:#fff;border-color:#b99e72}.tools button:disabled{cursor:not-allowed;opacity:.55}
.tool-symbol{display:grid;place-items:center;width:26px;height:26px;flex:0 0 26px;border-radius:6px;color:var(--gold-deep);background:#f3ead6}
.tools button:hover .tool-symbol{background:#f5e5bc}.tools button:focus-visible{outline:2px solid var(--gold-deep);outline-offset:2px}
.launcher-icon{display:block;width:18px;height:18px;flex-shrink:0;fill:none;stroke:currentColor;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
.check-row .mark .launcher-icon{width:12px;height:12px;stroke-width:2}.status .mark .launcher-icon{width:17px;height:17px;stroke-width:2}
.check-row.info .mark{color:#856523;background:#eee2c6}
.runbar{margin-top:12px;padding:10px 18px 14px;background:var(--card);border:1px solid var(--line);border-radius:14px}
.run-track{display:none;position:relative;height:40px;border-bottom:2px dashed var(--gold);margin-bottom:8px}
.run-fox{position:absolute;bottom:0;display:none;width:48px;height:48px;background:url("__FOX1_URI__") no-repeat center/contain;image-rendering:pixelated;animation:fox-run 18s linear infinite,fox-frames 1.2s steps(1,end) infinite,fox-bob .6s ease-in-out infinite}
.runbar.busy .run-track,.runbar.busy .run-fox{display:block}
@keyframes fox-frames{0%,49.9%{background-image:url("__FOX1_URI__")}50%,100%{background-image:url("__FOX2_URI__")}}
@keyframes fox-run{0%{left:0;transform:scaleX(1)}48%{left:calc(100% - 48px);transform:scaleX(1)}50%{left:calc(100% - 48px);transform:scaleX(-1)}98%{left:0;transform:scaleX(-1)}100%{left:0;transform:scaleX(1)}}
@keyframes fox-bob{0%,100%{margin-bottom:0}50%{margin-bottom:2px}}
@media (prefers-reduced-motion:reduce){.run-fox{animation:none}}
.status{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.status .mark{display:grid;width:26px;height:26px;flex:0 0 26px;place-items:center;color:white;background:var(--gold);border-radius:50%;font-size:14px;font-weight:800}
.status.blocked .mark{background:var(--red)}.status.ready .mark{background:var(--green)}
.status b{font-size:14px}.status span{color:var(--muted);font-size:12px}
.progress-row{display:grid;grid-template-columns:minmax(0,1fr) 190px;gap:24px;align-items:end}
.launch-details{min-width:0}.status{flex-wrap:wrap}.status #stateCopy{flex-basis:100%;margin-left:36px}
.bar{height:12px;border:1px solid #ddcfb9;border-radius:999px;background:#eee2cc;overflow:hidden}
.bar-fill{height:100%;width:0;background:linear-gradient(#e6aa0b,#ce8c00);transition:width .4s ease}
.launch-progress{display:none;margin-top:8px;grid-template-columns:repeat(3,1fr);gap:5px}.launch-progress.show{display:grid}.launch-progress span{padding-top:6px;color:#a49483;border-top:3px solid #ddcfb9;font-size:11px;text-align:center}.launch-progress span.active{color:var(--gold-deep);border-color:var(--gold);font-weight:700}.launch-progress span.done{color:var(--green);border-color:var(--green)}
.start{width:100%;height:44px;border:0;border-radius:11px;color:white;background:linear-gradient(#e6aa0b,#ce8c00);box-shadow:0 4px 0 #855900,0 8px 14px #a56e1630;font-size:16px;font-weight:800}.start:hover{filter:brightness(1.04)}.start:active{transform:translateY(2px);box-shadow:0 2px 0 #855900}.start:disabled{cursor:not-allowed;filter:grayscale(.65);opacity:.66}
.start-actions{display:flex;justify-content:flex-end;padding-bottom:4px}.start.ledger{color:#65532d;background:linear-gradient(#f8f1df,#ead9b3);box-shadow:0 4px 0 #b59b67,0 8px 14px #7d672430}
.note{margin:auto 2px 0;padding-top:8px;color:#918579;font-size:11px}
@media(max-width:940px){header{padding-inline:24px}main{width:calc(100% - 30px)}.board{grid-template-columns:1fr}.garden{height:150px}.garden-scene{object-position:center bottom}.panel{grid-template-columns:1fr}.checks{border-right:0;border-bottom:1px dashed #e2d7c5}.progress-row{grid-template-columns:1fr}.start-actions .start{width:190px}}
</style></head><body>
<header><div class="brand"><img src="__ICON_URI__" alt=""><div class="brand-copy"><h1>まあ丸</h1><p>本丸近侍启动器 · 狐之助已经替你看过一遍</p></div><span class="version">v__VERSION__</span></div></header>
<main>
<div class="board">
<div class="garden"><img class="garden-scene" src="__GARDEN_URI__" alt="雨后放晴的像素庭院"><img class="garden-fox" src="__GARDEN_FOX_URI__" alt="在缘侧等候的狐之助"></div>
<section class="panel">
<div class="checks"><h3>检查</h3><div id="checks"><div class="loading">狐之助正在巡查……</div></div><span id="checksCount" class="checks-count"></span></div>
<div class="tools"><h3>功能</h3><button data-icon="refresh" onclick="refresh()">重新检查</button><button data-icon="repair" onclick="repair()">修复环境</button><button data-icon="folder" onclick="openData()">数据目录</button><button data-icon="move" id="migrateDataButton" onclick="migrateData(this)">迁移数据</button><button data-icon="sweep" id="cleanupDataButton" style="display:none" onclick="cleanupOldData(this)">清理旧副本</button><button data-icon="monitor" onclick="chooseEmulator(this)">选择模拟器</button><button data-icon="report" id="feedbackButton" onclick="exportFeedback(this)">反馈错误</button><button data-icon="external" id="issueButton" style="display:none" onclick="openIssue()">去 Issue</button><button data-icon="update" onclick="update()">检查更新</button><p class="note">QQ 协议端是可选功能，请在面板“系统 → QQ”中配置。</p></div>
</section>
</div>
<div id="runbar" class="runbar">
<div class="run-track"><span class="run-fox"></span></div>
<div class="progress-row"><div class="launch-details"><div id="status" class="status"><span id="stateMark" class="mark">…</span><b id="stateTitle">正在整理启动环境</b><span id="stateCopy">稍等一下，狐之助正在确认程序、面板与模拟器。</span></div>
<div class="bar"><div id="barFill" class="bar-fill"></div></div><div id="launchProgress" class="launch-progress"><span>整理环境</span><span>启动面板</span><span>打开本丸</span></div></div><div class="start-actions"><button id="startJp" class="start ledger" onclick="startApp('jp')" disabled>日服本丸</button><button id="start" class="start" onclick="startApp('automation')" disabled>正在检查…</button></div></div>
</div>
</main>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// Small, code-native icons share the panel's rounded 16px line style.
const iconPaths={
 refresh:'M13.5 6A5.5 5.5 0 1 0 13 11M13.5 2v4H9.5',
 repair:'M9.5 2a4 4 0 0 0-4.7 4.7L1.5 10a2.1 2.1 0 0 0 3 3l3.3-3.3A4 4 0 0 0 12.5 5L10 7 8 5z',
 folder:'M1.5 12.5v-9h4l2 2h7v7a1 1 0 0 1-1 1h-11a1 1 0 0 1-1-1ZM1.5 6.5h13',
 move:'M6 13.5H2.5a1 1 0 0 1-1-1v-9h4l2 2h6v2M7 11h7m-3-3 3 3-3 3',
 sweep:'m10.5 1.5-4 6M4 7l5 3-3 4H1.5L2 10zM4 10l-1 3.5M10 13.5h4M12 10.5h2',
 monitor:'M1.5 2.5h13v9h-13zM8 11.5v3M5 14.5h6M4 5.5h3M4 8h5',
 report:'M9.5 1.5h-7v13h11v-9zM9.5 1.5v4h4M8 7.5v3M8 12.5h.01',
 external:'M9 2h5v5M14 2 7 9M6 3H2v11h11v-4',
 update:'M8 11V2m-3 3 3-3 3 3M2 10v4h12v-4',
 check:'m3 8 3.5 3.5L13 4.5',
 close:'m4 4 8 8M12 4l-8 8',
 warn:'M8 3v6M8 12h.01',
 info:'M8 7v5M8 4h.01',
 wait:'M3 8h.01M8 8h.01M13 8h.01',
};
function icon(name){return `<svg class="launcher-icon" viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="${iconPaths[name]||iconPaths.info}"/></svg>`}
function setToolLabel(button,label){button.innerHTML=`<span class="tool-symbol" aria-hidden="true">${icon(button.dataset.icon)}</span><span>${esc(label)}</span>`}
document.querySelectorAll('.tools button[data-icon]').forEach(button=>setToolLabel(button,button.textContent));
const stateIcon={error:'close',warn:'warn',info:'info',ok:'check'};
const stateMarkIcon={'×':'close','!':'warn','i':'info','✓':'check','…':'wait'};
document.querySelector('#stateMark').innerHTML=icon('wait');
const issueUrl='https://github.com/qymd-NOT-official/maamaru-engine/issues/new';let feedbackFailures=0;let feedbackResetTimer=0;const feedbackLines={1:'导出失败？问问上天',2:'还失败？去issue骂作者',4:'干嘛不去？',5:'你是不是想骂连错误处理系统都做不好？',6:'噫吁嚱，惶恐滩头说惶恐，零丁洋里叹零丁。',7:'面包店里卖面包，蛋糕店里卖蛋糕。',8:'你还点',9:'？',10:'我没有日志，你也不去issue，你到底想让我怎样'};
function setState(kind,title,copy,mark){const status=document.querySelector('#status');status.className='status '+kind;document.querySelector('#stateTitle').textContent=title;document.querySelector('#stateCopy').textContent=copy;document.querySelector('#stateMark').innerHTML=icon(stateMarkIcon[mark]||'info');document.querySelector('#runbar').classList.toggle('busy',kind==='')}
function setProgress(ratio){document.querySelector('#barFill').style.width=(Math.max(0,Math.min(1,ratio))*100).toFixed(1)+'%'}
function renderChecks(items){
 const issues=items.filter(x=>x.state==='error'||x.state==='warn');
 document.querySelector('#checks').innerHTML=`<div class="check-list">${items.map(x=>`<div class="check-row ${x.state}"><span class="mark">${icon(stateIcon[x.state]||'info')}</span><div title="${esc(x.label)}：${esc(x.detail)}"><b>${esc(x.label)}</b><small>${esc(x.detail)}</small></div></div>`).join('')}</div>`;
 document.querySelector('#checksCount').textContent=`${items.length} 项 · ${issues.length?issues.length+' 项提醒':'全部通过'}`;
 return issues;
}
async function refresh(){
 setState('','正在整理启动环境','稍等一下，狐之助正在确认程序、面板与模拟器。','…');setProgress(0);const start=document.querySelector('#start');start.disabled=true;start.textContent='正在检查…';const startJp=document.querySelector('#startJp');if(startJp)startJp.disabled=true;document.querySelector('#checks').innerHTML='<div class="loading">狐之助正在巡查……</div>';
 const data=await pywebview.api.check();if(data.update_result){alert(data.update_result.ok?data.update_result.message:(data.update_result.message+(data.update_result.rolled_back?'\n旧版程序已经恢复。':'')))}const cleanup=document.querySelector('#cleanupDataButton');cleanup.style.display=data.data_cleanup?'flex':'none';cleanup.dataset.token=data.data_cleanup?.token||'';cleanup.dataset.source=data.data_cleanup?.source||'';
 const issues=renderChecks(data.items);const warnings=issues.filter(x=>x.state==='warn').length;
 if(data.blocked){setState('blocked','还差一步','先处理上方红色项目，处理完成后再重新检查。','×');start.textContent='暂时无法启动'}
 else if(warnings){setState('ready','准备就绪',`${warnings} 项提醒不会阻止打开面板，需要时再处理。`,'✓');start.textContent='启动まあ丸';start.disabled=false;const sj=document.querySelector('#startJp');if(sj)sj.disabled=false}
 else{setState('ready','准备就绪','程序与运行环境均已就绪，可以安心开工。','✓');start.textContent='启动まあ丸';start.disabled=false;const sj=document.querySelector('#startJp');if(sj)sj.disabled=false}
}
function setLaunchStep(index){const steps=[...document.querySelectorAll('#launchProgress span')];document.querySelector('#launchProgress').classList.add('show');steps.forEach((step,i)=>step.className=i<index?'done':i===index?'active':'')}
async function startApp(mode){const b=document.querySelector(mode==='jp'?'#startJp':'#start');b.disabled=true;b.textContent='正在启动…';setState('','正在打开本丸','这次不需要你盯着黑窗口。','…');setLaunchStep(0);const timer=setTimeout(()=>setLaunchStep(1),500);const r=await pywebview.api.start(mode);clearTimeout(timer);if(!r.ok){document.querySelector('#launchProgress').classList.remove('show');setState('blocked','启动没有完成','错误已经留在启动记录中，可以修复后重试。','×');alert('启动失败：'+r.message);b.disabled=false;b.textContent='重新启动'}else{setLaunchStep(2);b.textContent='✓ 已启动';setState('ready','本丸已经打开','启动器的工作完成了，接下来交给まあ丸。','✓')}}
async function repair(){setState('','正在修复环境','狐之助正在补齐可以自动恢复的项目。','…');const r=await pywebview.api.repair();alert(r.message);await refresh()}
async function update(full=false){
 setState('','正在检查更新','正在向まあ丸的 GitHub 发布页确认最新版。','…');const r=await pywebview.api.check_update(full);
 if(r.update_available&&r.download_ready){
  if(confirm(r.message+'\n\n要现在安全下载到更新暂存区吗？')){
   setState('','正在下载更新','安装包下载后还会校验大小和 SHA-256。','…');setProgress(0);
   const started=await pywebview.api.download_update();
   if(!started.ok){alert(started.message);await refresh();return}
   let d;
   for(;;){
    await new Promise(resolve=>setTimeout(resolve,500));
    d=await pywebview.api.download_progress();
    if(d.total>0){setProgress(d.downloaded/d.total);document.querySelector('#stateCopy').textContent=`已下载 ${(d.downloaded/1048576).toFixed(1)} / ${(d.total/1048576).toFixed(1)} MB，下载后还会校验大小和 SHA-256。`}
    if(!d.active)break;
   }
   d=d.result||{ok:false,message:'下载没有完成，请重试。'};
   alert(d.message);
   if(d.ok&&confirm('更新文件已经校验完成。\n\n要关闭まあ丸并开始更新吗？轻量更新会在检查通过后重新打开，完整更新会打开安装向导。')){const a=await pywebview.api.apply_update();if(!a.ok)alert(a.message)}
   if(!d.ok&&r.light_update&&confirm('轻量更新下载未完成。要改用完整安装包吗？'))await update(true)
  }
 }else if(r.update_available&&r.url){if(confirm(r.message+'\n\n暂时无法自动下载，要打开发布页面吗？'))await pywebview.api.open_url(r.url)}else{alert(r.message)}
 await refresh();
}
async function exportFeedback(button){if(button.disabled)return;button.disabled=true;setToolLabel(button,'正在整理…');let r;try{r=await pywebview.api.export_diagnostics()}catch(_){r={ok:false}}if(r.ok){alert(r.message);feedbackFailures=0;document.querySelector('#issueButton').style.display='none';button.disabled=false;setToolLabel(button,'反馈错误');return}feedbackFailures+=1;if(feedbackFailures===3){document.querySelector('#issueButton').style.display='flex'}else if(feedbackFailures>=11){setToolLabel(button,'狐之助已下班');clearTimeout(feedbackResetTimer);feedbackResetTimer=setTimeout(()=>{feedbackFailures=0;button.disabled=false;setToolLabel(button,'反馈错误');document.querySelector('#issueButton').style.display='none'},3000);return}else{alert(feedbackLines[feedbackFailures]||'导出失败')}button.disabled=false;setToolLabel(button,'反馈错误')}
async function openIssue(){await pywebview.api.open_url(issueUrl)}
async function openData(){await pywebview.api.open_data()}
async function chooseEmulator(button){button.disabled=true;const r=await pywebview.api.choose_emulator();if(r.message)alert(r.message);button.disabled=false;if(r.ok)await refresh()}
async function migrateData(button){button.disabled=true;let chosen;try{chosen=await pywebview.api.choose_data_location()}catch(_){chosen={ok:false,message:'没能打开目录选择器'}}if(!chosen.ok){button.disabled=false;if(chosen.message)alert(chosen.message);return}if(!confirm(`まあ丸会先完整复制并校验用户数据，再让下次启动改用：\n\n${chosen.target}\n\n原目录暂时保留，确认新目录可用后可在启动器清理。现在开始吗？`)){button.disabled=false;return}setToolLabel(button,'正在迁移…');setState('','正在迁移数据','狐之助正在搬运家当，搬完会点一遍数。','…');const r=await pywebview.api.migrate_data(chosen.selected);alert(r.message);button.disabled=false;setToolLabel(button,'迁移数据');await refresh();if(r.ok){alert('请关闭并重新打开まあ丸。新目录通过启动检查后，会出现“清理旧副本”按钮。')}}
async function cleanupOldData(button){const source=button.dataset.source;if(!confirm(`新目录已经通过校验。确定永久删除旧数据副本吗？\n\n${source}\n\n此操作无法撤销。`))return;button.disabled=true;const r=await pywebview.api.cleanup_old_data(button.dataset.token);alert(r.message);if(r.ok){button.style.display='none'}else{button.disabled=false}}
window.addEventListener('pywebviewready',refresh);
</script></body></html>
"""


class Api:
    def __init__(self):
        self._pending_update = None
        self._download_state = None
        self._download_thread = None
        self._ledger_port = None
        self._prefer_full_update = False

    def check(self):
        ensure_runtime_data()
        auto_configure_emulator()
        checks = run_checks()
        cleanup = pending_relocation_cleanup()
        update_result = consume_result()
        if update_result and not update_result.get("ok") and update_result.get("kind") == "light":
            self._prefer_full_update = True
        return {
            "blocked": has_blocker(checks),
            "items": [item.__dict__ for item in checks],
            "update_result": update_result,
            "data_root": str(DATA_ROOT),
            "data_cleanup": ({
                "token": cleanup["token"],
                "source": cleanup["source"],
            } if cleanup else None),
        }

    def start(self, mode="automation"):
        if mode == "ledger":
            return {"ok": False, "message": "独立账房入口已暂停，请启动まあ丸后打开仓库。"}
        try:
            jp_mode = mode == "jp"
            ledger_mode = mode == "ledger"
            expected_mode = "ledger" if ledger_mode else "automation"
            if ledger_mode:
                if self._ledger_port and _panel_mode(self._ledger_port) == "ledger":
                    port = self._ledger_port
                else:
                    port = _available_port(18082)
                    self._ledger_port = port
            else:
                port = 8080

            current_mode = _panel_mode(port)
            if current_mode != expected_mode:
                if _port_alive(port):
                    return {
                        "ok": False,
                        "message": f"本机端口 {port} 已被其他程序占用，无法打开面板",
                    }
                from maamaru_app import _run_server
                error = {"traceback": ""}

                def run_server():
                    try:
                        _run_server(port=port, ledger_mode=ledger_mode)
                    except BaseException:
                        error["traceback"] = traceback.format_exc()
                        _write_launcher_log(error["traceback"])

                threading.Thread(target=run_server, daemon=True).start()
                deadline = time.time() + 15
                while (time.time() < deadline
                       and _panel_mode(port) != expected_mode
                       and not error["traceback"]):
                    time.sleep(0.2)
            observed_mode = _panel_mode(port)
            if observed_mode != expected_mode:
                if error["traceback"]:
                    detail = error["traceback"].strip().splitlines()[-1]
                    message = f"面板服务没有成功启动：{detail}\n错误记录：{LOG_DIR / 'launcher.log'}"
                elif observed_mode:
                    message = (
                        f"面板启动成了{_mode_label(observed_mode)}，"
                        f"没有进入{_mode_label(expected_mode)}。请关闭启动器后重试"
                    )
                else:
                    message = "面板服务启动等待超时；本次没有产生错误记录，请关闭启动器后重试"
                return {"ok": False, "message": message}

            # pywebview 的 JS 正在等待本次 API 调用返回；此时同步换页会互相等待。
            # 稍后从独立线程切换，先让“启动”调用顺利结束。
            def open_panel():
                time.sleep(0.15)
                window = webview.windows[0]
                # 面板的三栏布局需要 ≥1101px，启动时把窗口调到位
                window.resize(1280, 860)
                url = f"http://127.0.0.1:{port}"
                if jp_mode:
                    url += "/?server=jp"
                window.load_url(url)

            threading.Thread(target=open_panel, daemon=True).start()
            return {
                "ok": True,
                "message": ("日服本丸已启动" if jp_mode else
                            "纯净账房已启动" if ledger_mode else "まあ丸已启动"),
            }
        except Exception as exc:
            _write_launcher_log(traceback.format_exc())
            return {"ok": False, "message": str(exc)}

    def return_to_launcher(self):
        """把同一个桌面窗口切回启动器，不停止仍在运行的面板或任务。"""
        try:
            if not webview.windows:
                raise RuntimeError("启动器窗口已经关闭")

            def open_launcher():
                try:
                    # 先让 pywebview 的 API 调用返回，避免换页与当前调用互相等待。
                    time.sleep(0.15)
                    window = webview.windows[0]
                    window.resize(1080, 720)
                    window.load_html(_launcher_html())
                except BaseException:
                    _write_launcher_log(traceback.format_exc())

            threading.Thread(target=open_launcher, daemon=True).start()
            return {"ok": True, "message": "正在返回启动器"}
        except Exception as exc:
            _write_launcher_log(traceback.format_exc())
            return {"ok": False, "message": str(exc)}

    def repair(self):
        try:
            repaired = _repair_runtime_files()
            ensure_runtime_data()
            checks = run_checks()
            runtime = next((item for item in checks if item.key == "runtime"), None)
            if runtime and runtime.state == "error" and not getattr(sys, "frozen", False):
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", str(_project_root())],
                    capture_output=True,
                    timeout=300,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if result.returncode == 0:
                    repaired.append("重新安装了缺失的 Python 依赖")
                else:
                    detail = (result.stderr or result.stdout or "未知错误").strip().splitlines()[-1]
                    repaired.append(f"Python 依赖安装失败：{detail}")
                checks = run_checks()

            blockers = [item.detail for item in checks if item.state == "error"]
            warnings = [item.detail for item in checks if item.state == "warn"]
            lines = repaired or ["本地配置和数据目录无需修复"]
            if blockers:
                lines.append("仍需处理：" + "；".join(blockers))
                lines.append("若程序文件缺失，请重新下载最新版启动器。")
            elif warnings:
                lines.append("环境已可启动。提示：" + "；".join(warnings))
            else:
                lines.append("环境已恢复并通过全部检查。")
            return {"ok": not blockers, "message": "\n".join(lines)}
        except Exception as exc:
            _write_launcher_log(traceback.format_exc())
            return {"ok": False, "message": f"修复失败：{exc}"}

    def check_update(self, full=False):
        try:
            request = urllib.request.Request(
                "https://api.github.com/repos/qymd-NOT-official/maamaru-engine/releases/latest",
                headers={"User-Agent": "MaamaruLauncher/0.1"})
            with urllib.request.urlopen(request, timeout=8) as response:
                data = json.load(response)
            tag = data.get("tag_name") or "未知版本"
            latest = tag.removeprefix("v")
            comparison = (_version_tuple(latest) > _version_tuple(CURRENT_VERSION)) - (
                _version_tuple(latest) < _version_tuple(CURRENT_VERSION)
            )
            update_available = comparison > 0
            self._pending_update = None
            if comparison > 0:
                message = f"发现新版本 {tag}\n当前版本：v{CURRENT_VERSION}"
                try:
                    from .update_apply import _program_dir
                    from .updater import select_installer
                    self._pending_update = select_installer(data) if (full or self._prefer_full_update) else select_update(data, _program_dir())
                    kind = '轻量更新' if self._pending_update.get('kind') == 'light' else '完整安装包'
                    message += f"\n{kind} · {self._pending_update['size'] / 1024 / 1024:.1f} MB"
                except UpdateError as exc:
                    message += f"\n自动下载暂不可用：{exc}"
            elif comparison == 0:
                message = f"已经是最新版 v{CURRENT_VERSION}。\nGitHub 最新版本：{tag}"
            else:
                message = f"当前是待发布版本 v{CURRENT_VERSION}。\nGitHub 已发布版本：{tag}"
            return {
                "ok": True,
                "message": message,
                "update_available": update_available,
                "download_ready": self._pending_update is not None,
                "light_update": bool(self._pending_update and self._pending_update.get("kind") == "light"),
                "url": data.get("html_url") or "https://github.com/qymd-NOT-official/maamaru-engine/releases/latest",
            }
        except Exception:
            self._pending_update = None
            return {"ok": False, "message": "暂时连不上 GitHub，稍后再试。", "update_available": False}

    def download_update(self):
        if self._pending_update is None:
            return {"ok": False, "message": "请先重新检查更新。"}
        if self._download_thread is not None and self._download_thread.is_alive():
            return {"ok": True, "message": "下载已经在进行中。"}
        asset = self._pending_update
        self._download_state = {
            "active": True,
            "downloaded": 0,
            "total": int(asset["size"]),
            "result": None,
        }

        def work():
            result = None
            error = None
            for _attempt in range(2):
                try:
                    result = download_installer(asset, progress=self._report_download_progress)
                    break
                except Exception as exc:  # GitHub 直连经常中途抽风，原地重试一次
                    error = exc
            if result is not None:
                action = "已找到此前校验完成的更新文件" if result["reused"] else "更新文件已下载并通过安全校验"
                self._download_state["result"] = {
                    "ok": True,
                    "message": f"{action}。\n位置：{result['path']}",
                }
            else:
                _write_launcher_log(traceback.format_exc())
                self._download_state["result"] = {"ok": False, "message": f"更新下载失败：{error}"}
            self._download_state["active"] = False

        self._download_thread = threading.Thread(target=work, daemon=True)
        self._download_thread.start()
        return {"ok": True, "message": "下载已开始，小狐狸替你盯着进度。"}

    def download_progress(self):
        if self._download_state is None:
            return {"active": False, "downloaded": 0, "total": 0, "result": None}
        return dict(self._download_state)

    def _report_download_progress(self, downloaded, total):
        if self._download_state is not None:
            self._download_state["downloaded"] = downloaded
            self._download_state["total"] = total

    def apply_update(self):
        if self._pending_update is None:
            return {"ok": False, "message": "请先重新检查并下载更新。"}
        installer = UPDATES_DIR / self._pending_update["version"] / self._pending_update["name"]
        try:
            if _port_alive(8080):
                with urllib.request.urlopen("http://127.0.0.1:8080/api/scripts", timeout=2) as response:
                    status = json.load(response)
                if status.get("running") is not False:
                    return {"ok": False, "message": "请先让当前任务收工，再开始更新。"}
            prepare_apply(installer, self._pending_update["digest"].removeprefix("sha256:"),
                          self._pending_update["version"], self._pending_update.get("kind", "installer"))
            threading.Thread(target=lambda: (time.sleep(0.3), webview.windows[0].destroy()), daemon=True).start()
            return {"ok": True, "message": "まあ丸即将退出并开始更新。"}
        except Exception as exc:
            _write_launcher_log(traceback.format_exc())
            return {"ok": False, "message": f"无法开始安装：{exc}"}

    def open_updates(self):
        UPDATES_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(UPDATES_DIR)
        return {"ok": True}

    def open_url(self, url):
        if isinstance(url, str) and url.startswith("https://github.com/qymd-NOT-official/maamaru-engine/"):
            os.startfile(url)
            return {"ok": True}
        return {"ok": False}

    def open_data(self):
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        os.startfile(DATA_ROOT)
        return {"ok": True}

    def choose_data_location(self):
        try:
            selected = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
            if not selected:
                return {"ok": False, "message": ""}
            folder = selected[0] if isinstance(selected, (list, tuple)) else selected
            return {
                "ok": True,
                "selected": str(folder),
                "target": str(suggested_data_root(folder)),
            }
        except Exception as exc:
            _write_launcher_log(traceback.format_exc())
            return {"ok": False, "message": f"没能选择新目录：{exc}"}

    def choose_emulator(self):
        try:
            selected = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
            if not selected:
                return {"ok": False, "message": ""}
            folder = selected[0] if isinstance(selected, (list, tuple)) else selected
            found = configure_mumu_from_folder(folder)
            if found is None:
                return {
                    "ok": False,
                    "message": "这个目录里没有同时找到 MuMu 的 ADB 和管理器。\n"
                               "请选择 MuMu 的安装目录，而不是桌面快捷方式所在的目录。",
                }
            return {
                "ok": True,
                "message": f"已设置 MuMu 模拟器：\n{found.manager_path}",
            }
        except Exception as exc:
            _write_launcher_log(traceback.format_exc())
            return {"ok": False, "message": f"没能设置模拟器：{exc}"}

    def migrate_data(self, selected_folder):
        try:
            result = relocate_user_data(selected_folder)
            size_mb = result["bytes"] / (1024 * 1024)
            return {
                "ok": True,
                "message": (
                    f"用户数据已复制并校验完成：{result['files']} 个文件，"
                    f"约 {size_mb:.1f} MB。\n\n新目录：{result['target']}\n"
                    "原目录尚未删除，重新启动まあ丸后再确认清理。"
                ),
            }
        except Exception as exc:
            _write_launcher_log(traceback.format_exc())
            return {"ok": False, "message": f"迁移没有完成：{exc}\n原目录未删除。"}

    def cleanup_old_data(self, confirmation_token):
        try:
            result = cleanup_previous_data(str(confirmation_token or ""))
            return {
                "ok": True,
                "message": f"旧数据副本已清理：{result['source']}\n当前数据仍保存在：{result['target']}",
            }
        except Exception as exc:
            _write_launcher_log(traceback.format_exc())
            return {"ok": False, "message": f"旧副本没有删除：{exc}"}

    def export_diagnostics(self):
        try:
            path = create_diagnostic_bundle()
            try:
                subprocess.Popen(
                    ["explorer.exe", "/select,", str(path)],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError:
                os.startfile(path.parent)
            return {"ok": True, "message": f"错误反馈包已经生成，并在文件夹中替你选好了。\n\n{path.name}"}
        except Exception as exc:
            _write_launcher_log(traceback.format_exc())
            return {"ok": False, "message": f"暂时没能生成错误反馈包：{exc}"}


def _launcher_html() -> str:
    return (HTML
            .replace("__VERSION__", CURRENT_VERSION)
            .replace("__ICON_URI__", _asset_data_uri("maamaru-launcher-header.png"))
            .replace("__GARDEN_URI__", _asset_data_uri("honmaru_garden_after_rain.png"))
            .replace("__GARDEN_FOX_URI__", _asset_data_uri("fox_idle.png"))
            .replace("__FOX1_URI__", _asset_data_uri("fox_run_1_alpha.png"))
            .replace("__FOX2_URI__", _asset_data_uri("fox_run_2_alpha.png")))


def main():
    ensure_runtime_data()
    html = _launcher_html()
    webview.create_window("まあ丸启动器", html=html,
                          js_api=Api(), width=1080, height=720,
                          min_size=(860, 660), resizable=True)
    webview.start(debug=False, icon=str(_launcher_icon_path()))


def _launcher_icon_path() -> Path:
    return _project_root() / "launcher" / "assets" / "maamaru-launcher.ico"


def _asset_data_uri(name: str, directory: str = "launcher/assets") -> str:
    # WebView2 NavigateToString 对 HTML 总大小有限制（约 2MB），
    # 内联大图会直接导致窗口初始化失败；大图须先缩到启动器专用小图。
    path = _project_root() / directory / name
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _repair_runtime_files() -> list[str]:
    """Restore writable JSON defaults without overwriting healthy user data."""
    repaired = []
    for target, source, label in _runtime_defaults():
        if not target.exists():
            continue
        try:
            json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            backup = target.with_suffix(target.suffix + f".broken-{time.strftime('%Y%m%d-%H%M%S')}")
            target.replace(backup)
            if source.is_file():
                target.write_bytes(source.read_bytes())
            else:
                target.write_text("{}\n", encoding="utf-8")
            repaired.append(f"已备份并重建损坏的{label}")
    ensure_runtime_data()
    return repaired


def _runtime_defaults():
    from touken.runtime_paths import CONFIG_PATH, PANEL_CONFIG_PATH, SCHEDULE_PATH

    return (
        (CONFIG_PATH, _project_root() / "touken_config.example.json", "本丸配置"),
        (PANEL_CONFIG_PATH, _project_root() / "panel" / "panel_config.example.json", "面板配置"),
        (SCHEDULE_PATH, _project_root() / "panel" / "expedition_schedule.json", "远征排班"),
    )


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = []
    for segment in value.split("."):
        digits = "".join(char for char in segment if char.isdigit())
        parts.append(int(digits or 0))
    return tuple((parts + [0, 0, 0])[:3])


def _port_alive(port: int = 8080) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _panel_mode(port: int) -> str | None:
    """端口有人不等于那是まあ丸；必须由模式接口验明正身。"""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/app-mode", timeout=0.8) as response:
            payload = json.loads(response.read().decode("utf-8"))
        mode = payload.get("mode")
        return mode if mode in {"automation", "ledger"} else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _mode_label(mode: str) -> str:
    return "纯净账房" if mode == "ledger" else "正常管家模式"


def _available_port(preferred: int) -> int:
    """优先使用稳定端口；被 QQ 等程序占用时自动申请本机空闲端口。"""
    for candidate in (preferred, 0):
        if candidate and _port_alive(candidate):
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", candidate))
                probe.listen(1)
                port = int(probe.getsockname()[1])
        except OSError:
            continue
        if port:
            return port
    raise OSError("找不到可用的本机端口")


def _write_launcher_log(content: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / "launcher.log"
        # The panel startup hook writes the original exception before Uvicorn
        # converts it to SystemExit(3).  Append here instead of erasing it.
        with path.open("a", encoding="utf-8") as stream:
            if path.stat().st_size:
                stream.write("\n\n--- launcher ---\n")
            stream.write(content)
    except OSError:
        pass
