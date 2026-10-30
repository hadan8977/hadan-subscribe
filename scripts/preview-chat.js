"use strict";
(() => {
  if(document.body.dataset.preview!=="true" || !document.querySelector('#demo-transcript'))return;
  const $=s=>document.querySelector(s); let step=0;
  const words={
    'zh-CN':{you:'你',agent:'Agent',prompt:'帮我订阅 ',authorize:'验证邮箱并授权',intro:'打开授权页面，验证邮箱并确认连接。',done:'已完成授权。',ready:'已连接。测试正文和附件已读取，后续更新会自动送来。',manual:'已连接。你让我查看时，我会读取新报告；邮件继续接收。',browse:'我能打开网页，但无法接收私有更新。请先用邮箱订阅。',start:'用邮箱订阅',next:['模拟完成授权','预览收到更新','预览收到修订','重新演示'],read:'看看最新更新。',update:'本期新增 SMPB、移出 SMPD；SMPA 评分从 8.10 调整为 8.42。报告和附件已保存。',revision:'这份报告有一处修订：SMPC 的下次财报日期已更新。旧版记录保留，新版附件已替换。',revisionUser:'这份报告有新版本吗？'},
    en:{you:'You',agent:'Agent',prompt:'Subscribe me to ',authorize:'Verify email and authorize',intro:'Open the authorization page to verify your email and approve this connection.',done:'I have authorized it.',ready:'Connected. I have read the test report and file. Future updates will arrive automatically.',manual:'Connected. Ask me to check for new reports whenever you need them. Email delivery stays on.',browse:'I can open the website, but cannot receive private updates. Start with an email subscription.',start:'Subscribe by email',next:['Simulate authorization','Preview an update','Preview a revision','Restart'],read:'Check the latest update.',update:'SMPB was added and SMPD removed. SMPA’s score changed from 8.10 to 8.42. The report and attachments are saved.',revision:'This report has a revision: SMPC’s next earnings date changed. I kept the previous version and updated the files.',revisionUser:'Is there a newer version?'},
    es:{you:'Tú',agent:'Agente',prompt:'Suscríbeme a ',authorize:'Verificar correo y autorizar',intro:'Abre la página para verificar tu correo y autorizar la conexión.',done:'Ya lo he autorizado.',ready:'Conectado. He leído el informe y el archivo de prueba. Recibiré las próximas actualizaciones automáticamente.',manual:'Conectado. Pídeme que consulte los informes cuando los necesites. El correo sigue activado.',browse:'Puedo abrir la web, pero no recibir actualizaciones privadas. Empieza con la suscripción por correo.',start:'Suscribirse por correo',next:['Simular autorización','Ver una actualización','Ver una revisión','Reiniciar'],read:'Consulta la última actualización.',update:'Se añadió SMPB y se retiró SMPD. La puntuación de SMPA pasó de 8,10 a 8,42. El informe y los archivos están guardados.',revision:'Hay una revisión: cambió la fecha de resultados de SMPC. Conservé la versión anterior y actualicé los archivos.',revisionUser:'¿Hay una versión nueva?'}
  };
  function render(){
    const language=$('#demo-language').value==='auto'?document.documentElement.lang:$('#demo-language').value;
    const w=words[language]||words.en,mode=$('#demo-capability').value,list=$('#demo-transcript');list.replaceChildren();
    function message(role,text,links=[]){const li=document.createElement('li');li.className='demo-message '+role;const label=document.createElement('span');label.className='demo-role';label.textContent=w[role];const p=document.createElement('p');p.textContent=text;li.append(label,p);for(const [name,path,download] of links){const a=document.createElement('a');a.className='demo-message-link';a.textContent=name;a.href=path;if(download)a.download=download;else{a.target='_blank';a.rel='noopener';}li.append(a);}list.append(li);}
    message('you',w.prompt+location.origin);
    message('agent',mode==='browse'?w.browse:w.intro,[[mode==='browse'?w.start:w.authorize,mode==='browse'?'/app?flow=subscribe':'/app?flow=subscribe&pair=DEADBEEF']]);
    if(step>0&&mode!=='browse'){message('you',w.done);message('agent',mode==='auto'?w.ready:w.manual);}
    if(step>1&&mode!=='browse'){message('you',w.read);message('agent',w.update,[['Quant_2026-10-28.xlsx','/static/quant-sample.xlsx','Quant_2026-10-28.xlsx'],['Portfolio_2026-10-28.png','/static/quant-sample.png','Portfolio_2026-10-28.png']]);}
    if(step>2&&mode!=='browse'){message('you',w.revisionUser);message('agent',w.revision);}
    $('#demo-next').hidden=mode==='browse'||step===3;$('#demo-next').textContent=w.next[step];
  }
  $('#demo-next').addEventListener('click',()=>{step=(step+1)%4;render();});
  $('#demo-reset').addEventListener('click',()=>{step=0;render();});
  $('#demo-capability').addEventListener('change',()=>{step=0;render();});$('#demo-language').addEventListener('change',render);
  new MutationObserver(render).observe(document.documentElement,{attributes:true,attributeFilter:['lang']});render();
})();
