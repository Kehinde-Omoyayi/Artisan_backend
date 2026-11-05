document.addEventListener('DOMContentLoaded', () => {
  const flow = document.querySelector('[data-register-flow]');
  if (!flow) return;

  const roleStage = flow.querySelector('[data-register-stage="role"]');
  const detailsStage = flow.querySelector('[data-register-stage="details"]');
  const nextButton = flow.querySelector('[data-register-next]');
  const backButton = flow.querySelector('[data-register-back]');
  const roleFeedback = flow.querySelector('[data-register-role-feedback]');
  const roleOptions = flow.querySelector('.register-role-options');
  const stepStatus = document.querySelector('[data-register-step-status]');
  const detailsHeading = flow.querySelector('#register-details-title');
  const radios = Array.from(flow.querySelectorAll('input[name="role"]'));
  const revealTargets = Array.from(flow.querySelectorAll('[data-register-reveal]'));
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  if (!roleStage || !detailsStage || !nextButton || !backButton || radios.length === 0) return;

  nextButton.hidden = false;
  let revealObserver = null;

  const selectedRole = () => radios.find((radio) => radio.checked);

  function animateStage(stage) {
    stage.classList.remove('register-stage-enter');
    if (reducedMotion.matches) return;
    void stage.offsetWidth;
    stage.classList.add('register-stage-enter');
    stage.addEventListener('animationend', (event) => {
      if (event.target === stage) stage.classList.remove('register-stage-enter');
    }, { once: true });
  }

  function showStep(step, moveFocus = false) {
    const details = step === 'details';
    roleStage.hidden = details;
    detailsStage.hidden = !details;
    nextButton.hidden = details;
    backButton.hidden = !details;
    animateStage(details ? detailsStage : roleStage);

    if (stepStatus) {
      const selected = selectedRole();
      const roleName = selected?.closest('.register-role-choice')?.querySelector('.register-role-name')?.textContent.trim();
      stepStatus.textContent = details
        ? `Step 2 of 2 · Enter your account details${roleName ? ` as a ${roleName.toLowerCase()}` : ''}.`
        : 'Step 1 of 2 · Choose your role first.';
    }

    if (moveFocus) {
      if (details) detailsHeading?.focus();
      else (selectedRole() || radios[0]).focus();
    }
  }

  function setRevealMotion() {
    revealObserver?.disconnect();
    revealObserver = null;

    if (reducedMotion.matches || !('IntersectionObserver' in window)) {
      flow.classList.remove('register-motion-ready');
      revealTargets.forEach((target) => target.classList.add('is-in-view'));
      return;
    }

    flow.classList.add('register-motion-ready');
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
    }, { threshold: 0.12, rootMargin: '0px 0px -5% 0px' });

    revealTargets.forEach((target) => {
      if (!target.classList.contains('is-in-view')) revealObserver.observe(target);
    });
  }

  function playButtonEffect(button) {
    if (reducedMotion.matches || button.disabled) return;
    button.classList.remove('is-clicked', 'is-rippling');
    void button.offsetWidth;
    button.classList.add('is-clicked', 'is-rippling');
    window.setTimeout(() => button.classList.remove('is-clicked', 'is-rippling'), 520);
  }

  function playChoiceEffect(choice) {
    if (!choice || reducedMotion.matches) return;
    choice.classList.remove('is-selected-pop');
    void choice.offsetWidth;
    choice.classList.add('is-selected-pop');
    window.setTimeout(() => choice.classList.remove('is-selected-pop'), 360);
  }

  function shakeRoleOptions() {
    if (!roleOptions || reducedMotion.matches) return;
    roleOptions.classList.remove('is-shaking');
    void roleOptions.offsetWidth;
    roleOptions.classList.add('is-shaking');
    window.setTimeout(() => roleOptions.classList.remove('is-shaking'), 400);
  }

  function clearRoleError() {
    if (roleFeedback) {
      roleFeedback.textContent = '';
      roleFeedback.hidden = true;
    }
    radios.forEach((radio) => radio.removeAttribute('aria-invalid'));
  }

  function showRoleError(message) {
    if (roleFeedback) {
      roleFeedback.textContent = message;
      roleFeedback.hidden = false;
    }
    radios.forEach((radio) => radio.setAttribute('aria-invalid', 'true'));
    shakeRoleOptions();
    radios[0].focus();
  }

  flow.addEventListener('click', (event) => {
    const target = event.target instanceof Element ? event.target : event.target?.parentElement;
    const button = target?.closest('button');
    if (button && flow.contains(button)) playButtonEffect(button);
  }, true);

  nextButton.addEventListener('click', () => {
    if (!selectedRole()) {
      showRoleError('Choose a role to continue.');
      return;
    }

    clearRoleError();
    showStep('details', true);
  });

  backButton.addEventListener('click', () => showStep('role', true));

  flow.querySelectorAll('[data-social-provider]').forEach((button) => {
    button.addEventListener('click', () => {
      const chosen = selectedRole();
      if (!chosen) {
        showStep('role', true);
        showRoleError('Choose a role before continuing with a provider.');
        return;
      }

      clearRoleError();

      const csrfToken = flow.querySelector('input[name="csrfmiddlewaretoken"]')?.value || '';
      const socialForm = document.createElement('form');
      socialForm.method = 'post';
      socialForm.action = `/auth/social/${encodeURIComponent(button.dataset.socialProvider)}/start/`;
      socialForm.hidden = true;
      const values = {
        csrfmiddlewaretoken: csrfToken,
        intent: 'register',
        role: chosen.value,
        first_name: flow.querySelector('[name="first_name"]')?.value || '',
        last_name: flow.querySelector('[name="last_name"]')?.value || '',
      };
      Object.entries(values).forEach(([name, value]) => {
        const input = document.createElement('input');
        input.type = 'hidden';
        input.name = name;
        input.value = value;
        socialForm.append(input);
      });
      document.body.append(socialForm);
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
      button.classList.add('is-connecting');
      button.textContent = 'Connecting…';
      socialForm.submit();
    });
  });

  radios.forEach((radio) => {
    radio.addEventListener('change', () => {
      clearRoleError();
      playChoiceEffect(radio.closest('.register-role-choice'));
    });
  });

  showStep(flow.dataset.registerStartStep === 'details' ? 'details' : 'role');
  setRevealMotion();

  if (typeof reducedMotion.addEventListener === 'function') {
    reducedMotion.addEventListener('change', setRevealMotion);
  } else if (typeof reducedMotion.addListener === 'function') {
    reducedMotion.addListener(setRevealMotion);
  }
});
