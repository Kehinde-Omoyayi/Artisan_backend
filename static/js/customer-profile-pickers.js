/** Shared customer/artisan profile pickers, institutions, professional links and optional DOB calendar. */
(() => {
  const form = document.querySelector('[data-profile-pickers]');
  if (!form) return;
  const data = document.getElementById('profile-geography-data');
  let geo;
  try { geo = JSON.parse(data.textContent); } catch (_) { return; }
  // The server-rendered copy is a no-network fallback from the same static file.
  if (form.dataset.geographyUrl) fetch(form.dataset.geographyUrl).then(r=>{if(!r.ok)throw Error('geography');return r.json();}).then(value=>{geo=value;}).catch(()=>{});
  const names = [...new Set(['phone_country_calling_code', 'country', 'state', 'area', 'nearest_institution', 'gender', 'date_of_birth', ...[...form.querySelectorAll('select[name]')].map(f=>f.name)])].filter(n=>form.elements.namedItem(n));
  const fields = Object.fromEntries(names.map(n => [n, form.elements.namedItem(n)]));
  const rows = {};
  let requestSerial = 0, controller, debounce;
  const make = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
  const button = (text, fn, cls) => { const b = make('button', cls, text); b.type = 'button'; b.addEventListener('click', fn); return b; };
  const sort = xs => [...new Set(xs)].sort((a,b) => a.localeCompare(b));
  const closeAll = () => Object.values(rows).forEach(r => { r.panel.hidden = true; r.toggle.setAttribute('aria-expanded', 'false'); });
  const labelFor = name => {
    const f = fields[name];
    if (name === 'nearest_institution') return f.value ? (f.dataset.selectedLabel || form.dataset.institutionLabel || [f.value,form.elements.namedItem('nearest_institution_abbreviation')?.value ? `(${form.elements.namedItem('nearest_institution_abbreviation').value})` : ''].filter(Boolean).join(' ')) : 'Choose nearest institution';
    if (f.tagName === 'SELECT') return f.selectedOptions[0]?.textContent || 'Choose an option';
    return f.value || (name === 'date_of_birth' ? 'Choose date of birth' : `Choose ${name}`);
  };
  const refresh = n => { if (rows[n]) rows[n].value.textContent = labelFor(n); };
  const assign = (n, value, label) => { const f = fields[n]; f.value = value; if (label !== undefined) f.dataset.selectedLabel = label; f.dispatchEvent(new Event('change', {bubbles:true})); refresh(n); };
  const choose = (n, value, label) => {
    assign(n, value, label);
    const downstream = {country:['state','area','nearest_institution'],state:['area','nearest_institution'],area:['nearest_institution']};
    (downstream[n] || []).forEach(key => assign(key, '', ''));
    if (downstream[n] || (n==='nearest_institution' && !value)) {
      controller?.abort();requestSerial++;
      const abbreviation=form.elements.namedItem('nearest_institution_abbreviation');
      if(abbreviation)abbreviation.value='';
      const source=form.elements.namedItem('nearest_institution_source_id');
      if(source)source.value='';
    }
    // Directly reduce institution list when geography changes — auto-enable local scope
    const hasGeo = fields.country?.value || fields.state?.value || fields.area?.value;
    if(hasGeo && rows.nearest_institution){
      rows.nearest_institution.localScope = true;
      const scopeRow = rows.nearest_institution.panel?.querySelector('.institution-scope');
      if(scopeRow){
        scopeRow.querySelectorAll('button').forEach(b=>{
          const isLocal = b.dataset.institutionScope==='local';
          b.setAttribute('aria-pressed', String(isLocal));
        });
      }
    }
    closeAll(); rows[n].toggle.focus();
  };
  const localOptions = n => {
    if (fields[n].tagName === 'SELECT') return [...fields[n].options].map(o => ({value:o.value,label:o.textContent}));
    let values = [];
    if (n === 'country') values = Object.keys(geo);
    if (n === 'state') values = Object.keys(geo[fields.country.value] || {});
    if (n === 'area') values = (geo[fields.country.value] || {})[fields.state.value] || [];
    return sort(values).map(value => ({value,label:value}));
  };
  function renderLocal(n) {
    const r = rows[n], q = r.search.value.trim().toLocaleLowerCase();
    const all = localOptions(n).filter(o => o.label.toLocaleLowerCase().includes(q));
    r.list.replaceChildren();
    all.slice(0,250).forEach(o => {
      const b = button(o.label, () => choose(n,o.value,o.label),'profile-pick-option');
      b.setAttribute('aria-pressed', String(fields[n].value === o.value)); r.list.append(b);
    });
    r.status.textContent = all.length > 250 ? `${all.length} matches. Type to narrow the full list.` : all.length ? '' : 'No matches. Choose the country and state first when selecting an area.';
  }
  async function institutions(append=false) {
    const r = rows.nearest_institution;
    if (!append) { controller?.abort(); controller = new AbortController(); requestSerial++; r.offset=0; r.list.replaceChildren(); }
    const serial = requestSerial;
    r.more.hidden = true;
    r.status.textContent='Loading institutions…';
    try {
      const url = new URL(form.dataset.institutionsUrl, location.origin === 'null' ? 'https://preview.invalid/' : location.href);
      const hasGeo = fields.country?.value || fields.state?.value || fields.area?.value;
      if(r.localScope || hasGeo) ['country','state','area'].forEach(k=>{if(fields[k]?.value)url.searchParams.set(k,fields[k].value);});
      url.searchParams.set('q',r.search.value);url.searchParams.set('offset',r.offset||0);
      const response=await fetch(url,{signal:controller.signal,headers:{Accept:'application/json'}});
      if(!response.ok)throw Error('catalogue');
      const payload=await response.json();
      if (serial !== requestSerial) return;
      const items = Array.isArray(payload) ? payload : payload.results || [];
      items.forEach(o => {
        const abbreviation=(o.abbreviations || [])[0] || '';
        const label = [o.name,abbreviation ? `(${abbreviation})` : '',`· ${o.area}, ${o.state}, ${o.country}`].filter(Boolean).join(' ');
        r.list.append(button(label, () => {
          geo[o.country] ||= {};geo[o.country][o.state] ||= [];
          if(!geo[o.country][o.state].includes(o.area))geo[o.country][o.state].push(o.area);
          ['country','state','area'].forEach(key=>assign(key,o[key]||''));
          choose('nearest_institution',o.name,label);
          const source=form.elements.namedItem('nearest_institution_source_id');
          if(source)source.value=o.source_id||o.id;
          const field=form.elements.namedItem('nearest_institution_abbreviation');
          if(field)field.value=abbreviation;
        },'profile-pick-option'));
      });
      r.offset += items.length;
      r.more.hidden = !items.length || r.offset >= (payload.count || items.length);
      r.status.textContent = r.offset ? `${r.offset} of ${payload.count} institutions shown.` : 'No institutions match. Try All institutions or another search.';
    } catch (error) { if (error.name !== 'AbortError' && serial === requestSerial) r.status.textContent='Unable to load institutions. Check your connection and retry.'; }
  }
  names.forEach(n => {
    const f=fields[n]; if (!f) return;
    const host=make('div','profile-pick'); host.dataset.profilePick=n;
    const originalId=f.id; f.id=originalId+'-value';
    const value=make('span','profile-pick-value');
    const toggle=button('',() => {
      const open=toggle.getAttribute('aria-expanded') !== 'true'; closeAll();
      if (!open) return;
      panel.hidden=false;toggle.setAttribute('aria-expanded','true');
      if (n==='date_of_birth') renderCalendar();
      else {
        search.value='';
        if(n==='nearest_institution'){
          const hasGeo = fields.country?.value || fields.state?.value || fields.area?.value;
          if(hasGeo){
            rows[n].localScope = true;
            const scopeEl = panel.querySelector('.institution-scope');
            if(scopeEl) scopeEl.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed', String(b.dataset.institutionScope==='local')));
          }
          institutions();
        } else renderLocal(n);
        search.focus();
      }
    },'profile-pick-toggle');
    value.id=originalId+'-display';
    toggle.id=originalId; toggle.setAttribute('aria-describedby',value.id); toggle.append(value,make('span','profile-pick-chevron','⌄'));
    toggle.setAttribute('aria-expanded','false');toggle.setAttribute('aria-controls',originalId+'-panel');
    const panel=make('div','profile-pick-panel'); panel.id=originalId+'-panel'; panel.hidden=true;
    const search=make('input','profile-pick-search');search.type='search';search.placeholder='Search…';search.setAttribute('aria-label',`Search ${form.querySelector(`label[for="${originalId}"]`)?.textContent || n}`);
    const list=make('div','profile-pick-options');
    const status=make('p','profile-pick-status');status.setAttribute('role','status');status.setAttribute('aria-live','polite');
    const more=button('Load more institutions',()=>institutions(true),'profile-pick-more');more.hidden=true;
    if(n==='nearest_institution'){
      const scope=make('div','institution-scope');
      ['all','local'].forEach(mode=>{
        const b=button(mode==='all'?'All institutions':'Use selected location',()=>{
          rows[n].localScope=mode==='local';
          scope.querySelectorAll('button').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));
          institutions();
        },'institution-scope-button');
        b.dataset.institutionScope=mode;b.setAttribute('aria-pressed',String(mode==='all'));scope.append(b);
      });
      panel.append(scope,make('p','institution-picker-help','Search worldwide. Location is optional; choosing an institution fills its country, state and area.'));
    }
    if(n!=='date_of_birth')panel.append(search,list,status,more);
    panel.append(button(n==='date_of_birth' ? 'Clear date' : 'Clear selection',()=>choose(n,'',''),'profile-pick-clear'));
    host.append(toggle,panel);f.after(host);
    f.hidden=true;f.required=false; if(f.tagName==='INPUT') f.type='hidden';
    rows[n]={host,toggle,panel,value,search,list,status,more};refresh(n);
    f.addEventListener('change',()=>refresh(n));
    search.addEventListener('input',()=> {if(n==='nearest_institution'){controller?.abort();requestSerial++;clearTimeout(debounce);debounce=setTimeout(()=>institutions(),180);}else renderLocal(n);});
    host.addEventListener('keydown', e=>{
      if(e.key==='Escape'){closeAll();toggle.focus();}
      if(e.key==='ArrowDown' || e.key==='ArrowUp'){
        const buttons=[...panel.querySelectorAll('button:not([hidden]):not([disabled])')];
        if(panel.hidden || !buttons.length)return;
        e.preventDefault();const i=buttons.indexOf(document.activeElement),step=e.key==='ArrowDown'?1:-1;
        buttons[(i+step+buttons.length)%buttons.length].focus();
      }
    });
  });
  const todayString=form.dataset.today || new Date().toISOString().slice(0,10);
  const today=new Date(todayString+'T12:00:00');
  let cursor=new Date((fields.date_of_birth?.value || todayString)+'T12:00:00'),mode='days';
  if(Number.isNaN(cursor.getTime())) cursor=new Date(today);
  const iso=(y,m,d)=>`${y}-${String(m+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
  function renderCalendar(){
    const r=rows.date_of_birth,p=r.panel, restoreFocus=p.contains(document.activeElement);
    p.querySelector('.profile-calendar')?.remove();
    const cal=make('div','profile-calendar');cal.setAttribute('aria-label','Choose date of birth');
    const y=cursor.getFullYear(),m=cursor.getMonth();
    const head=make('div','profile-calendar-head');
    const move=step=>{if(mode==='years')cursor.setFullYear(y+step*12, m, 1);else if(mode==='months')cursor.setFullYear(y+step,m,1);else cursor.setFullYear(y,m+step,1);renderCalendar();};
    const prev=button('‹',()=>move(-1));prev.setAttribute('aria-label','Previous '+(mode==='years'?'12 years':mode==='months'?'year':'month'));
    const next=button('›',()=>move(1));next.setAttribute('aria-label','Next '+(mode==='years'?'12 years':mode==='months'?'year':'month'));
    head.append(prev,button(new Intl.DateTimeFormat('en',{month:'long'}).format(cursor),()=>{mode='months';renderCalendar();},'profile-calendar-month'),button(String(y),()=>{mode='years';renderCalendar();},'profile-calendar-year'),next);
    cal.append(head);
    const grid=make('div',mode==='days'?'profile-calendar-grid':'profile-calendar-choices');
    if(mode==='years'){
      for(let yr=y-11;yr<=y;yr++){
        const b=button(String(yr),()=>{cursor.setFullYear(yr, m,1);mode='months';renderCalendar();}); b.disabled=yr>today.getFullYear() || yr<1;grid.append(b);
      }
    }else if(mode==='months'){
      for(let mo=0;mo<12;mo++){
        const b=button(new Intl.DateTimeFormat('en',{month:'short'}).format(new Date(y,mo,1)),()=>{cursor.setFullYear(y,mo,1);mode='days';renderCalendar();});
        b.disabled=iso(y,mo,1)>todayString;grid.append(b);
      }
    }else{
      ['Su','Mo','Tu','We','Th','Fr','Sa'].forEach(day=>grid.append(make('span','profile-calendar-weekday',day)));
      for(let i=0;i<new Date(y,m,1).getDay();i++)grid.append(make('span'));
      for(let day=1;day<=new Date(y,m+1,0).getDate();day++){
        const value=iso(y,m,day),b=button(String(day),()=>choose('date_of_birth',value));
        b.setAttribute('aria-label',value);b.disabled=value>todayString;
        b.setAttribute('aria-pressed',String(value===fields.date_of_birth.value));
        if(value===todayString)b.setAttribute('aria-current','date');grid.append(b);
      }
    }
    cal.append(grid,make('p','profile-pick-status','Choose a month or year above to jump to your birth date.'));p.prepend(cal);
    if(restoreFocus) cal.querySelector('button[aria-pressed="true"]:not(:disabled),button:not(:disabled)')?.focus();
  }
  form.querySelectorAll('.artisan-profile-links-editor').forEach(details=>{
    const summary=details.querySelector('summary'), content=details.querySelector('.artisan-profile-links-fields');
    let busy=false;
    summary.addEventListener('click',event=>{
      event.preventDefault(); if(busy)return;
      const opening=!details.open;
      if(matchMedia('(prefers-reduced-motion: reduce)').matches || !content.animate){details.open=opening;return;}
      busy=true; details.open=true; content.style.overflow='hidden';
      const animation=content.animate([{height:opening?'0px':`${content.scrollHeight}px`,opacity:opening?0:1},{height:opening?`${content.scrollHeight}px`:'0px',opacity:opening?1:0}],{duration:200,easing:'ease',fill:'both'});
      animation.finished.finally(()=>{details.open=opening;animation.cancel();content.style.overflow='';busy=false;});
    });
  });
  document.addEventListener('click',e=>{if(!e.composedPath().some(node=>node.classList?.contains('profile-pick')))closeAll();});
  form.addEventListener('reset',()=>setTimeout(()=>{names.forEach(refresh);closeAll();},0));
})();
