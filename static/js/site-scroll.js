/* Base-wide, reversible viewport reveals. No navigation or API side effects. */
(() => {
  const init = () => {
    const root = document.documentElement;
    const main = document.querySelector('main');
    if (!main || !('IntersectionObserver' in window)) return;
    const legacy = '[data-reveal],[data-artisan-reveal],[data-text-motion],[data-auth-reveal],[data-login-reveal],[data-register-reveal]';
    const selector = `${legacy},section,article,form,.panel,.card,.page-heading,.page-header,.section-heading,.landing-section-heading,.landing-hero__content,.landing-hero__visual,.authenticated-home-hero__copy,.authenticated-home-hero__visual,[class*="-card"],[class*="-panel"]`;
    const excluded = 'dialog,[role="dialog"],nav,[role="menu"],[role="listbox"],table,.response-stack,[role="alert"],[aria-live]:not([aria-live="off"]),[data-motion-skip],[hidden]';
    const targets = new Set();
    let scheduled = 0;
    const reduced = () => root.classList.contains('experience-motion-off') || matchMedia('(prefers-reduced-motion: reduce)').matches;
    const inView = el => {
      const rect = el.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < innerHeight * .96;
    };
    const show = (el, visible) => {
      // Never fade a control out while the user is working with it.
      if (reduced() || el.contains(document.activeElement)) visible = true;
      el.classList.toggle('experience-in-view', visible);
    };
    const observer = new IntersectionObserver(entries => {
      for (const entry of entries) {
        entry.target.style.setProperty('--experience-entry-y', entry.boundingClientRect.bottom <= 0 ? '-22px' : '22px');
        show(entry.target, entry.isIntersecting);
      }
    }, {threshold: 0, rootMargin: '0px 0px -4% 0px'});
    const eligible = el => {
      if (el.closest(excluded)) return false;
      if (el.matches(legacy) || el.matches('section,article,form,.panel,.card,.page-heading,.page-header,.section-heading,.landing-section-heading,.landing-hero__content,.landing-hero__visual,.authenticated-home-hero__copy,.authenticated-home-hero__visual')) return true;
      return [...el.classList].some(c => c.endsWith('-card') || c.endsWith('-panel'));
    };
    function discover() {
      scheduled = 0;
      // The leaf structural blocks move, rather than multiplying transforms on wrappers.
      const candidates = [...main.querySelectorAll(selector)].filter(eligible);
      const leaves = candidates.filter(el => !candidates.some(child => child !== el && el.contains(child)));
      for (const old of targets) {
        if (!old.isConnected || !leaves.includes(old)) {
          observer.unobserve(old);old.removeAttribute('data-experience-reveal');old.classList.remove('experience-in-view');targets.delete(old);
        }
      }
      for (const el of leaves) {
        if (targets.has(el)) continue;
        targets.add(el);
        show(el, inView(el));
        el.setAttribute('data-experience-reveal', '');
        observer.observe(el);
      }
      root.classList.add('experience-scroll-managed');
    }
    discover();
    // Covers newly loaded request cards and other partial updates without per-view wiring.
    new MutationObserver(() => {
      if (!scheduled) scheduled = requestAnimationFrame(discover);
    }).observe(main, {childList: true, subtree: true});
    window.addEventListener('experience-motion-change', () => targets.forEach(el => show(el, inView(el))));
    main.addEventListener('focusin', event => {
      targets.forEach(el => { if (el.contains(event.target)) show(el, true); });
    });
    main.addEventListener('focusout', () => requestAnimationFrame(() => targets.forEach(el => show(el, inView(el)))));
    window.addEventListener('beforeprint', () => root.classList.add('experience-print'));
    window.addEventListener('afterprint', () => root.classList.remove('experience-print'));
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true});
  else init();
})();