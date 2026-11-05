/* Elapsed posting time only: this script never expires, hides or submits requests. */
(() => {
  const clocks = [...document.querySelectorAll('[data-request-age]')].map(element => {
    const seconds = Number(element.dataset.ageSeconds);
    if (element.dataset.ageSeconds === '' || !Number.isFinite(seconds)) return null;
    return {element, seconds:Math.max(0,seconds), started:performance.now()};
  }).filter(Boolean);
  if (!clocks.length) return;
  const format = total => {
    const days=Math.floor(total/86400), hours=Math.floor(total%86400/3600), minutes=Math.floor(total%3600/60), seconds=total%60;
    return `${days?days+'d ':''}${days||hours?hours+'h ':''}${days||hours||minutes?minutes+'m ':''}${seconds}s`;
  };
  const update = () => {
    clocks.forEach(clock=>{
      const seconds=Math.floor(clock.seconds+(performance.now()-clock.started)/1000);
      clock.element.textContent=format(seconds);
    });
  };
  let interval;
  const start = () => {clearInterval(interval);update();interval=setInterval(update,1000);};
  start();
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)update();});
  window.addEventListener('pagehide',()=>clearInterval(interval));
  window.addEventListener('pageshow',start);
})();
