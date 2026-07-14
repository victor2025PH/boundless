/* video-stream.js — 流媒体: WebCodecs硬件解码、ABR自适应码率、Scrcpy H.264直播流、屏幕录制 */
/* ── WebCodecs H.264 硬件加速解码器 ── */
const _WEBCODECS_OK = typeof VideoDecoder !== 'undefined';

class WcH264 {
  constructor(cvs){
    this._cvs=cvs; this._ctx=cvs.getContext('2d');
    this._dec=null; this._ok=false;
    this._sps=null; this._pps=null;
    this._pending=0; this._frames=0;
    this._fpsTimes=[]; this._drops=0; this._codecStr='';
    this._bytesIn=0; this._bpsT0=performance.now(); this._kbps=0;
    this._gotKey=false;
    this.onResize=null;
  }
  init(){
    this._dec=new VideoDecoder({
      output:frame=>{
        this._pending--;
        if(this._cvs.width!==frame.displayWidth||this._cvs.height!==frame.displayHeight){
          this._cvs.width=frame.displayWidth;
          this._cvs.height=frame.displayHeight;
          if(this.onResize) this.onResize(frame.displayWidth,frame.displayHeight);
        }
        this._ctx.drawImage(frame,0,0);
        frame.close();
        this._frames++;
        this._fpsTimes.push(performance.now());
      },
      error:(e)=>{this._pending=0;console.error('[WebCodecs] decode error:',e);}
    });
  }
  feed(buf){
    if(buf.byteLength<16) return;
    this._bytesIn+=buf.byteLength;
    const now=performance.now();
    if(now-this._bpsT0>=1000){this._kbps=this._bytesIn*8/(now-this._bpsT0);this._bytesIn=0;this._bpsT0=now;}
    const dv=new DataView(buf);
    const hi32=dv.getUint32(0);
    const ts=((hi32&0x3FFFFFFF)*4294967296+dv.getUint32(4));
    const h264=new Uint8Array(buf,12);
    if(h264.length<4) return;
    const nals=this._split(h264);
    let isKey=false,newCfg=false;
    const videoNals=[];
    for(const n of nals){
      if(!n.length) continue;
      const t=n[0]&0x1f;
      if(t===7){this._sps=n;newCfg=true;}
      else if(t===8){this._pps=n;newCfg=true;}
      else if(t===6||t===9){/* 跳过SEI/AUD辅助NAL */}
      else{
        if(t===5) isKey=true;
        videoNals.push(n);
      }
    }
    if(newCfg&&this._sps&&this._pps){this._configure();this._gotKey=false;}
    if(!videoNals.length) return;
    if(!this._ok||this._dec.state!=='configured') return;
    // 配置后必须等到IDR关键帧才开始解码，跳过之前的P帧
    if(!this._gotKey&&!isKey) return;
    if(isKey) this._gotKey=true;
    if(this._pending>3&&!isKey){this._drops++;return;}
    const avcc=this._toAVCC(videoNals);
    if(!avcc.length) return;
    try{
      this._dec.decode(new EncodedVideoChunk({type:isKey?'key':'delta',timestamp:ts,data:avcc}));
      this._pending++;
    }catch(e){console.error('[WcH264] decode throw:',e);}
  }
  _configure(){
    const p=this._sps[1],c=this._sps[2],l=this._sps[3];
    const codec='avc1.'+p.toString(16).padStart(2,'0')+c.toString(16).padStart(2,'0')+l.toString(16).padStart(2,'0');
    const sz=11+this._sps.length+this._pps.length;
    const b=new ArrayBuffer(sz),v=new DataView(b),a=new Uint8Array(b);
    v.setUint8(0,1);v.setUint8(1,p);v.setUint8(2,c);v.setUint8(3,l);
    v.setUint8(4,0xFF);v.setUint8(5,0xE1);
    v.setUint16(6,this._sps.length);a.set(this._sps,8);
    v.setUint8(8+this._sps.length,1);
    v.setUint16(9+this._sps.length,this._pps.length);
    a.set(this._pps,11+this._sps.length);
    try{
      if(this._dec.state==='configured') this._dec.flush().catch(()=>{});
      this._dec.configure({codec,optimizeForLatency:true,hardwareAcceleration:'prefer-hardware',description:new Uint8Array(b)});
      this._ok=true; this._codecStr=codec;
      console.log('[WebCodecs] 硬件解码器就绪:',codec);
    }catch(e){this._ok=false;console.warn('[WebCodecs] 配置失败:',e);}
  }
  _split(d){
    const r=[],idx=[];
    for(let i=0;i<d.length-2;i++){
      if(d[i]===0&&d[i+1]===0){
        if(d[i+2]===1){idx.push({p:i,l:3});i+=2;}
        else if(d[i+2]===0&&i+3<d.length&&d[i+3]===1){idx.push({p:i,l:4});i+=3;}
      }
    }
    for(let k=0;k<idx.length;k++){
      const s=idx[k].p+idx[k].l,e=k+1<idx.length?idx[k+1].p:d.length;
      if(e>s) r.push(d.subarray(s,e));
    }
    return r;
  }
  _toAVCC(nals){
    let len=0;for(const n of nals)len+=4+n.length;
    if(!len)return new Uint8Array(0);
    const out=new Uint8Array(len),v=new DataView(out.buffer);
    let off=0;
    for(const n of nals){v.setUint32(off,n.length);out.set(n,off+4);off+=4+n.length;}
    return out;
  }
  fps(){
    const now=performance.now(),cut=now-2000;
    this._fpsTimes=this._fpsTimes.filter(t=>t>cut);
    return this._fpsTimes.length/2;
  }
  stats(){return{fps:Math.round(this.fps()),drops:this._drops,codec:this._codecStr,kbps:Math.round(this._kbps),pending:this._pending,frames:this._frames};}
  resetDrops(){this._drops=0;}
  destroy(){
    if(this._dec){try{if(this._dec.state!=='closed')this._dec.close();}catch(e){}this._dec=null;}
    this._ok=false;this._sps=null;this._pps=null;this._pending=0;
  }
}

/* ── ABR 自适应码率控制器 ── */
const _ABR_LEVELS=['thumb','minimal','low','medium','high','ultra'];
const _ABR_TARGET_FPS={thumb:10,minimal:15,low:24,medium:30,high:30,ultra:60};
let _abrIdx=3,_abrStable=0,_abrChanging=false,_abrEnabled=false,_abrStartTime=0;

function _abrReset(quality){
  _abrIdx=_ABR_LEVELS.indexOf(quality||'medium');
  if(_abrIdx<0)_abrIdx=3;
  _abrStable=0;_abrChanging=false;_abrStartTime=performance.now();
}

async function _abrEvaluate(){
  if(!_abrEnabled||_abrChanging||_qualityChanging||!_streamActive||!_streamDecoder) return;
  if(performance.now()-_abrStartTime<6000) return;
  const s=_streamDecoder.stats();
  const target=_ABR_TARGET_FPS[_ABR_LEVELS[_abrIdx]]||30;
  if(s.fps<target*0.5||s.drops>5){
    _abrStable=0;
    if(_abrIdx>0){
      _abrIdx--;_abrChanging=true;
      const q=_ABR_LEVELS[_abrIdx];
      showToast('自适应降质: '+q);
      try{
        await api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/stream/quality`,{quality:q});
        await new Promise(r=>setTimeout(r,300));
        _streamDecoder.resetDrops();
        _saveLastFrame();_showReconnectOverlay();
        stopStreaming(true);await new Promise(r=>setTimeout(r,200));startStreaming();
      }catch(e){}
      _abrChanging=false;
    }
  }else if(s.fps>=target*0.85&&s.drops===0){
    _abrStable++;
    if(_abrStable>=15&&_abrIdx<_ABR_LEVELS.length-2){
      _abrIdx++;_abrStable=0;_abrChanging=true;
      const q=_ABR_LEVELS[_abrIdx];
      showToast('自适应提质: '+q);
      try{
        await api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/stream/quality`,{quality:q});
        await new Promise(r=>setTimeout(r,300));
        _streamDecoder.resetDrops();
        _saveLastFrame();_showReconnectOverlay();
        stopStreaming(true);await new Promise(r=>setTimeout(r,200));startStreaming();
      }catch(e){}
      _abrChanging=false;
    }
  }else{_abrStable=0;}
  _streamDecoder.resetDrops();
}

/* ── Scrcpy H.264 Streaming ── */
let _streamWs=null;
let _streamFirstFrameTimer=null;
let _streamJmuxer=null;
let _streamDecoder=null;
let _streamActive=false;

async function toggleStreaming(){
  if(_streamActive){
    stopStreaming();
  }else{
    startStreaming();
  }
}

async function startStreaming(){
  if(!modalDeviceId){showToast('无设备','warn');return;}
  if(_streamFirstFrameTimer){clearTimeout(_streamFirstFrameTimer);_streamFirstFrameTimer=null;}
  if(_streamWs){try{_streamWs.onclose=null;_streamWs.close();}catch(e){}_streamWs=null;}
  if(_streamDecoder){try{_streamDecoder.destroy();}catch(e){}_streamDecoder=null;}
  stopModalAuto();
  const btn=document.getElementById('stream-toggle');
  if(btn){btn.textContent='连接中...';btn.style.background='var(--accent)';}

  try{
    const body=document.getElementById('modal-body');
    let displayEl;

    if(_WEBCODECS_OK){
      const wrap=document.createElement('div');
      wrap.id='stream-wrap';
      wrap.style.cssText='position:relative;display:inline-block;margin:0 auto';
      const cvs=document.createElement('canvas');
      cvs.id='stream-video';
      cvs.width=800;cvs.height=600;
      cvs.style.cssText='cursor:crosshair;touch-action:none;background:#000;display:block';
      wrap.appendChild(cvs);
      const overlay=document.createElement('canvas');
      overlay.id='touch-overlay';
      overlay.width=800;overlay.height=600;
      overlay.style.cssText='position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:2';
      wrap.appendChild(overlay);
      const hud=document.createElement('div');
      hud.id='stream-hud';
      hud.style.cssText='position:absolute;top:4px;left:4px;background:rgba(0,0,0,.6);color:#0f0;font:11px monospace;padding:3px 6px;border-radius:4px;z-index:3;pointer-events:none;display:block';
      wrap.appendChild(hud);
      body.innerHTML='';
      body.style.display='flex';body.style.justifyContent='center';
      body.appendChild(wrap);
      _streamDecoder=new WcH264(cvs);
      _streamDecoder.onResize=(w,h)=>{
        // 用 screen-viewport 获取稳定的容器宽度（不受 modal-body zoom transform 影响）
        const vp=document.getElementById('screen-viewport');
        const maxW=(vp&&vp.clientWidth>0?vp.clientWidth:0)||(body.offsetWidth>0?body.offsetWidth:0)||500;
        const maxH=Math.max(window.innerHeight*0.74,300);
        const scale=Math.min(1,maxW/w,maxH/h);
        const dw=Math.round(w*scale),dh=Math.round(h*scale);
        // 防止闪烁：仅在尺寸真正变化时才更新 CSS
        if(cvs.style.width!==dw+'px'||cvs.style.height!==dh+'px'){
          cvs.style.width=dw+'px';cvs.style.height=dh+'px';
          wrap.style.width=dw+'px';wrap.style.height=dh+'px';
        }
        overlay.width=dw;overlay.height=dh;
        // 同步 offline overlay 尺寸
        const ol=document.getElementById('stream-offline-overlay');
        if(ol){ol.style.width=dw+'px';ol.style.height=dh+'px';}
      };
      _streamDecoder.init();
      if(!_abrChanging) _abrReset();
      displayEl=cvs;
    }else if(!window._jmuxerFailed&&typeof JMuxer!=='undefined'){
      const video=document.createElement('video');
      video.id='stream-video';
      video.style.cssText='max-width:100%;max-height:74vh;cursor:crosshair;touch-action:none;background:#000';
      video.muted=true;video.autoplay=true;video.playsInline=true;
      body.innerHTML='';
      body.appendChild(video);
      _streamJmuxer=new JMuxer({node:'stream-video',mode:'video',flushingTime:0,fps:30,debug:false});
      displayEl=video;
    }else{
      showToast('无解码器可用','warn');return;
    }

    /* ctrl-hint removed — HUD/fullscreen moved to sidebar */

    // 本地设备：通过画质API重启scrcpy确保拿到IDR关键帧
    // 集群设备：代理会创建新连接，自动拿到IDR，不需要额外操作
    // _qualityChanging=true 时跳过（changeStreamQuality 已经调用过画质API）
    if(!_currentModalIsCluster&&!_qualityChanging){
      try{
        const qs=document.getElementById('quality-sel');
        const q=qs?qs.value:'medium';
        await api('POST',`/devices/${modalDeviceId}/stream/quality`,{quality:q});
      }catch(e){}
      await new Promise(r=>setTimeout(r,500));
    }

    _streamWs=new WebSocket(_wsUrl(`${_modalApiPrefix()}/devices/${modalDeviceId}/stream/ws`));
    _streamWs.binaryType='arraybuffer';
    const _wsDeviceId=modalDeviceId;
    let _streamGotConfig=false;

    _streamWs.onopen=function(){
      _streamActive=true;
      btn.textContent='\u25A0 停止流';
      const qsel=document.getElementById('quality-sel');
      if(qsel){qsel.style.display='inline-block';_lastSuccessQuality=qsel.value||'medium';}
      // 设备卡片实时流指示器
      if(modalDeviceId){const ind=document.getElementById('stream-ind-'+modalDeviceId.substring(0,8));if(ind)ind.style.display='inline';}
      _startStatsPolling();
      // 每30s检查设备网络状态
      if(_networkCheckTimer) clearInterval(_networkCheckTimer);
      _networkCheckTimer=setInterval(_checkDeviceNetwork,30000);
      showToast(_WEBCODECS_OK?'实时流已连接 · 点击=触摸 · 拖拽=滑动 · 键盘可用':'实时流已连接(软解码) · 点击=触摸 · 拖拽=滑动','info',4000,'stream-status');
      _streamGotConfig=false;
      _wsBinCount=0;
      if(_streamFirstFrameTimer) clearTimeout(_streamFirstFrameTimer);
      _streamFirstFrameTimer=setTimeout(function(){
        if(modalDeviceId!==_wsDeviceId) return;
        if(_wsBinCount>0||_streamGotConfig) return;
        showToast('实时流无数据(超时)。若地址栏不是 :8000，请 F12 控制台执行: localStorage.setItem("oc_api_origin","http://127.0.0.1:8000") 后刷新','warn',4000,'stream-status');
        try{_streamWs.close();}catch(e){}
        _streamActive=false;
        stopStreaming(true);
        captureModalScreen();
        startModalAuto();
        if(btn){btn.textContent='\u25B6 实时流';btn.style.background='var(--bg-input)';}
      },20000);
    };

    let _wsBinCount=0;
    _streamWs.onmessage=function(ev){
      if(typeof ev.data==='string'){
        try{
          const cfg=JSON.parse(ev.data);
          if(cfg.type==='config'){
            _streamGotConfig=true;
            if(_streamFirstFrameTimer){clearTimeout(_streamFirstFrameTimer);_streamFirstFrameTimer=null;}
            // ws 是最权威来源（来自 scrcpy 实际分辨率），覆盖 HTTP fallback
            if(typeof _setModalScreenSize==='function')
              _setModalScreenSize(cfg.width,cfg.height,'ws');
            else
              modalScreenSize={w:cfg.width,h:cfg.height};
            _streamHasControl=!!cfg.has_control;
            if(_streamHasControl){
              const s=document.getElementById('ctrl-status');
              if(s) s.textContent=_WEBCODECS_OK?'GPU硬解 + 低延迟控制':'低延迟控制';
              _hideReadOnlyBanner();
            }else{
              // P0-3: control socket 未建立时，给用户明确提示而不是静默吞点击
              console.warn('[stream] control socket NOT established — touchscreen will be read-only. Sidebar buttons (HOME/BACK) still work via adb input keyevent.');
              _showReadOnlyBanner('🚫 投屏只读模式 — 屏幕点击无效（侧边栏按钮可用）。请重启实时流；若反复失败，重启 server.bat');
              const s=document.getElementById('ctrl-status');
              if(s) s.textContent='⚠️ video-only';
            }
            if(cfg.quality){
              const qs=document.getElementById('quality-sel');
              if(qs) qs.value=cfg.quality;
              if(!_abrChanging) _abrReset(cfg.quality);
            }
          }
        }catch(e){}
      }else{
        _wsBinCount++;
        if(_streamFirstFrameTimer){clearTimeout(_streamFirstFrameTimer);_streamFirstFrameTimer=null;}
        if(_wsBinCount<=5) console.log('[stream] binary frame #'+_wsBinCount+' size='+ev.data.byteLength);
        if(_streamDecoder) _streamDecoder.feed(ev.data);
        else if(_streamJmuxer){
          // scrcpy帧=12字节header(8B PTS+4B长度)+H.264数据，JMuxer只需要H.264部分
          const h264=ev.data.byteLength>12?new Uint8Array(ev.data,12):new Uint8Array(ev.data);
          _streamJmuxer.feed({video:h264});
        }
      }
    };

    _streamWs.onclose=function(ev){
      if(_streamFirstFrameTimer){clearTimeout(_streamFirstFrameTimer);_streamFirstFrameTimer=null;}
      if(modalDeviceId!==_wsDeviceId) return;
      if(ev.code===4004){
        showToast('设备未找到，无法启动实时流','error',4000,'stream-status');
        stopStreaming();
        return;
      }
      if(ev.code===1011){
        showToast('scrcpy 启动失败，请检查设备连接状态。回退到截图模式','error',4000,'stream-status');
        stopStreaming();
        captureModalScreen();
        startModalAuto();
        return;
      }
      if(_streamActive&&modalDeviceId&&!_abrChanging&&!_qualityChanging){
        _saveLastFrame();
        _showReconnectOverlay();
        showToast('实时流断开，重连中...','warn',4000,'stream-status');
        setTimeout(()=>{
          if(modalDeviceId===_wsDeviceId&&_streamActive&&!_qualityChanging){_streamActive=false;startStreaming();}
        },1500);
        return;
      }
      if(_streamActive&&!_abrChanging) stopStreaming();
    };
    _streamWs.onerror=function(){
      if(_streamFirstFrameTimer){clearTimeout(_streamFirstFrameTimer);_streamFirstFrameTimer=null;}
      console.log('实时流不可用，使用截图模式');
      _streamActive=false;
      stopStreaming(true);
      captureModalScreen();
      startModalAuto();
      if(btn){btn.textContent='\u25B6 实时流';btn.style.background='var(--bg-input)';}
    };

    _attachVideoListeners(displayEl);

  }catch(e){
    showToast('启动实时流失败: '+e.message,'warn');
    stopStreaming();
  }
}

let _lastFrameDataUrl=null;

function _saveLastFrame(){
  const cvs=document.getElementById('stream-video');
  if(cvs&&cvs.tagName==='CANVAS'&&cvs.width>10){
    try{_lastFrameDataUrl=cvs.toDataURL('image/jpeg',0.8);}catch(e){}
  }
}

function _showReconnectOverlay(){
  const body=document.getElementById('modal-body');
  if(!body) return;
  if(_lastFrameDataUrl){
    body.innerHTML=`<div style="position:relative;display:inline-block;margin:0 auto">
      <img src="${_lastFrameDataUrl}" style="max-width:100%;max-height:74vh;filter:brightness(0.5)">
      <div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);text-align:center;color:#fff">
        <div style="font-size:24px;animation:spin 1s linear infinite">&#8635;</div>
        <div style="margin-top:8px;font-size:14px">重连中...</div>
      </div>
    </div>`;
  }
}

function stopStreaming(keepFrame){
  if(_streamFirstFrameTimer){clearTimeout(_streamFirstFrameTimer);_streamFirstFrameTimer=null;}
  if(_networkCheckTimer){clearInterval(_networkCheckTimer);_networkCheckTimer=null;}
  // 隐藏设备卡片实时流指示器
  if(modalDeviceId){const ind=document.getElementById('stream-ind-'+modalDeviceId.substring(0,8));if(ind)ind.style.display='none';}
  if(!keepFrame) _saveLastFrame();
  _streamActive=false;
  _streamHasControl=false;
  _hideReadOnlyBanner();
  _readOnlyToastShown=false;
  _stopStatsPolling();
  _hideOfflineOverlay();
  if(_streamWs){try{_streamWs.close();}catch(e){}_streamWs=null;}
  if(_streamDecoder){try{_streamDecoder.destroy();}catch(e){}_streamDecoder=null;}
  if(_streamJmuxer){try{_streamJmuxer.destroy();}catch(e){}_streamJmuxer=null;}
  const btn=document.getElementById('stream-toggle');
  if(btn){btn.textContent='\u25B6 实时流';btn.style.background='var(--bg-input)';}
  const s=document.getElementById('ctrl-status');if(s) s.textContent='';
  const ss=document.getElementById('stream-stats');if(ss) ss.textContent='';
  const qs=document.getElementById('quality-sel');if(qs) qs.style.display='none';
  if(!keepFrame&&modalDeviceId){
    captureModalScreen();
    startModalAuto();
  }
}

/* ── Screen Recording (client-side MediaRecorder) ── */
let _recording=false;
let _recordTimer=null;
let _recordStart=0;
let _mediaRecorder=null;
let _recordChunks=[];

async function toggleRecording(){
  if(_recording) await stopRecording();
  else await startRecording();
}

async function startRecording(){
  if(!modalDeviceId||_recording) return;
  const cvs=document.getElementById('stream-video');
  if(cvs&&cvs.tagName==='CANVAS'&&cvs.captureStream){
    try{
      const stream=cvs.captureStream(30);
      _recordChunks=[];
      const opts={mimeType:'video/webm;codecs=vp9'};
      if(!MediaRecorder.isTypeSupported(opts.mimeType)) opts.mimeType='video/webm;codecs=vp8';
      if(!MediaRecorder.isTypeSupported(opts.mimeType)) opts.mimeType='video/webm';
      _mediaRecorder=new MediaRecorder(stream,{...opts,videoBitsPerSecond:3000000});
      _mediaRecorder.ondataavailable=e=>{if(e.data.size>0)_recordChunks.push(e.data);};
      _mediaRecorder.onstop=()=>{
        const blob=new Blob(_recordChunks,{type:_mediaRecorder.mimeType||'video/webm'});
        const url=URL.createObjectURL(blob);
        const a=document.createElement('a');
        a.href=url;
        const ts=new Date().toISOString().replace(/[:.]/g,'-').substring(0,19);
        a.download=`openclaw_${ALIAS[modalDeviceId]||modalDeviceId.substring(0,8)}_${ts}.webm`;
        a.click();
        URL.revokeObjectURL(url);
        _recordChunks=[];
        showToast(`录屏已保存: ${a.download}`);
      };
      _mediaRecorder.start(1000);
      _recording=true;
      _recordStart=Date.now();
      const btn=document.getElementById('record-toggle');
      btn.style.background='#ef4444';btn.style.color='#fff';
      _recordTimer=setInterval(()=>{
        const sec=Math.round((Date.now()-_recordStart)/1000);
        const m=Math.floor(sec/60),s=sec%60;
        btn.textContent=`\u25A0 ${m}:${s<10?'0':''}${s}`;
      },1000);
      showToast('客户端录屏已开始 (WebM)');
      return;
    }catch(e){console.warn('MediaRecorder failed, falling back to server-side',e);}
  }
  try{
    await api('POST',`/devices/${modalDeviceId}/record/start`,{});
    _recording=true;
    _recordStart=Date.now();
    const btn=document.getElementById('record-toggle');
    btn.style.background='#ef4444';btn.style.color='#fff';
    _recordTimer=setInterval(()=>{
      const sec=Math.round((Date.now()-_recordStart)/1000);
      const m=Math.floor(sec/60),s=sec%60;
      btn.textContent=`\u25A0 ${m}:${s<10?'0':''}${s}`;
    },1000);
    showToast('服务端录屏已开始');
  }catch(e){showToast('录屏启动失败','warn');}
}

async function stopRecording(){
  if(!_recording) return;
  if(_recordTimer){clearInterval(_recordTimer);_recordTimer=null;}
  _recording=false;
  const btn=document.getElementById('record-toggle');
  btn.textContent='处理中...';btn.style.background='var(--bg-input)';btn.style.color='';
  if(_mediaRecorder&&_mediaRecorder.state!=='inactive'){
    _mediaRecorder.stop();
    _mediaRecorder=null;
    btn.textContent='\u25CF 录屏';
    return;
  }
  try{
    const r=await api('POST',`/devices/${modalDeviceId}/record/stop`,{});
    btn.textContent='\u25CF 录屏';
    showToast(`录屏已保存 (${r.count||0} 个文件)`);
  }catch(e){
    btn.textContent='\u25CF 录屏';
    showToast('录屏停止失败','warn');
  }
}

let _streamHasControl=false;
let _readOnlyToastShown=false;
let _statsTimer=null;

function _startStatsPolling(){
  _stopStatsPolling();
  _statsTimer=setInterval(async()=>{
    if(!modalDeviceId||!_streamActive) return;
    try{
      const el=document.getElementById('stream-stats');
      if(_streamDecoder){
        const s=_streamDecoder.stats();
        if(el) el.textContent=`${s.fps}fps ${s.kbps}kbps`;
        const hud=document.getElementById('stream-hud');
        if(hud&&hud.style.display!=='none'){
          // 水印：显示设备编号和别名，多屏监控时防止误操作
          const devLabel=(typeof ALIAS!=='undefined'&&ALIAS[modalDeviceId])||
                         (modalDeviceId?modalDeviceId.substring(0,8):'?');
          const sizeStr=modalScreenSize?`${modalScreenSize.w}x${modalScreenSize.h}`:'';
          hud.innerHTML=`<span style="color:#60a5fa;font-weight:700">${devLabel}</span> ${sizeStr}<br>`+
            `FPS: ${s.fps} | ${s.kbps}kbps<br>`+
            `Codec: ${s.codec||'?'} GPU硬解<br>`+
            `Drops: ${s.drops} | Queue: ${s.pending}<br>`+
            `Decoded: ${s.frames} | ABR: ${_abrEnabled?_ABR_LEVELS[_abrIdx]:'OFF'}`;
        }
        _abrEvaluate();
      }else{
        const r=await api('GET',`${_modalApiPrefix()}/devices/${modalDeviceId}/stream/stats`);
        if(el&&r.active) el.textContent=`${r.fps}fps ${Math.round(r.kbps)}kbps`;
      }
    }catch(e){}
  },2000);
}

function _stopStatsPolling(){
  if(_statsTimer){clearInterval(_statsTimer);_statsTimer=null;}
}

function _toggleHud(){
  const h=document.getElementById('stream-hud');
  if(h) h.style.display=h.style.display==='none'?'block':'none';
}

function _toggleFullscreen(){
  const wrap=document.getElementById('stream-wrap');
  if(!wrap) return;
  if(document.fullscreenElement){
    document.exitFullscreen();
  }else{
    // 全屏前保存当前 CSS 尺寸，退出时可以恢复
    const cvs=document.getElementById('stream-video');
    if(cvs){
      wrap.setAttribute('data-pre-fs-w',cvs.style.width);
      wrap.setAttribute('data-pre-fs-h',cvs.style.height);
      cvs.style.width='100vw';cvs.style.height='100vh';
      cvs.style.objectFit='contain';
    }
    wrap.requestFullscreen().catch(()=>{});
  }
}
document.addEventListener('fullscreenchange',()=>{
  if(!document.fullscreenElement){
    const wrap=document.getElementById('stream-wrap');
    const cvs=document.getElementById('stream-video');
    if(cvs&&wrap){
      // 恢复全屏前的尺寸，否则触发 onResize 重新计算
      const preW=wrap.getAttribute('data-pre-fs-w');
      const preH=wrap.getAttribute('data-pre-fs-h');
      if(preW){cvs.style.width=preW;wrap.style.width=preW;}
      if(preH){cvs.style.height=preH;wrap.style.height=preH;}
      cvs.style.objectFit='';
    }
    if(cvs&&_streamDecoder&&_streamDecoder._cvs===cvs){
      // 稍微延迟等 DOM 更新后再重算
      setTimeout(()=>_streamDecoder.onResize(cvs.width,cvs.height),50);
    }
  }
});

let _qualityChanging=false;
let _lastSuccessQuality='medium';

async function changeStreamQuality(){
  const sel=document.getElementById('quality-sel');
  if(!sel||!modalDeviceId||_qualityChanging) return;
  const targetQ=sel.value;
  const prevQ=_lastSuccessQuality;
  if(targetQ===prevQ) return;

  _qualityChanging=true;
  sel.disabled=true;
  const label=sel.options[sel.selectedIndex]?.text||targetQ;
  showToast(`切换画质中: ${label}...`,'info',12000,'stream-quality');

  // ── 先API，成功才停流重启，失败自动降级最多三次 ──
  let success=false;
  let attemptQ=targetQ;
  const fallbackLevels=_ABR_LEVELS.slice(0,_ABR_LEVELS.indexOf(targetQ));
  const queue=[targetQ,...fallbackLevels.reverse()];

  for(let i=0;i<Math.min(queue.length,3);i++){
    attemptQ=queue[i];
    try{
      await api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/stream/quality`,{quality:attemptQ});
      success=true;
      break;
    }catch(e){
      if(i<queue.length-1){
        showToast(`${attemptQ} 切换失败，尝试降级...`,'info',1500,'stream-quality');
        await new Promise(r=>setTimeout(r,600));
      }
    }
  }

  sel.disabled=false;

  if(!success){
    _qualityChanging=false;
    sel.value=prevQ;
    showToast(`画质切换失败，已保持当前画质 (${prevQ})`,'warn',4000,'stream-quality');
    // 状态栏显示失败原因
    const bar=document.getElementById('quality-status-bar');
    const msg=document.getElementById('quality-status-msg');
    if(bar&&msg){msg.textContent=`切换 "${targetQ}" 失败，设备保持 "${prevQ}" 画质`;bar.style.display='block';}
    // 无论流是否还在，都以原画质重启（流可能因切换过程中断了）
    stopStreaming(true);
    try{ await api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/stream/quality`,{quality:prevQ}); }catch(_){}
    await new Promise(r=>setTimeout(r,400));
    startStreaming();
    return;
  }
  // 成功时清除失败状态栏
  const bar=document.getElementById('quality-status-bar');
  if(bar) bar.style.display='none';

  // 成功：更新记录值，平滑过渡到新流
  _lastSuccessQuality=attemptQ;
  if(attemptQ!==targetQ){
    sel.value=attemptQ;
    showToast(`画质已降级至: ${attemptQ}（原目标 ${targetQ} 不可用）`,'warn',3000,'stream-quality');
  }else{
    showToast(`画质已切换: ${label}`,'success',2000,'stream-quality');
  }
  _saveLastFrame();
  _showReconnectOverlay();
  stopStreaming(true);
  await new Promise(r=>setTimeout(r,300));
  // 在 startStreaming 建立连接后再清除 _qualityChanging，
  // 确保 startStreaming 内部跳过重复调用画质 API，onclose 也不误触重连
  startStreaming();
  // 短暂等待 WebSocket 建立后再解锁（避免 onclose 1500ms 定时器在锁内触发）
  await new Promise(r=>setTimeout(r,600));
  _qualityChanging=false;
}

let _lastCtrlSend=0;
const _CTRL_THROTTLE_MS=20;
const _BINARY_CTRL=true;

function _sendCtrl(msg){
  if(!_streamWs||_streamWs.readyState!==1||!_streamHasControl) return false;
  if(msg.type==='touch'&&msg.action===2){
    const now=performance.now();
    if(now-_lastCtrlSend<_CTRL_THROTTLE_MS) return true;
    _lastCtrlSend=now;
  }
  if(_BINARY_CTRL&&(msg.type==='touch'||msg.type==='scroll')){
    const buf=new ArrayBuffer(14);
    const dv=new DataView(buf);
    if(msg.type==='touch'){
      dv.setUint8(0,0x80);
      dv.setUint8(1,msg.action||0);
      dv.setInt32(2,msg.x||0);
      dv.setInt32(6,msg.y||0);
      dv.setInt16(10,msg.pointerId!==undefined?msg.pointerId:-1);
    }else{
      dv.setUint8(0,0x83);
      dv.setUint8(1,0);
      dv.setInt32(2,msg.x||0);
      dv.setInt32(6,msg.y||0);
      dv.setInt16(10,msg.hscroll||0);
      dv.setInt16(12,msg.vscroll||0);
    }
    _streamWs.send(buf);
    return true;
  }
  _streamWs.send(JSON.stringify(msg));
  return true;
}

function _drawTouchIndicator(clientX,clientY,type){
  const overlay=document.getElementById('touch-overlay');
  if(!overlay) return;
  const ctx=overlay.getContext('2d');
  const rect=overlay.getBoundingClientRect();
  const x=(clientX-rect.left)*(overlay.width/rect.width);
  const y=(clientY-rect.top)*(overlay.height/rect.height);
  if(type==='down'){
    ctx.clearRect(0,0,overlay.width,overlay.height);
    ctx.beginPath();ctx.arc(x,y,18,0,Math.PI*2);
    ctx.fillStyle='rgba(59,130,246,0.35)';ctx.fill();
    ctx.beginPath();ctx.arc(x,y,6,0,Math.PI*2);
    ctx.fillStyle='rgba(59,130,246,0.7)';ctx.fill();
  }else if(type==='move'){
    ctx.beginPath();ctx.arc(x,y,4,0,Math.PI*2);
    ctx.fillStyle='rgba(59,130,246,0.25)';ctx.fill();
  }else{
    setTimeout(()=>{if(overlay)overlay.getContext('2d').clearRect(0,0,overlay.width,overlay.height);},200);
  }
}

/* ── 设备网络状态监控 ── */
let _deviceNetworkOk=true;
let _networkCheckTimer=null;

async function _checkDeviceNetwork(){
  if(!modalDeviceId||!_streamActive) return;
  try{
    const r=await api('GET',`${_modalApiPrefix()}/devices/${modalDeviceId}/battery`,{},2000);
    _deviceNetworkOk=true;
    _hideOfflineOverlay();
  }catch(e){
    // 仅当连续失败才标记离线（避免单次超时误判）
  }
}

function _showOfflineOverlay(msg){
  const wrap=document.getElementById('stream-wrap');
  if(!wrap) return;
  let ol=document.getElementById('stream-offline-overlay');
  if(!ol){
    ol=document.createElement('div');
    ol.id='stream-offline-overlay';
    ol.style.cssText='position:absolute;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.65);display:flex;flex-direction:column;align-items:center;justify-content:center;z-index:10;border-radius:4px;cursor:not-allowed';
    ol.innerHTML=`<div style="font-size:28px;margin-bottom:8px">📵</div>
      <div style="color:#fff;font-size:13px;font-weight:600;margin-bottom:4px">${msg||'设备网络已断开'}</div>
      <div style="color:rgba(255,255,255,.6);font-size:11px">操控已暂停，网络恢复后自动解除</div>`;
    wrap.appendChild(ol);
  }else{
    ol.style.display='flex';
    ol.querySelector('div+div').textContent=msg||'设备网络已断开';
  }
}

function _hideOfflineOverlay(){
  const ol=document.getElementById('stream-offline-overlay');
  if(ol) ol.style.display='none';
}

/* P0-3 + P1-C: 只读模式横幅 + 一键重启按钮 — control socket 未建立时显示 */
function _showReadOnlyBanner(msg){
  const wrap=document.getElementById('stream-wrap');
  if(!wrap) return;
  let bn=document.getElementById('stream-readonly-banner');
  if(!bn){
    bn=document.createElement('div');
    bn.id='stream-readonly-banner';
    bn.style.cssText='position:absolute;top:0;left:0;right:0;background:rgba(220,38,38,0.92);color:#fff;font-size:12px;font-weight:600;padding:6px 10px;text-align:center;z-index:9;border-radius:4px 4px 0 0;display:flex;justify-content:center;align-items:center;gap:8px';
    wrap.appendChild(bn);
  }
  bn.style.display='flex';
  // P2-③: 优先调 reconnect-control 端点 (仅重连 control 通道, 不重建视频流, 快 5-10s);
  // 失败再 fallback 到 stop+start.
  const btnHtml=`<button id="readonly-reconnect-btn" style="background:#fff;color:#dc2626;border:none;padding:3px 10px;border-radius:3px;font-weight:600;font-size:11px;cursor:pointer">🔁 重连 control 通道</button>`;
  bn.innerHTML=`<span>${msg||'投屏只读模式'}</span>${btnHtml}`;
  const btn=document.getElementById('readonly-reconnect-btn');
  if(btn){
    btn.onclick=async function(ev){
      ev.preventDefault();ev.stopPropagation();
      btn.disabled=true;
      btn.textContent='重连中...';
      const did=modalDeviceId;
      if(!did){btn.disabled=false; btn.textContent='🔁 重连 control 通道'; return;}
      try{
        // P2-③ 先尝试 reconnect-control (轻量, 不动视频)
        const r=await api('POST',`/devices/${encodeURIComponent(did)}/stream/reconnect-control`,{});
        if(r&&r.has_control){
          _streamHasControl=true;
          _hideReadOnlyBanner();
          _readOnlyToastShown=false;
          showToast('✅ control 通道已重连, 屏幕点击恢复','success',3000,'stream-status');
          const s=document.getElementById('ctrl-status');
          if(s) s.textContent=_WEBCODECS_OK?'GPU硬解 + 低延迟控制':'低延迟控制';
          return;
        }
        // server 报 still video-only → 升级到完整重启
        showToast('control 仍未建立, 改成重启完整实时流','warn',2500,'stream-status');
        throw new Error('still video-only');
      }catch(e){
        console.warn('[readonly-reconnect] reconnect-control failed, fallback to stop+start:',e);
        try{
          stopStreaming(true);
          setTimeout(()=>{ if(typeof startStreaming==='function') startStreaming(); },1000);
        }catch(e2){
          console.error('[readonly-reconnect] fallback failed',e2);
        }
        btn.disabled=false;
        btn.textContent='🔁 重连 control 通道';
      }
    };
  }
}

function _hideReadOnlyBanner(){
  const bn=document.getElementById('stream-readonly-banner');
  if(bn) bn.style.display='none';
}

function _attachVideoListeners(video){
  let _touchSampleN=0;
  video.addEventListener('mousedown',function(e){
    e.preventDefault();
    // 离线浮层可见时阻断操控
    const ol=document.getElementById('stream-offline-overlay');
    if(ol&&ol.style.display!=='none') return;
    const rect=video.getBoundingClientRect();
    const rx=(e.clientX-rect.left)/rect.width;
    const ry=(e.clientY-rect.top)/rect.height;
    if(!modalScreenSize) return;
    const dx=Math.round(rx*modalScreenSize.w), dy=Math.round(ry*modalScreenSize.h);
    _dragState={startX:dx,startY:dy,clientX:e.clientX,clientY:e.clientY,img:video,moved:false};
    _drawTouchIndicator(e.clientX,e.clientY,'down');
    // P0-3: 采样日志 — 每 10 次点击打 1 次，便于排查"点不动"是 has_control 还是坐标系
    if(((++_touchSampleN)%10)===1){
      console.log(`[stream] touch sample #${_touchSampleN}: device=(${dx},${dy}) screen=${modalScreenSize.w}x${modalScreenSize.h} hasControl=${_streamHasControl} renderRect=${Math.round(rect.width)}x${Math.round(rect.height)}`);
    }
    if(_streamHasControl){
      _sendCtrl({type:'touch',action:0,x:dx,y:dy});
    }else{
      // 只读模式下用户点屏幕：toast 提醒一次（避免重复刷屏）
      if(!_readOnlyToastShown){
        _readOnlyToastShown=true;
        showToast('🚫 投屏只读模式 — 屏幕点击无效（control socket 未建立）。请关掉再开实时流','warn',4000,'stream-status');
        setTimeout(()=>{_readOnlyToastShown=false;},10000);
      }
    }
  });
  video.addEventListener('mousemove',function(e){
    const pt=_videoCoord(video,e);
    if(pt){const c=document.getElementById('ctrl-coord');if(c)c.textContent=`${pt.x}, ${pt.y}`;}
    if(!_dragState) return;
    if(Math.abs(e.clientX-_dragState.clientX)+Math.abs(e.clientY-_dragState.clientY)>8){
      _dragState.moved=true;
      _drawTouchIndicator(e.clientX,e.clientY,'move');
      _showSwipeTrail(e.clientX,e.clientY);
      if(_streamHasControl&&pt) _sendCtrl({type:'touch',action:2,x:pt.x,y:pt.y});
    }
  });
  video.addEventListener('mouseup',function(e){
    _drawTouchIndicator(e.clientX,e.clientY,'up');
    if(!_dragState||!modalDeviceId) return;
    const pt=_videoCoord(video,e);
    if(!pt){_dragState=null;return;}
    if(!_dragState.moved){
      _recordMacroStep({type:'tap',x:pt.x,y:pt.y});
    }else{
      // 用欧几里得距离（Math.hypot）替代 Manhattan 距离，精准估算 swipe 时长
      const pixelDist=Math.hypot(e.clientX-_dragState.clientX, e.clientY-_dragState.clientY);
      const dur=Math.min(800,Math.max(100,pixelDist*1.8|0));
      _recordMacroStep({type:'swipe',x1:_dragState.startX,y1:_dragState.startY,x2:pt.x,y2:pt.y,duration:dur});
    }
    if(_streamHasControl){
      _sendCtrl({type:'touch',action:1,x:pt.x,y:pt.y});
    }else{
      if(!_dragState.moved){
        api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/input/tap`,{x:pt.x,y:pt.y});
      }else{
        const pixelDist2=Math.hypot(e.clientX-_dragState.clientX, e.clientY-_dragState.clientY);
        const dur2=Math.min(800,Math.max(100,pixelDist2*1.8|0));
        api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/input/swipe`,{x1:_dragState.startX,y1:_dragState.startY,x2:pt.x,y2:pt.y,duration:dur2});
      }
    }
    _dragState=null;
  });
  video.addEventListener('mouseleave',function(){
    if(_dragState&&_streamHasControl){
      _sendCtrl({type:'touch',action:1,x:_dragState.startX,y:_dragState.startY});
    }
    _dragState=null;
  });
  video.addEventListener('wheel',function(e){
    e.preventDefault();
    if(e.ctrlKey||e.metaKey){
      if(e.deltaY<0) zoomIn(); else zoomOut();
      return;
    }
    const pt=_videoCoord(video,e);
    if(pt) _sendCtrl({type:'scroll',x:pt.x,y:pt.y,vscroll:e.deltaY>0?-1:1});
  },{passive:false});

  const _activeTouches=new Map();
  function _touchToDevice(video,touch){
    const rect=video.getBoundingClientRect();
    const rx=(touch.clientX-rect.left)/rect.width;
    const ry=(touch.clientY-rect.top)/rect.height;
    if(!modalScreenSize) return null;
    const x=Math.round(rx*modalScreenSize.w);
    const y=Math.round(ry*modalScreenSize.h);
    if(x<0||y<0||x>modalScreenSize.w||y>modalScreenSize.h) return null;
    return {x,y};
  }
  video.addEventListener('touchstart',function(e){
    e.preventDefault();
    for(let i=0;i<e.changedTouches.length;i++){
      const t=e.changedTouches[i];
      const pt=_touchToDevice(video,t);
      if(!pt) continue;
      const pid=t.identifier;
      _activeTouches.set(pid,pt);
      if(_streamHasControl){
        _sendCtrl({type:'touch',action:0,x:pt.x,y:pt.y,pointerId:pid});
      }
    }
    if(e.touches.length===1){
      const t=e.touches[0];
      const pt=_touchToDevice(video,t);
      if(pt) _dragState={startX:pt.x,startY:pt.y,clientX:t.clientX,clientY:t.clientY,img:video,moved:false};
    }
  },{passive:false});
  video.addEventListener('touchmove',function(e){
    e.preventDefault();
    for(let i=0;i<e.changedTouches.length;i++){
      const t=e.changedTouches[i];
      const pt=_touchToDevice(video,t);
      if(!pt) continue;
      const pid=t.identifier;
      _activeTouches.set(pid,pt);
      if(_streamHasControl){
        _sendCtrl({type:'touch',action:2,x:pt.x,y:pt.y,pointerId:pid});
      }
    }
    if(_dragState&&e.touches.length===1){
      const t=e.touches[0];
      const dx=t.clientX-_dragState.clientX;
      const dy=t.clientY-_dragState.clientY;
      if(Math.abs(dx)+Math.abs(dy)>8) _dragState.moved=true;
    }
  },{passive:false});
  video.addEventListener('touchend',function(e){
    for(let i=0;i<e.changedTouches.length;i++){
      const t=e.changedTouches[i];
      const pt=_touchToDevice(video,t)||_activeTouches.get(t.identifier)||{x:0,y:0};
      const pid=t.identifier;
      if(_streamHasControl){
        _sendCtrl({type:'touch',action:1,x:pt.x,y:pt.y,pointerId:pid});
      }
      _activeTouches.delete(pid);
    }
    if(_dragState&&e.touches.length===0){
      const t=e.changedTouches[0];
      const pt=_touchToDevice(video,t);
      if(pt&&!_dragState.moved&&!_streamHasControl){
        api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/input/tap`,{x:pt.x,y:pt.y});
      }else if(pt&&_dragState.moved&&!_streamHasControl){
        api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/input/swipe`,{x1:_dragState.startX,y1:_dragState.startY,x2:pt.x,y2:pt.y,duration:300});
      }
      _dragState=null;
    }
  });
  video.addEventListener('touchcancel',function(e){
    for(let i=0;i<e.changedTouches.length;i++){
      const t=e.changedTouches[i];
      const pid=t.identifier;
      const pt=_activeTouches.get(pid)||{x:0,y:0};
      if(_streamHasControl) _sendCtrl({type:'touch',action:1,x:pt.x,y:pt.y,pointerId:pid});
      _activeTouches.delete(pid);
    }
    _dragState=null;
  });
}

function _videoCoord(video,e){
  const rect=video.getBoundingClientRect();
  const rx=(e.clientX-rect.left)/rect.width;
  const ry=(e.clientY-rect.top)/rect.height;
  if(!modalScreenSize) return null;
  return {x:Math.round(rx*modalScreenSize.w),y:Math.round(ry*modalScreenSize.h)};
}

