document.addEventListener('DOMContentLoaded', () => { const e=document.querySelector('input[name=otp]'); if(e) { e.inputMode='numeric'; e.maxLength=6; e.focus(); } });
