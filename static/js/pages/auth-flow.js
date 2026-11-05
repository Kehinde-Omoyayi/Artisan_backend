document.addEventListener('DOMContentLoaded', () => {
  const page = document.querySelector('[data-auth-flow-page]');
  if (!page) return;

  const reducedMotion = window.matchMedia
    ? window.matchMedia('(prefers-reduced-motion: reduce)')
    : { matches: false };
  const revealTargets = Array.from(page.querySelectorAll('[data-auth-reveal]'));
  let revealObserver = null;

  function updateRevealMotion() {
    revealObserver?.disconnect();
    revealObserver = null;

    if (reducedMotion.matches || !('IntersectionObserver' in window)) {
      page.classList.remove('auth-flow-motion-ready');
      revealTargets.forEach((target) => target.classList.add('is-in-view'));
      return;
    }

    page.classList.add('auth-flow-motion-ready');
    revealTargets.forEach((target) => {
      const rect = target.getBoundingClientRect();
      const alreadyVisible = rect.width > 0 && rect.height > 0
        && rect.top < window.innerHeight && rect.bottom > 0;
      target.classList.toggle('is-in-view', alreadyVisible);
    });

    revealObserver = new IntersectionObserver((entries, observer) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add('is-in-view');
        observer.unobserve(entry.target);
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -4% 0px' });

    revealTargets.forEach((target) => {
      if (!target.classList.contains('is-in-view')) revealObserver.observe(target);
    });
  }

  function animateButton(button) {
    if (reducedMotion.matches || button.disabled) return;
    if (button.authFlowTimer) window.clearTimeout(button.authFlowTimer);
    button.classList.remove('is-clicked', 'is-rippling');
    void button.offsetWidth;
    button.classList.add('is-clicked', 'is-rippling');
    button.authFlowTimer = window.setTimeout(() => {
      button.classList.remove('is-clicked', 'is-rippling');
    }, 520);
  }

  page.addEventListener('click', (event) => {
    const target = event.target instanceof Element ? event.target : event.target?.parentElement;
    const button = target?.closest('button');
    if (button && page.contains(button)) animateButton(button);
  }, true);

  page.querySelectorAll('.auth-flow-form input[type="password"]').forEach((input) => {
    const wrap = document.createElement('div');
    wrap.className = 'auth-flow-password-wrap';
    input.parentNode.insertBefore(wrap, input);
    wrap.append(input);

    const toggle = document.createElement('button');
    toggle.className = 'auth-flow-password-toggle';
    toggle.type = 'button';
    toggle.textContent = 'Show';
    toggle.setAttribute('aria-label', 'Show password');
    toggle.setAttribute('aria-pressed', 'false');
    if (input.id) toggle.setAttribute('aria-controls', input.id);
    toggle.addEventListener('click', () => {
      const reveal = input.type === 'password';
      input.type = reveal ? 'text' : 'password';
      toggle.textContent = reveal ? 'Hide' : 'Show';
      toggle.setAttribute('aria-label', `${reveal ? 'Hide' : 'Show'} password`);
      toggle.setAttribute('aria-pressed', String(reveal));
      input.focus({ preventScroll: true });
    });
    wrap.append(toggle);
  });

  const otp = page.querySelector('input[name="otp"]');
  if (otp) {
    otp.inputMode = 'numeric';
    otp.maxLength = 6;
    otp.setAttribute('autocomplete', 'one-time-code');
  }

  page.querySelectorAll('[data-auth-submit-form]').forEach((form) => {
    form.addEventListener('submit', () => {
      const button = form.querySelector('[data-auth-submit-button]');
      if (!button) return;
      button.classList.add('is-submitting');
      button.setAttribute('aria-busy', 'true');
    });
  });

  updateRevealMotion();
  if (typeof reducedMotion.addEventListener === 'function') {
    reducedMotion.addEventListener('change', updateRevealMotion);
  } else if (typeof reducedMotion.addListener === 'function') {
    reducedMotion.addListener(updateRevealMotion);
  }
});
