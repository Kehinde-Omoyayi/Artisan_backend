(() => {
  const initArtisanPage = () => {
    const page = document.querySelector('.artisan-motion-page');
    if (!page) return;

    const motionPreference = window.matchMedia
      ? window.matchMedia('(prefers-reduced-motion: reduce)')
      : { matches: false };
    const revealNodes = Array.from(page.querySelectorAll('[data-artisan-reveal]'));
    const textNodes = Array.from(page.querySelectorAll('[data-text-motion]'));
    let revealObserver = null;

    const splitHeading = (heading) => {
      if (heading.dataset.artisanTextReady === 'true') return;
      const label = heading.textContent.trim();
      if (!label) return;

      heading.dataset.artisanTextReady = 'true';
      heading.setAttribute('aria-label', label);
      heading.classList.add('artisan-text-split');
      const fragment = document.createDocumentFragment();
      Array.from(label).forEach((character, index) => {
        const letter = document.createElement('span');
        letter.className = 'artisan-text-char';
        letter.setAttribute('aria-hidden', 'true');
        letter.style.setProperty('--artisan-char-delay', `${Math.min(index, 32) * 18}ms`);
        letter.textContent = character === ' ' ? '\u00a0' : character;
        fragment.appendChild(letter);
      });
      heading.replaceChildren(fragment);
    };

    const revealAll = () => {
      if (revealObserver) revealObserver.disconnect();
      page.classList.remove('artisan-motion-ready');
      revealNodes.forEach((node) => node.classList.add('is-revealed'));
      textNodes.forEach((node) => node.classList.add('is-text-revealed'));
    };

    const enableMotion = () => {
      if (motionPreference.matches || !('IntersectionObserver' in window)) return;
      revealNodes.forEach((node, index) => {
        node.style.setProperty('--artisan-reveal-delay', `${Math.min(index % 7, 6) * 42}ms`);
      });
      textNodes.forEach(splitHeading);
      page.classList.add('artisan-motion-ready');

      revealObserver = new IntersectionObserver((entries, observer) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          const node = entry.target;
          if (node.matches('[data-artisan-reveal]')) node.classList.add('is-revealed');
          if (node.matches('[data-text-motion]')) node.classList.add('is-text-revealed');
          observer.unobserve(node);
        });
      }, { root: null, rootMargin: '0px 0px -7% 0px', threshold: 0.12 });

      revealNodes.forEach((node) => revealObserver.observe(node));
      textNodes.forEach((node) => revealObserver.observe(node));
    };

    enableMotion();

    const onMotionPreferenceChange = (event) => {
      if (event.matches) revealAll();
    };
    if (typeof motionPreference.addEventListener === 'function') {
      motionPreference.addEventListener('change', onMotionPreferenceChange);
    } else if (typeof motionPreference.addListener === 'function') {
      motionPreference.addListener(onMotionPreferenceChange);
    }

    const methodForm = page.querySelector('[data-bvn-method-form]');
    const selectionStatus = page.querySelector('[data-bvn-selection-status]');
    if (methodForm) {
      page.querySelectorAll('[data-bvn-method-option]').forEach((option) => {
        option.addEventListener('click', () => {
          const methodValue = option.dataset.bvnMethodOption || '';
          const methodField = Array.from(methodForm.elements).find((field) => field.name === 'method');
          if (!methodField || !methodValue) return;
          methodField.value = methodValue;
          methodField.dispatchEvent(new Event('input', { bubbles: true }));
          methodField.dispatchEvent(new Event('change', { bubbles: true }));
          page.querySelectorAll('[data-bvn-method-option]').forEach((candidate) => {
            const selected = candidate === option;
            candidate.classList.toggle('is-selected', selected);
            candidate.setAttribute('aria-pressed', String(selected));
          });
          if (selectionStatus) {
            const displayName = option.querySelector('.bvn-method-option-copy strong');
            selectionStatus.textContent = `${displayName ? displayName.textContent.trim() : methodValue} selected. Continue to submit your choice.`;
          }
        });
      });
    }

    const activeTimers = new WeakMap();
    let pressedTarget = null;
    const feedbackTarget = (target) => {
      if (!(target instanceof Element)) return null;
      return target.closest('button:not(:disabled), a[href], [role="button"]');
    };
    const releaseFeedback = (element, delay = 120) => {
      const previousTimer = activeTimers.get(element);
      if (previousTimer) window.clearTimeout(previousTimer);
      const timer = window.setTimeout(() => {
        element.classList.remove('is-pressed');
        activeTimers.delete(element);
      }, delay);
      activeTimers.set(element, timer);
    };

    page.addEventListener('pointerdown', (event) => {
      const target = feedbackTarget(event.target);
      if (!target || target.getAttribute('aria-disabled') === 'true') return;
      pressedTarget = target;
      target.classList.add('is-pressed');
      if (activeTimers.has(target)) {
        window.clearTimeout(activeTimers.get(target));
        activeTimers.delete(target);
      }
    });
    window.addEventListener('pointerup', () => {
      if (!pressedTarget) return;
      releaseFeedback(pressedTarget, 150);
      pressedTarget = null;
    });
    window.addEventListener('pointercancel', () => {
      if (!pressedTarget) return;
      releaseFeedback(pressedTarget, 0);
      pressedTarget = null;
    });
    page.addEventListener('click', (event) => {
      if (event.detail !== 0) return;
      const target = feedbackTarget(event.target);
      if (!target || target.getAttribute('aria-disabled') === 'true') return;
      target.classList.add('is-pressed');
      releaseFeedback(target, 150);
    });
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initArtisanPage, { once: true });
  } else {
    initArtisanPage();
  }
})();
