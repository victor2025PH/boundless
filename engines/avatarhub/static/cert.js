/* ==========================================================================
   无界科技 BOUNDLESS · 可下载「验真证书」共享生成器（全站单一实现）
   --------------------------------------------------------------------------
   用法：页面加载 brand.js 后再加载本文件 <script src="/static/cert.js"></script>，
   验真出结果后调用：
     window.__cert.generate({
       kind:'音频'|'视频/图片', tone:'good'|'mid'|'none', verdict:'判定文案',
       maker:'出品方(claim_generator)', file:'文件名', rows:[['键','值'], ...]
     }, base)   // base：API/二维码所在 origin（默认当前页 origin；converse 传 HUB）
   纯前端 canvas → 品牌化 PNG，零后端依赖；二维码复用 /api/qr，指纹取 /api/provenance/pubkey。
   品牌名/图标/产品线随白标(window.__brandConfig)，主色取 --bd-acc，自动白标。
   ========================================================================== */
(function () {
  function esc(s){ return String(s==null?'':s); }
  function cssVar(n, fb){
    try{ var v=getComputedStyle(document.documentElement).getPropertyValue(n).trim(); return v||fb; }catch(_){ return fb; }
  }
  function brandInfo(){
    var name='无界科技 BOUNDLESS', logo='🛡', product='幻声 VoiceX / 幻影 LiveX / 幻颜 FaceX';
    try{ var c=window.__brandConfig&&window.__brandConfig.get&&window.__brandConfig.get();
      if(c){ if(c.name)name=c.name; if(c.logo)logo=c.logo; if(c.product)product=c.product; } }catch(_){}
    return {name:name, logo:logo, product:product};
  }
  var _pkFp = {};   // 按 base 缓存公钥指纹
  async function fingerprint(base){
    base = base||''; if(_pkFp[base]!==undefined) return _pkFp[base];
    try{
      var d=await (await fetch(base+'/api/provenance/pubkey')).json();
      var pem=(d&&d.public_key)||''; var b64=pem.replace(/-----[^-]+-----/g,'').replace(/\s+/g,'');
      var der=Uint8Array.from(atob(b64), function(c){return c.charCodeAt(0);});
      var buf=await crypto.subtle.digest('SHA-256', der);
      var hex=[].slice.call(new Uint8Array(buf)).map(function(b){return b.toString(16).padStart(2,'0');});
      _pkFp[base]=hex.slice(0,8).join(':').toUpperCase();
    }catch(_){ _pkFp[base]=''; }
    return _pkFp[base];
  }
  function loadImg(src){ return new Promise(function(res){ var i=new Image(); i.onload=function(){res(i);}; i.onerror=function(){res(null);}; i.src=src; }); }
  function roundRect(x,X,Y,w,h,r){ x.beginPath(); x.moveTo(X+r,Y); x.arcTo(X+w,Y,X+w,Y+h,r); x.arcTo(X+w,Y+h,X,Y+h,r); x.arcTo(X,Y+h,X,Y,r); x.arcTo(X,Y,X+w,Y,r); x.closePath(); }
  function truncate(s,n){ s=String(s||''); return s.length>n ? s.slice(0,n-1)+'…' : s; }

  async function generate(d, base){
    base = base||'';
    var br=brandInfo();
    var fp=await fingerprint(base);
    var qrTarget=(base||location.origin)+'/verify';
    var qr=await loadImg(base+'/api/qr?data='+encodeURIComponent(qrTarget));
    var acc=cssVar('--bd-acc','#4f7aff');
    var COL={bg:'#0e1320', panel:'#161d2e', line:'#26304a', txt:'#e9edf7', mut:'#aab4cc', faint:'#6b7794',
             good:'#34d399', warn:'#fbbf24', none:'#6b7794'};
    var vcol = d.tone==='good'?COL.good : d.tone==='mid'?COL.warn : COL.none;
    var rows=d.rows||[];
    var W=1240, PAD=64, H=470 + rows.length*52 + 40;
    var s=2, cv=document.createElement('canvas'); cv.width=W*s; cv.height=H*s;
    var x=cv.getContext('2d'); x.scale(s,s);
    var F='"Microsoft YaHei","PingFang SC","Segoe UI",sans-serif';
    x.fillStyle=COL.bg; x.fillRect(0,0,W,H);
    x.strokeStyle=acc; x.lineWidth=3; x.strokeRect(10,10,W-20,H-20);
    x.fillStyle=acc; x.font='30px '+F; x.textBaseline='alphabetic';
    x.fillText(br.logo+' '+br.name, PAD, 78);
    x.fillStyle=COL.faint; x.font='16px '+F; x.fillText(br.product, PAD, 106);
    x.fillStyle=COL.txt; x.font='bold 34px '+F; x.textAlign='right';
    x.fillText('内容验真证书', W-PAD, 78);
    x.fillStyle=COL.faint; x.font='15px '+F; x.fillText('Content Authenticity Certificate', W-PAD, 104);
    x.textAlign='left';
    x.strokeStyle=COL.line; x.lineWidth=1; x.beginPath(); x.moveTo(PAD,128); x.lineTo(W-PAD,128); x.stroke();
    var y=170;
    x.fillStyle=COL.panel; roundRect(x,PAD,y,W-PAD*2,72,12); x.fill();
    x.fillStyle=vcol; x.font='bold 24px '+F; x.textBaseline='middle';
    x.fillText((d.tone==='good'?'✔  ':d.tone==='mid'?'⚠  ':'—  ')+(d.verdict||''), PAD+24, y+38);
    x.textBaseline='alphabetic';
    y+=72+28;
    if(d.maker){
      x.fillStyle=acc; x.font='bold 22px '+F; x.fillText('出品方', PAD, y+6);
      x.fillStyle=COL.txt; x.font='22px '+F; x.fillText(truncate(d.maker,46), PAD+110, y+6);
      y+=44;
    }
    x.fillStyle=COL.mut; x.font='16px '+F;
    x.fillText('资产类型：'+(d.kind||'-')+(d.file?('　·　文件：'+truncate(d.file,42)):''), PAD, y+4);
    y+=36;
    x.strokeStyle=COL.line; x.beginPath(); x.moveTo(PAD,y); x.lineTo(W-PAD,y); x.stroke(); y+=30;
    x.font='18px '+F;
    rows.forEach(function(r){
      x.fillStyle=COL.mut; x.fillText(r[0], PAD, y);
      x.fillStyle=COL.txt; x.fillText(truncate(String(r[1]),52), PAD+220, y);
      y+=52;
    });
    var fy=H-150;
    x.strokeStyle=COL.line; x.beginPath(); x.moveTo(PAD,fy-24); x.lineTo(W-PAD,fy-24); x.stroke();
    x.fillStyle=COL.faint; x.font='14px '+F;
    if(fp) x.fillText('签名公钥指纹 (SHA-256/8)：'+fp, PAD, fy+6);
    x.fillText('签发时间：'+new Date().toLocaleString(), PAD, fy+32);
    x.fillText('凭证算法对齐 C2PA / CAI · Ed25519 公钥可对外离线验签', PAD, fy+58);
    x.fillStyle=COL.mut; x.fillText('扫码打开验真工具，可独立复验 →', PAD, fy+90);
    if(qr){ var qs=120; x.drawImage(qr, W-PAD-qs, fy-6, qs, qs);
            x.fillStyle=COL.faint; x.font='12px '+F; x.textAlign='center';
            x.fillText('在线复验', W-PAD-qs/2, fy+qs+18); x.textAlign='left'; }
    await new Promise(function(res){
      cv.toBlob(function(blob){ var a=document.createElement('a'); a.href=URL.createObjectURL(blob);
        a.download='验真证书_'+br.name.replace(/\s+/g,'')+'_'+Date.now()+'.png'; a.click();
        setTimeout(function(){URL.revokeObjectURL(a.href);},1500); res(); }, 'image/png');
    });
  }

  window.__cert = { generate: generate, fingerprint: fingerprint };
})();
