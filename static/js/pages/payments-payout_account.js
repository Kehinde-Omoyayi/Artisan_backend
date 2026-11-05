/* Enhance only provider-rendered bank choices; payout submission stays server-owned. */
(() => {
  function init() {
    const form = document.querySelector('[data-payout-form]');
    if (!form || form.dataset.bankReady) return;
    const select = form.querySelector('[data-payout-bank-select]');
    const root = form.querySelector('[data-bank-picker]');
    if (!select || !root) return;
    // Saved details render locked (native select, disabled) until a verified
    // change request starts; do not build the interactive picker over that.
    if (select.disabled) return;
    form.dataset.bankReady = 'true';
    const toggle=root.querySelector('[data-bank-toggle]'), panel=root.querySelector('[data-bank-panel]');
    const search=root.querySelector('[data-payout-bank-search]'), list=root.querySelector('[data-bank-options]');
    const value=root.querySelector('[data-bank-label]'), status=root.querySelector('[data-bank-status]'), error=root.querySelector('[data-bank-error]');
    const choices=[...select.options].filter(o=>o.value&&!o.disabled).map(o=>({value:o.value,label:o.textContent.trim()}));
    const id=select.id;select.id=id+'-native';toggle.id=id;toggle.setAttribute('aria-describedby','payout-bank-selection');value.id='payout-bank-selection';
    select.hidden=true;select.required=false;root.hidden=false;
    const close=(focus=false)=>{panel.hidden=true;toggle.setAttribute('aria-expanded','false');if(focus)toggle.focus();};
    const sync=()=>{value.textContent=choices.find(o=>o.value===select.value)?.label||'Choose a supported bank';};
    function render(){
      const q=search.value.trim().toLocaleLowerCase();
      const matches=choices.filter(o=>o.label.toLocaleLowerCase().includes(q));
      list.replaceChildren();
      matches.forEach(o=>{
        const b=document.createElement('button');b.type='button';b.className='payout-bank-option';b.textContent=o.label;
        b.setAttribute('aria-pressed',String(select.value===o.value));
        b.addEventListener('click',()=>{select.value=o.value;select.dispatchEvent(new Event('change',{bubbles:true}));error.hidden=true;toggle.removeAttribute('aria-invalid');sync();close(true);});
        list.append(b);
      });
      status.textContent=matches.length?`${matches.length} supported bank${matches.length===1?'':'s'}`:'No matching bank. Try a different search.';
    }
    toggle.addEventListener('click',()=>{
      if(!panel.hidden){close();return;}
      search.value='';render();panel.hidden=false;toggle.setAttribute('aria-expanded','true');search.focus();
    });
    search.addEventListener('input',render);
    search.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();const options=[...list.querySelectorAll('button')];if(options.length===1)options[0].click();else options[0]?.focus();}});select.addEventListener('change',sync);
    root.addEventListener('keydown',e=>{
      if(e.key==='Escape'){e.preventDefault();close(true);}
      if((e.key==='ArrowDown'||e.key==='ArrowUp')&&!panel.hidden){
        const buttons=[...list.querySelectorAll('button')];if(!buttons.length)return;
        e.preventDefault();const index=buttons.indexOf(document.activeElement),step=e.key==='ArrowDown'?1:-1;
        buttons[(index+step+buttons.length)%buttons.length].focus();
      }
    });
    document.addEventListener('click',e=>{if(!root.contains(e.target))close();});
    document.addEventListener('focusin',e=>{if(!root.contains(e.target))close();});
    form.addEventListener('submit',e=>{if(!choices.some(o=>o.value===select.value)){e.preventDefault();error.hidden=false;toggle.setAttribute('aria-invalid','true');toggle.focus();}});
    form.addEventListener('reset',()=>setTimeout(()=>{sync();close();error.hidden=true;toggle.removeAttribute('aria-invalid');},0));
    if(!choices.length){toggle.disabled=true;status.textContent='Bank catalogue unavailable. Reload this page to retry.';panel.hidden=false;search.hidden=true;form.querySelector('button[type=submit]').disabled=true;}
    sync();
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init,{once:true});else init();
})();