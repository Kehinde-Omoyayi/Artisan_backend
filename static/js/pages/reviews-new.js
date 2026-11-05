document.addEventListener('DOMContentLoaded', () => { const r=document.querySelector('input[name=rating]'); if(r) { r.min=1; r.max=5; r.step=1; } });
