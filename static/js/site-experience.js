/* Shared motion policy and read-only activity dialogs. No API writes. */
window.ArtisanMotion = {heroRotationMs:2000,titleTypeMs:350,descriptionTypeMs:380};
(() => {
  const root=document.documentElement, media=matchMedia('(prefers-reduced-motion: reduce)');
  root.classList.add('experience-enhanced');
  const button=document.querySelector('[data-motion-toggle]');
  let preference=false;try{preference=localStorage.getItem('artisan-reduce-motion')==='true';}catch(_){}
  let reduced=false,frame=0;const animations=new Set();
  function apply(){
    reduced=media.matches||preference;root.classList.toggle('experience-motion-off',reduced);
    if(button){button.textContent=reduced?'Motion off':'Motion on';button.setAttribute('aria-pressed',String(!reduced));button.setAttribute('aria-label',reduced?'Enable decorative motion':'Reduce decorative motion');}
    if(reduced)animations.forEach(a=>a.cancel());
    window.dispatchEvent(new CustomEvent('experience-motion-change',{detail:{reduced}}));
  }
  button?.addEventListener('click',()=>{preference=!preference;try{localStorage.setItem('artisan-reduce-motion',String(preference));}catch(_){}apply();});
  media.addEventListener?.('change',apply);apply();
  document.addEventListener('visibilitychange',()=>root.classList.toggle('experience-idle',document.hidden));
  function scroll(){if(frame||reduced||document.hidden)return;frame=requestAnimationFrame(()=>{root.style.setProperty('--ambient-shift',`${Math.min(scrollY*.035,60)}px`);frame=0;});}
  addEventListener('scroll',scroll,{passive:true});
  document.querySelectorAll('[data-activity-dialog]').forEach(dialog=>{
    const trigger=document.querySelector(`[data-activity-open="${dialog.id}"]`),items=[...dialog.querySelectorAll('[data-activity-item]')];
    const prev=dialog.querySelector('[data-activity-prev]'),next=dialog.querySelector('[data-activity-next]'),status=dialog.querySelector('[data-activity-page]');let page=0;
    function render(){items.forEach((item,index)=>item.hidden=Math.floor(index/3)!==page);prev.disabled=page===0;next.disabled=(page+1)*3>=items.length;status.textContent=items.length?`${page*3+1}–${Math.min((page+1)*3,items.length)} of ${items.length} recent items`:'No recent items';}
    trigger?.addEventListener('click',()=>{page=0;render();dialog.showModal();trigger.setAttribute('aria-expanded','true');});
    dialog.querySelector('[data-activity-close]')?.addEventListener('click',()=>dialog.close());
    dialog.addEventListener('click',event=>{if(event.target===dialog){const r=dialog.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)dialog.close();}});
    dialog.addEventListener('close',()=>{trigger?.setAttribute('aria-expanded','false');trigger?.focus();});
    prev?.addEventListener('click',()=>{if(page>0){page--;render();}});next?.addEventListener('click',()=>{if((page+1)*3<items.length){page++;render();}});render();
  });
})();