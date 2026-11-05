document.addEventListener('DOMContentLoaded', () => {
  const page = document.querySelector('[data-login-page]');
  if (!page) return;

  const form = page.querySelector('[data-login-form]');
  const password = form?.querySelector('input[name="password"]');
  const passwordToggle = form?.querySelector('[data-login-password-toggle]');
  const reducedMotion = window.matchMedia
    ? window.matchMedia('(prefers-reduced-motion: reduce)')
    : { matches: false };
  const revealTargets = Array.from(page.querySelectorAll('[data-login-reveal]'));
  let revealObserver = null;

  function updateRevealMotion() {
    revealObserver?.disconnect();
    revealObserver = null;

    if (reducedMotion.matches || !('IntersectionObserver' in window)) {
      page.classList.remove('login-motion-ready');
      revealTargets.forEach((target) => target.classList.add('is-in-view'));
      return;
    }

    page.classList.add('login-motion-ready');
    revealTargets.forEach((target) => {
      const rect = target.getBoundingClientRect();
      const visible = rect.width > 0 && rect.height > 0
        && rect.top < window.innerHeight && rect.bottom > 0;
      target.classList.toggle('is-in-view', visible);
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

  function pulseButton(button) {
    if (reducedMotion.matches || button.disabled) return;
    if (button.loginEffectTimer) window.clearTimeout(button.loginEffectTimer);
    button.classList.remove('is-clicked', 'is-rippling');
    void button.offsetWidth;
    button.classList.add('is-clicked', 'is-rippling');
    button.loginEffectTimer = window.setTimeout(() => {
      button.classList.remove('is-clicked', 'is-rippling');
    }, 520);
  }

  page.addEventListener('click', (event) => {
    const target = event.target instanceof Element ? event.target : event.target?.parentElement;
    const button = target?.closest('button');
    if (button && page.contains(button)) pulseButton(button);
  }, true);

  if (password && passwordToggle) {
    passwordToggle.hidden = false;
    passwordToggle.addEventListener('click', () => {
      const reveal = password.type === 'password';
      password.type = reveal ? 'text' : 'password';
      passwordToggle.textContent = reveal ? 'Hide' : 'Show';
      passwordToggle.setAttribute('aria-label', `${reveal ? 'Hide' : 'Show'} password`);
      passwordToggle.setAttribute('aria-pressed', String(reveal));
      password.focus({ preventScroll: true });
    });
  }

  const email = form?.querySelector('input[name="email"]');
  if (email && !email.value && !form.querySelector('.has-error') && document.activeElement === document.body) {
    email.focus({ preventScroll: true });
  }

  form?.addEventListener('submit', () => {
    const submitButton = form.querySelector('[data-login-submit]');
    if (!submitButton) return;
    submitButton.classList.add('is-submitting');
    submitButton.setAttribute('aria-busy', 'true');
  });

  page.querySelectorAll('[data-login-provider]').forEach((providerForm) => {
    providerForm.addEventListener('submit', () => {
      const button = providerForm.querySelector('[data-login-provider-button]');
      if (!button) return;
      button.disabled = true;
      button.classList.add('is-connecting');
      button.setAttribute('aria-busy', 'true');
      const provider = providerForm.dataset.loginProvider === 'apple' ? 'Apple' : 'Google';
      const label = button.querySelector('span:last-child');
      if (label) label.textContent = `Connecting to ${provider}…`;
    });
  });

  updateRevealMotion();
  if (typeof reducedMotion.addEventListener === 'function') {
    reducedMotion.addEventListener('change', updateRevealMotion);
  } else if (typeof reducedMotion.addListener === 'function') {
    reducedMotion.addListener(updateRevealMotion);
  }
});
