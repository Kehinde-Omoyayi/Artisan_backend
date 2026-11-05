(function(){
  const apiErrors=window.ArtisanApiErrors;
  const formatApiError=(payload,status,options)=>apiErrors?apiErrors.format(payload,status,options):"The request could not be completed. Please try again.";
  const readApiPayload=(response)=>apiErrors?apiErrors.readResponse(response):response.json().catch(()=>null);
  const stream=document.getElementById("conversation-stream");
  if(!stream)return;
  const id=stream.dataset.conversationId;
  const form=document.getElementById("conversation-message-form");
  const input=form?.querySelector("textarea[name=body]");
  const fileInput=document.getElementById("conversation-attachments");
  const preview=document.getElementById("attachment-preview");
  const statusNode=document.getElementById("chat-status");
  const unreadNode=document.getElementById("chat-unread-count");
  const scrollArea=document.getElementById("conversation-scroll-area")||stream;
  const csrf=form?.querySelector("input[name=csrfmiddlewaretoken]")?.value||document.querySelector('meta[name="csrf-token"]')?.content||"";
  const viewerId=Number(stream.dataset.viewerId||0);
  let delay=4000,timer=0,lastSignature="",firstPayload=true,readPending=false,sendingMessage=false;
  let selectedFiles=Array.from(fileInput?.files||[]);
  const seenMessageIds=new Set();
  const MEDIA_LIMIT_MB=30;
  const MEDIA_LIMIT_BYTES=MEDIA_LIMIT_MB*1024*1024;
  const VOICE_MAX_SECONDS=300;
  const MB=(bytes)=>`${Math.ceil(bytes/1024/1024)} MB`;
  let lastRenderedId=null,lastReadMessageId=null,lastOfferSignature=null;

  const setStatus=(text)=>{if(statusNode)statusNode.textContent=text;};
  const safeUrl=(value)=>{
    try{
      const parsed=new URL(value,window.location.origin);
      return parsed.protocol==="https:"||parsed.protocol==="http:"?parsed.href:"";
    }catch(_){return "";}
  };
  const localDateTimeValue=(value)=>{
    const date=new Date(value);
    if(Number.isNaN(date.getTime()))return "";
    const pad=(number)=>String(number).padStart(2,"0");
    return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  };
  document.querySelectorAll('input[type="datetime-local"][data-utc-value]').forEach(field=>{
    const value=localDateTimeValue(field.dataset.utcValue||"");
    if(value)field.value=value;
  });
  document.querySelectorAll(".workflow-local-time[data-utc-time]").forEach(node=>{
    const date=new Date(node.dataset.utcTime||"");
    if(Number.isNaN(date.getTime()))return;
    node.textContent=date.toLocaleString(undefined,{weekday:"short",day:"numeric",month:"short",year:"numeric",hour:"numeric",minute:"2-digit"});
  });

  function makeAttachment(attachment){
    const url=safeUrl(attachment.url);
    if(!url){
      if(String(attachment.content_type||'').match(/^(audio|video)\//)){
        const notice=document.createElement('span');notice.textContent='Recording unavailable (scan pending or access closed under the media policy).';return notice;
      }
      return null;
    }
    if(String(attachment.content_type||'').match(/^(audio|video)\//)){
      const player=document.createElement(attachment.content_type.startsWith('audio/')?'audio':'video');
      player.controls=true;player.preload='none';player.src=url;player.playsInline=true;return player;
    }
    const contentType=String(attachment.content_type||"file");
    if(contentType.toLowerCase().startsWith("image/")){
      const link=document.createElement("a");
      link.className="chat-attachment-image";
      link.href=url;
      link.target="_blank";
      link.rel="noopener";
      const image=document.createElement("img");
      image.src=url;
      image.alt="Shared photo; open full size";
      image.loading="lazy";
      link.appendChild(image);
      return link;
    }
    const link=document.createElement("a");
    link.className="chat-attachment-file";
    link.href=url;
    link.target="_blank";
    link.rel="noopener";
    link.textContent=`Attachment · ${contentType}`;
    return link;
  }

  function render(messages){
    const keepBottom=scrollArea.scrollHeight-scrollArea.scrollTop-scrollArea.clientHeight<120;
    const fragment=document.createDocumentFragment();
    messages.forEach(message=>{
      const article=document.createElement("article");
      const isMine=Number(message.sender_id)===viewerId;
      article.className="chat-message "+(message.message_type==="SYSTEM"?"system":isMine?"mine":"theirs");
      if(!isMine&&message.message_type!=="SYSTEM"){
        const avatar=document.createElement("span");
        avatar.className="chat-message-avatar";
        avatar.setAttribute("aria-hidden","true");
        const photo=safeUrl(stream.dataset.counterpartyAvatar||"");
        const fallback=document.createElement("span");
        fallback.dataset.profilePhotoFallback="";
        fallback.textContent=String(message.sender_name||"P").trim().slice(0,1).toUpperCase();
        fallback.hidden=Boolean(photo);
        avatar.appendChild(fallback);
        if(photo){
          const image=document.createElement("img");
          image.src=photo;
          image.alt="";
          image.loading="lazy";
          image.referrerPolicy="no-referrer";
          image.dataset.profilePhotoImage="";
          avatar.appendChild(image);
        }
        article.appendChild(avatar);
      }
      const bubble=document.createElement("div");
      bubble.className="chat-bubble";
      const who=document.createElement("strong");
      who.textContent=message.message_type==="SYSTEM"?"Artisan Market":isMine?"You":(message.sender_name||"Participant");
      bubble.appendChild(who);
      if(message.body){
        const raw=String(message.body);
        const body=document.createElement("p");
        body.textContent=message.message_type==="SYSTEM"?raw.split("Next steps:")[0].trim():raw;
        bubble.appendChild(body);
      }
      (message.attachments||[]).forEach(attachment=>{
        const rendered=makeAttachment(attachment);
        if(rendered)bubble.appendChild(rendered);
      });
      const time=document.createElement("time");
      time.dateTime=String(message.created_at||"");
      // Chat bubbles show the clock only, never a date: "12:34 pm".
      const parsedDate=new Date(message.created_at);
      time.textContent=Number.isNaN(parsedDate.getTime())
        ? String(message.created_at||"")
        : parsedDate.toLocaleTimeString([], {hour:"numeric", minute:"2-digit"}).toLowerCase().replace(/\s+/g," ");
      bubble.appendChild(time);
      article.appendChild(bubble);
      fragment.appendChild(article);
    });
    if(!messages.length){
      const empty=document.createElement("div");
      empty.className="empty chat-empty-state";
      empty.textContent="No messages yet. Start the conversation naturally — you can write a note or attach a photo.";
      fragment.appendChild(empty);
    }
    stream.replaceChildren(fragment);
    if(firstPayload||keepBottom)scrollArea.scrollTop=scrollArea.scrollHeight;
    firstPayload=false;
  }

  function markRead(){
    if(readPending||document.hidden||!lastRenderedId||lastRenderedId===lastReadMessageId)return;
    if(scrollArea.scrollHeight-scrollArea.scrollTop-scrollArea.clientHeight>80)return;
    const marker=lastRenderedId;
    readPending=true;
    const data=new FormData();
    data.set("csrfmiddlewaretoken",csrf);
    data.set("action","read");
    data.set("last_message_id",marker);
    fetch(window.location.href,{method:"POST",body:data,credentials:"same-origin",headers:{"X-CSRFToken":csrf,"X-Requested-With":"XMLHttpRequest"}})
      .then(response=>{
        if(!response.ok)throw new Error("read_failed");
        lastReadMessageId=marker;
        stream.dataset.unreadCount="0";
        if(unreadNode)unreadNode.textContent="0 unread";
      })
      .catch(()=>{})
      .finally(()=>{readPending=false;});
  }

  scrollArea.addEventListener("scroll",()=>markRead(),{passive:true});

  /* The itemized offer is rendered by the server, so the card that greets the
     page and the card that a live quote produces are the same markup. When an
     offer event lands we re-read this page and swap only that node: the room
     stays where it is, a half-typed message survives, and there is no second
     copy of the card to drift out of step. */
  async function syncOfferCard(){
    try{
      const response=await fetch(window.location.pathname,{headers:{Accept:"text/html"},credentials:"same-origin",cache:"no-store"});
      if(!response.ok)return;
      const page=new DOMParser().parseFromString(await response.text(),"text/html");
      const fresh=page.getElementById("offer-card");
      const current=document.getElementById("offer-card");
      if(!fresh){current?.remove();return;}
      // Same offer, same version, same status: nothing to redraw.
      if(current&&fresh.dataset.offerEvent&&current.dataset.offerEvent===fresh.dataset.offerEvent)return;
      const node=document.importNode(fresh,true);
      if(current){current.replaceWith(node);}
      else{
        stream.querySelector(".chat-empty-state")?.remove();
        stream.after(node);
      }
      node.classList.add("is-refreshed");
      window.setTimeout(()=>node.classList.remove("is-refreshed"),4000);
      if(scrollArea.scrollHeight-scrollArea.scrollTop-scrollArea.clientHeight<160)scrollArea.scrollTop=scrollArea.scrollHeight;
    }catch(_){/* the next poll tries again */}
  }

  async function refresh(){
    try{
      const response=await fetch(`/discover/conversations/${id}/messages/?limit=60`,{headers:{Accept:"application/json"},credentials:"same-origin",cache:"no-store"});
      if(response.status===401||response.status===403){window.location.assign("/auth/login/?next="+encodeURIComponent(window.location.pathname));return;}
      const payload=await readApiPayload(response);
      if(!response.ok){setStatus(formatApiError(payload,response.status));return;}
      if(!payload){setStatus(formatApiError(null,response.status));return;}
      let messages=Array.isArray(payload.results)?payload.results.slice().reverse():Array.isArray(payload)?payload.slice().reverse():[];
      // Any turn of the offer (sent, countered, accepted, declined, expired,
      // revision asked) means the card is stale: re-sync it in place.
      const offerSignature=messages
        .filter(message=>typeof message.event_code==="string"&&(message.event_code.startsWith("OFFER")||message.event_code==="QUOTE_REVISION_REQUESTED"))
        .map(message=>message.id)
        .join("|");
      const offersChanged=lastOfferSignature!==null&&offerSignature!==lastOfferSignature;
      lastOfferSignature=offerSignature;
      // "Conversation started" is guidance and "Offer created/countered" is the
      // card itself. Both stay in the database for audit but never reappear as
      // a bare text bubble in the stream.
      messages=messages.filter(message=>message.event_code!=="CONVERSATION_STARTED"&&message.event_code!=="OFFER_CREATED"&&message.event_code!=="OFFER_COUNTERED");
      const wasFirstPayload=firstPayload;
      const newIncoming=!wasFirstPayload&&messages.some(message=>message.id&&!seenMessageIds.has(String(message.id))&&Number(message.sender_id)!==viewerId&&message.message_type!=="SYSTEM");
      messages.forEach(message=>{if(message.id)seenMessageIds.add(String(message.id));});
      const signature=JSON.stringify(messages.map(message=>[message.id,message.body,(message.attachments||[]).map(attachment=>[attachment.id,attachment.url,attachment.scan_status])]));
      if(signature!==lastSignature){render(messages);lastSignature=signature;}
      lastRenderedId=messages.length?String(messages[messages.length-1].id):null;
      markRead();
      if(offersChanged)syncOfferCard();
    }catch(_){setStatus(formatApiError(null,null,{networkError:true}));}
  }

  function clearAttachmentPreview(){
    if(!preview)return;
    preview.replaceChildren();
    preview.hidden=true;
  }

  function syncFileInput(){
    if(!fileInput||typeof DataTransfer==="undefined")return;
    try{
      const transfer=new DataTransfer();
      selectedFiles.forEach(file=>transfer.items.add(file));
      fileInput.files=transfer.files;
    }catch(_){
      // selectedFiles remains the source used to build the secure submission payload.
    }
  }

  function showAttachmentPreview(){
    if(!preview||!fileInput)return;
    clearAttachmentPreview();
    const files=selectedFiles.slice();
    if(!files.length)return;
    preview.hidden=false;
    const heading=document.createElement("p");
    heading.className="attachment-preview-heading";
    heading.textContent=`${files.length} attachment${files.length===1?"":"s"} ready to send`;
    preview.appendChild(heading);
    files.forEach((file,index)=>{
      const item=document.createElement("div");
      item.className="attachment-preview-item";
      if(file.type&&file.type.toLowerCase().startsWith("image/")&&window.FileReader){
        const image=document.createElement("img");
        image.alt=`Preview of ${file.name}`;
        image.loading="lazy";
        item.appendChild(image);
        const reader=new FileReader();
        reader.addEventListener("load",()=>{if(typeof reader.result==="string")image.src=reader.result;},{once:true});
        reader.readAsDataURL(file);
      }else{
        const icon=document.createElement("span");
        icon.className="attachment-file-mark";
        icon.setAttribute("aria-hidden","true");
        icon.textContent="↗";
        item.appendChild(icon);
      }
      const name=document.createElement("span");
      name.className="attachment-preview-name";
      name.textContent=file.name;
      item.appendChild(name);
      const remove=document.createElement("button");
      remove.type="button";
      remove.className="attachment-preview-remove";
      remove.dataset.attachmentIndex=String(index);
      remove.setAttribute("aria-label",`Remove ${file.name}`);
      remove.textContent="Remove";
      item.appendChild(remove);
      preview.appendChild(item);
    });
  }

  fileInput?.addEventListener("change",()=>{
    selectedFiles=Array.from(fileInput.files||[]);
    if(voiceFile&&!selectedFiles.includes(voiceFile))clearVoice();
    showAttachmentPreview();
    syncComposerButtons();
  });
  preview?.addEventListener("click",event=>{
    const button=event.target.closest("[data-attachment-index]");
    if(!button)return;
    const index=Number(button.dataset.attachmentIndex);
    if(!Number.isInteger(index)||index<0||index>=selectedFiles.length)return;
    const removed=selectedFiles.splice(index,1)[0];
    if(removed===voiceFile)clearVoice();
    syncFileInput();
    showAttachmentPreview();
    setStatus(selectedFiles.length?"Attachment removed. The other files and message are unchanged.":"Attachment removed. Your message is unchanged.");
  });

  // WhatsApp-style composer: paperclip (photo / video / document), camera right
  // beside the mic, and the mic turning into Send as soon as there is anything
  // to send. No option sheet to read through.
  const sendButton=form?.querySelector('.chat-send-button');
  const attachButton=form?.querySelector('[data-composer-attach]');
  const cameraButton=form?.querySelector('[data-composer-camera]');
  const cameraInput=form?.querySelector('#conversation-camera');
  // One place decides what the right-hand button is: mic, stop, or Send.
  const syncComposerButtons=()=>{
    if(form&&typeof voiceState==='function')voiceState(form.dataset.voiceState||'idle');
  };
  // Files from the paperclip and from the camera share one queue.
  const queueFiles=incoming=>{
    const added=Array.from(incoming||[]);
    if(!added.length)return;
    selectedFiles=selectedFiles.concat(added);
    syncFileInput();
    showAttachmentPreview();
    syncComposerButtons();
  };
  attachButton?.addEventListener('click',()=>{
    if(!fileInput)return;
    fileInput.setAttribute('accept','image/*,video/*,audio/*,application/pdf');
    fileInput.click();
  });
  cameraButton?.addEventListener('click',()=>{
    if(cameraInput)cameraInput.click();
    else if(fileInput){fileInput.setAttribute('accept','image/*,video/*');fileInput.click();}
  });
  cameraInput?.addEventListener('change',()=>{
    queueFiles(cameraInput.files);
    cameraInput.value='';
  });
  input?.addEventListener('input',syncComposerButtons);

  // Voice notes. Tapping the mic starts recording visibly: the button becomes a
  // stop square, a live timer appears under the box, and Send stays hidden until
  // the recording has been stopped and staged.
  let voiceRecorder=null,voiceStream=null,voiceTimer=null,voiceBlobUrl=null,voiceFile=null,voiceRequesting=false,voiceLeaving=false;
  const voiceTools=form?.querySelector('[data-voice-tools]');
  const recordButton=form?.querySelector('[data-voice-record]');
  const discardButton=voiceTools?.querySelector('[data-voice-discard]');
  const voiceStatus=voiceTools?.querySelector('[data-voice-status]');
  const voiceDot=voiceTools?.querySelector('[data-voice-dot]');
  const voicePreview=voiceTools?.querySelector('[data-voice-preview]');
  const composerReady=()=>Boolean((input?.value||'').trim()||selectedFiles.length);
  function voiceState(state){
    if(!form)return;
    form.dataset.voiceState=state;
    if(recordButton){
      recordButton.hidden=!(state==='recording'||state==='starting'||!composerReady());
      recordButton.setAttribute('aria-pressed',String(state==='recording'));
      recordButton.setAttribute('aria-label',state==='recording'?'Stop recording':'Record an audio message');
      recordButton.disabled=state==='starting';
    }
    if(sendButton)sendButton.hidden=state==='recording'||state==='starting'||!composerReady();
    if(voiceTools)voiceTools.hidden=state==='idle';
    if(voiceDot)voiceDot.hidden=state!=='recording';
    if(discardButton)discardButton.hidden=state!=='ready';
    if(voicePreview)voicePreview.hidden=state!=='ready';
  }
  const voiceSay=text=>{if(voiceStatus)voiceStatus.textContent=text;};
  function releaseMicrophone(){clearInterval(voiceTimer);voiceStream?.getTracks().forEach(t=>t.stop());voiceStream=null;}
  function clearVoice(){
    if(voiceRecorder?.state==='recording')return;
    if(voiceBlobUrl)URL.revokeObjectURL(voiceBlobUrl);voiceBlobUrl=null;
    if(voiceFile)selectedFiles=selectedFiles.filter(f=>f!==voiceFile);voiceFile=null;
    if(voicePreview){voicePreview.pause();voicePreview.removeAttribute('src');}
    voiceSay('');
    voiceState('idle');
    syncFileInput();showAttachmentPreview();
  }
  discardButton?.addEventListener('click',()=>{clearVoice();syncComposerButtons();setStatus('Recording discarded.');});
  recordButton?.addEventListener('click',async()=>{
    if(sendingMessage)return;
    if(voiceRecorder?.state==='recording'){voiceRecorder.stop();return;}
    if(!window.isSecureContext||!navigator.mediaDevices?.getUserMedia||!window.MediaRecorder){
      voiceState('idle');
      if(voiceTools)voiceTools.hidden=false;
      voiceSay('This browser cannot record over this connection. Open the site on https, or attach an audio file with the paperclip.');
      setStatus('Recording needs https (or localhost). You can still attach an audio file.');
      return;
    }
    clearVoice();
    voiceState('starting');
    voiceSay('Asking for microphone permission…');
    voiceRequesting=true;
    try{
      voiceStream=await navigator.mediaDevices.getUserMedia({audio:true});
      voiceRequesting=false;
      if(voiceLeaving||sendingMessage){releaseMicrophone();voiceState('idle');return;}
      const mime=['audio/webm;codecs=opus','audio/mp4','audio/ogg;codecs=opus'].find(t=>window.MediaRecorder.isTypeSupported(t));
      if(!mime)throw Error('unsupported');
      const chunks=[];let bytes=0;const started=Date.now();
      voiceRecorder=new MediaRecorder(voiceStream,{mimeType:mime});
      voiceRecorder.ondataavailable=e=>{if(e.data.size){chunks.push(e.data);bytes+=e.data.size;if(bytes>MEDIA_LIMIT_BYTES&&voiceRecorder.state==='recording')voiceRecorder.stop();}};
      voiceRecorder.onstop=()=>{
        releaseMicrophone();
        if(bytes>MEDIA_LIMIT_BYTES){
          voiceState('idle');
          if(voiceTools)voiceTools.hidden=false;
          voiceSay(`That recording passed ${MEDIA_LIMIT_MB} MB. Record a shorter one.`);
          setStatus(`Recording exceeded ${MEDIA_LIMIT_MB} MB.`);
          return;
        }
        const seconds=Math.max(1,Math.round((Date.now()-started)/1000));
        const ext=mime.includes('mp4')?'m4a':mime.includes('ogg')?'ogg':'webm';
        voiceFile=new File(chunks,'voice-note.'+ext,{type:mime});
        selectedFiles.push(voiceFile);
        voiceBlobUrl=URL.createObjectURL(voiceFile);
        if(voicePreview)voicePreview.src=voiceBlobUrl;
        syncFileInput();showAttachmentPreview();
        voiceState('ready');
        voiceSay(`Voice note ready · ${seconds}s. Tap Send to send it, or discard.`);
      };
      voiceRecorder.onerror=()=>{
        releaseMicrophone();voiceRequesting=false;voiceState('idle');
        if(voiceTools)voiceTools.hidden=false;
        voiceSay('Recording failed. Try again, or attach an audio file.');
      };
      voiceRecorder.start(250);
      voiceState('recording');
      const clock=seconds=>`${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,'0')}`;
      voiceTimer=setInterval(()=>{
        const seconds=Math.floor((Date.now()-started)/1000);
        voiceSay(`Recording ${clock(seconds)} · tap the button to stop`);
        if(seconds>=VOICE_MAX_SECONDS&&voiceRecorder.state==='recording')voiceRecorder.stop();
      },250);
    }catch(error){
      voiceRequesting=false;releaseMicrophone();voiceState('idle');
      if(voiceTools)voiceTools.hidden=false;
      const denied=error?.name==='NotAllowedError'||error?.name==='SecurityError';
      voiceSay(denied?'Microphone permission was blocked. Allow it in the browser, or attach an audio file.'
                     :'No microphone was available. Attach an audio file instead.');
      setStatus('Recording unavailable.');
    }
  });
  document.addEventListener('visibilitychange',()=>{if(document.hidden&&voiceRecorder?.state==='recording')voiceRecorder.stop();});
  window.addEventListener('pagehide',()=>{voiceLeaving=true;if(voiceRecorder?.state==='recording'){voiceRecorder.onstop=null;voiceRecorder.stop();}releaseMicrophone();if(voiceBlobUrl)URL.revokeObjectURL(voiceBlobUrl);});

  form?.addEventListener("submit",async event=>{
    event.preventDefault();
    if(sendingMessage)return;
    if(voiceRequesting||voiceRecorder?.state==="recording"){setStatus("Stop the recording before sending.");return;}
    const body=input?.value.trim()||"";
    const files=selectedFiles.slice();
    if(!body&&!files.length){setStatus("Write a message or attach a photo or file first.");input?.focus();return;}
    if(files.length>5){setStatus("You can attach up to five files to one message.");fileInput?.focus();return;}
    const oversized=files.find(file=>file.size>MEDIA_LIMIT_BYTES);
    if(oversized){setStatus(`${oversized.name} is ${MB(oversized.size)}; the limit is ${MEDIA_LIMIT_MB} MB per file.`);fileInput?.focus();return;}
    const data=new FormData(form);
    data.delete("attachments");
    files.forEach(file=>data.append("attachments",file,file.name));
    const sendButton=form.querySelector('button[type="submit"]');
    const controls=[input,fileInput,sendButton].filter(Boolean);
    const previousDisabled=controls.map(control=>control.disabled);
    sendingMessage=true;
    form.dataset.sending="true";
    controls.forEach(control=>{control.disabled=true;});
    setStatus("Sending securely…");
    try{
      const response=await fetch(window.location.href,{method:"POST",body:data,credentials:"same-origin",headers:{Accept:"text/html,application/json","X-Requested-With":"XMLHttpRequest","X-CSRFToken":csrf}});
      if(!response.ok){setStatus(formatApiError(await readApiPayload(response),response.status));return;}
      const html=await response.text();
      const parsed=new DOMParser().parseFromString(html,"text/html");
      const errorPanel=parsed.querySelector(".response-panel--error");
      if(errorPanel){
        const errorText=errorPanel.querySelector("p")?.textContent.trim()||"The message could not be sent.";
        setStatus(errorText);
        return;
      }
      if(response.url&&new URL(response.url).pathname!==window.location.pathname){window.location.assign(response.url);return;}
      input.value="";
      if(fileInput)fileInput.value="";
      selectedFiles=[];
      clearVoice();
      clearAttachmentPreview();
      setStatus("Sent securely.");
      await refresh();
    }catch(_){setStatus(formatApiError(null,null,{networkError:true}));}
    finally{
      sendingMessage=false;
      delete form.dataset.sending;
      controls.forEach((control,index)=>{control.disabled=previousDisabled[index];});
    }
  });

  async function loop(){if(!document.hidden)await refresh();delay=Math.min(delay*1.2,12000);timer=window.setTimeout(loop,delay);}
  window.addEventListener("beforeunload",()=>window.clearTimeout(timer),{once:true});
  fileInput&&showAttachmentPreview();
  syncComposerButtons();
  refresh();
  timer=window.setTimeout(loop,delay);

  document.querySelectorAll("form").forEach(moneyForm=>{
    const action=moneyForm.querySelector('input[name="action"]')?.value;
    if(action==="offer"||action==="counter"){
      moneyForm.addEventListener("submit",()=>{
        moneyForm.querySelectorAll('input[type="datetime-local"][name]').forEach(field=>{
          if(!field.value)return;
          const date=new Date(field.value);
          if(Number.isNaN(date.getTime()))return;
          const hidden=document.createElement("input");
          hidden.type="hidden";
          hidden.name=field.name;
          hidden.value=date.toISOString();
          moneyForm.appendChild(hidden);
          field.disabled=true;
        });
      });
    }
    const inputs=moneyForm.querySelectorAll("[data-money-input]");
    const total=moneyForm.querySelector("[data-money-total]");
    if(!inputs.length||!total)return;
    moneyForm.addEventListener("input",event=>{
      if(event.target===total){total.dataset.manual="true";return;}
      if(!event.target.matches("[data-money-input]")||total.dataset.manual==="true")return;
      const sum=Array.from(inputs).reduce((value,field)=>{
        const amount=Number(String(field.value||"").replace(/,/g,""));
        return value+(Number.isFinite(amount)?amount:0);
      },0);
      total.value=sum?sum.toFixed(2):"";
    });
  });
  const tools=document.getElementById("chat-service-tools");
  document.addEventListener("keydown",event=>{if(event.key==="Escape"&&tools?.open)tools.open=false;});
})();

/* Escrow payment lives in the chat: while the provider link is being prepared,
   poll our own status route and swap the strip in place — no page hop. */
(() => {
  const strip = document.querySelector("[data-payment-watch]");
  if (!strip) return;
  const endpoint = strip.getAttribute("data-payment-watch");
  if (!endpoint) return;
  let attempts = 0;
  let waitedWithoutLink = false;
  const MAX_ATTEMPTS = 40;
  const tick = async () => {
    if (attempts >= MAX_ATTEMPTS) return;
    attempts += 1;
    try {
      const response = await fetch(endpoint, {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      if (response.ok) {
        const data = await response.json();
        const status = String(data.status || "").toUpperCase();
        const checkout = String(data.authorization_url || "");
        const button = strip.querySelector("[data-payment-pending-form] button");
        if (!checkout) {
          waitedWithoutLink = true;
        } else {
          const form = strip.querySelector("[data-payment-pending-form]");
          if (form && !strip.querySelector("[data-payment-checkout]")) {
            const link = document.createElement("a");
            link.className = "button button-primary button-small";
            link.href = checkout;
            link.setAttribute("data-artisan-action", "");
            link.setAttribute("data-payment-checkout", "");
            link.textContent = button ? button.textContent.trim() : "Continue secure payment";
            form.replaceWith(link);
          }
          // The payer tapped "Pay" and the link only arrived now: take them to
          // the provider instead of asking for a second tap. Only when this page
          // actually waited for it — a chat opened with a link already in hand
          // must not navigate on its own, or paying and coming back would bounce
          // the payer straight back to Flutterwave.
          if (waitedWithoutLink) {
            window.location.assign(checkout);
            return;
          }
        }
        if (status && status !== "PAYMENT_PENDING") {
          window.location.reload();
          return;
        }
      }
    } catch (error) {
      /* offline: try again on the next tick */
    }
    window.setTimeout(tick, 9000);
  };
  window.setTimeout(tick, 2500);
})();

/* Bank transfer is paid inside this page: copy the amount or the account
   number, watch the clock drain, and notice quietly when the money lands. */
(() => {
  const card = document.querySelector("[data-transfer-card]");
  if (!card) return;

  const chip = card.querySelector("[data-transfer-chip]");
  const bar = card.querySelector("[data-transfer-bar]");
  const clock = card.querySelector("[data-transfer-clock]");
  const count = card.querySelector("[data-transfer-count]");
  const confirmForm = card.querySelector("[data-transfer-confirm]");
  const renewForm = card.querySelector("[data-transfer-renew]");
  const expiredNote = card.querySelector("[data-transfer-expired-note]");

  /* The account lives about an hour. Prefer the seconds the server measured;
     fall back to the provider's closing time so the clock is never blank. */
  const seconds = Number(card.getAttribute("data-transfer-seconds") || 0);
  const expiresAt = String(card.getAttribute("data-transfer-expires") || "").trim();
  let deadline = 0;
  if (seconds > 0) {
    deadline = Date.now() + seconds * 1000;
  } else if (expiresAt) {
    const parsed = new Date(expiresAt.replace(" ", "T"));
    if (!Number.isNaN(parsed.getTime())) deadline = parsed.getTime();
  }
  const span = Math.max(1, seconds > 0 ? seconds : Math.round((deadline - Date.now()) / 1000));
  /* If the server gave no closing time at all, the card is shown as-is: an
     unknown deadline must never be mistaken for an expired account. */
  const timed = deadline > 0;

  card.querySelectorAll("[data-copy]").forEach((button) => {
    const label = button.querySelector("[data-copy-label]") || button;
    const reset = () => {
      button.removeAttribute("data-copied");
      label.textContent = "Copy";
    };
    button.addEventListener("click", async () => {
      const value = button.getAttribute("data-copy") || "";
      try {
        await navigator.clipboard.writeText(value);
      } catch (error) {
        /* Clipboard blocked (http, or a permission prompt): fall back. */
        const field = button
          .closest(".chat-transfer-hero")
          ?.querySelector("[data-copy-value]");
        if (field) {
          const range = document.createRange();
          range.selectNodeContents(field);
          const selection = window.getSelection();
          selection.removeAllRanges();
          selection.addRange(range);
          document.execCommand("copy");
          selection.removeAllRanges();
        }
      }
      button.setAttribute("data-copied", "true");
      label.textContent = "Copied";
      window.setTimeout(reset, 1800);
    });
  });

  const timeLabel = (date) =>
    date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const pad = (value) => String(value).padStart(2, "0");

  /* A visible countdown, and the honest end of it: the confirm button is
     replaced by "Get a new transfer account", because the provider has closed
     the old account and money sent to it would bounce. */
  const paint = () => {
    const left = timed ? Math.max(0, deadline - Date.now()) : 0;
    const minutes = Math.floor(left / 60000);
    const secs = Math.floor((left % 60000) / 1000);
    if (!timed) {
      if (count) count.textContent = "—";
      return true;
    }
    if (count) {
      count.textContent = left > 0 ? `${pad(minutes)}:${pad(secs)}` : "00:00";
      count.setAttribute("data-low", left > 0 && left < 5 * 60000 ? "true" : "false");
    }
    if (bar) {
      bar.style.width = deadline ? `${Math.min(100, (left / (span * 1000)) * 100)}%` : "0%";
      bar.setAttribute("data-low", left > 0 && left < 10 * 60000 ? "true" : "false");
    }
    if (left <= 0) {
      if (clock) {
        clock.textContent =
          "This account has closed. Nothing has been charged — get a fresh one.";
      }
      if (chip) {
        chip.textContent = "Expired";
        chip.setAttribute("data-state", "expired");
      }
      if (confirmForm) confirmForm.hidden = true;
      if (renewForm) renewForm.hidden = false;
      if (expiredNote) expiredNote.hidden = false;
      // The same moment the account closes, the way to start a new payment
      // comes back -- no reload, and the API refuses it until exactly now.
      const gate = document.querySelector("[data-pay-gate]");
      if (gate) {
        const start = gate.querySelector("[data-pay-start]");
        const wait = gate.querySelector("[data-pay-wait]");
        if (start) start.hidden = false;
        if (wait) {
          wait.hidden = false;
          wait.textContent =
            "This transfer account has closed. Nothing has been charged — start a fresh payment below.";
        }
        gate.setAttribute("data-pay-live", "false");
      }
      return false;
    }
    if (clock) {
      clock.textContent = `Closes at ${timeLabel(new Date(deadline))} — ${minutes} minute${minutes === 1 ? "" : "s"} left.`;
    }
    return true;
  };
  paint();
  const clockTimer = window.setInterval(() => {
    if (!paint()) window.clearInterval(clockTimer);
  }, 1000);

  const endpoint = card.getAttribute("data-payment-status");
  if (!endpoint) return;
  let attempts = 0;
  const tick = async () => {
    if (attempts >= 120) return;
    attempts += 1;
    try {
      const response = await fetch(endpoint, {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      if (response.ok) {
        const data = await response.json();
        const status = String(data.status || "").toUpperCase();
        if (status && status !== "PAYMENT_PENDING") {
          if (chip) {
            chip.textContent = status === "HELD" ? "Paid" : status.replace(/_/g, " ");
            chip.setAttribute("data-state", status === "HELD" ? "paid" : "expired");
          }
          window.setTimeout(() => window.location.reload(), 900);
          return;
        }
      }
    } catch (error) {
      /* offline: try again on the next tick */
    }
    window.setTimeout(tick, 6000);
  };
  window.setTimeout(tick, 6000);
})();
