(() => {
  if ('serviceWorker' in navigator && (window.isSecureContext || ['localhost', '127.0.0.1'].includes(window.location.hostname))) {
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/service-worker.js', { scope: '/' }).catch(() => {
        // The site remains usable online even when offline support cannot be installed.
      });
    }, { once: true });
  }

  const showProfilePhotoFallback = (image) => {
    if (!image || image.dataset.profilePhotoFailed === 'true') return;
    image.dataset.profilePhotoFailed = 'true';
    image.hidden = true;
    const fallback = image.parentElement && image.parentElement.querySelector('[data-profile-photo-fallback]');
    if (fallback) fallback.hidden = false;
  };

  // Capture handles broken images inserted later by the conversation polling UI too.
  document.addEventListener('error', (event) => {
    const image = event.target;
    if (image && image.matches && image.matches('img[data-profile-photo-image]')) {
      showProfilePhotoFallback(image);
    }
  }, true);
  document.querySelectorAll('img[data-profile-photo-image]').forEach((image) => {
    if (image.complete && image.naturalWidth === 0) showProfilePhotoFallback(image);
  });

  const prefersReducedMotion = () => Boolean(
    window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches,
  );

  // Progressive site navigation. Links remain available without JavaScript.
  const menu = document.querySelector('[data-menu-toggle]');
  const nav = document.querySelector('[data-nav]');

  if (menu && nav) {
    document.body.classList.add('site-nav-ready');
    menu.setAttribute('aria-expanded', 'false');

    const closeNavigation = ({ returnFocus = false } = {}) => {
      nav.classList.remove('open');
      menu.setAttribute('aria-expanded', 'false');
      menu.setAttribute('aria-label', 'Open navigation');
      if (returnFocus) menu.focus();
    };

    menu.addEventListener('click', () => {
      const willOpen = menu.getAttribute('aria-expanded') !== 'true';
      menu.setAttribute('aria-expanded', String(willOpen));
      menu.setAttribute('aria-label', willOpen ? 'Close navigation' : 'Open navigation');
      nav.classList.toggle('open', willOpen);
    });

    nav.addEventListener('click', (event) => {
      if (event.target.closest('a')) closeNavigation();
    });

    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && nav.classList.contains('open')) {
        closeNavigation({ returnFocus: true });
      }
    });

    document.addEventListener('click', (event) => {
      if (nav.classList.contains('open') && !nav.contains(event.target) && !menu.contains(event.target)) {
        closeNavigation();
      }
    });
  }

  document.querySelectorAll('[data-dropdown]').forEach((button) => {
    button.addEventListener('click', () => {
      const group = button.parentElement;
      const willOpen = !group.classList.contains('open');
      group.classList.toggle('open', willOpen);
      button.setAttribute('aria-expanded', String(willOpen));
    });
  });

  document.querySelectorAll('.js-submit-form').forEach((form) => {
    form.addEventListener('submit', () => {
      const button = form.querySelector('button[type="submit"]');
      if (button) {
        button.disabled = true;
        button.dataset.originalText = button.textContent;
        button.textContent = 'Working…';
      }
    });
  });

  document.querySelectorAll('textarea[data-min-words]').forEach((textarea) => {
    const minimum = Math.max(1, Number.parseInt(textarea.dataset.minWords || '6', 10) || 6);
    const form = textarea.form;
    const feedback = document.createElement('small');
    const message = `Enter at least ${minimum} meaningful words. Punctuation alone does not count.`;
    feedback.className = 'support-description-feedback';
    feedback.setAttribute('aria-live', 'polite');
    textarea.insertAdjacentElement('afterend', feedback);

    const updateWordFeedback = () => {
      const words = textarea.value.match(/[\p{L}]+(?:[’'-][\p{L}]+)*/gu) || [];
      const valid = words.length >= minimum;
      textarea.setCustomValidity(valid ? '' : message);
      feedback.textContent = valid
        ? `${words.length} meaningful words — minimum met.`
        : `${words.length} / ${minimum} meaningful words. ${message}`;
      feedback.classList.toggle('is-invalid', !valid);
      return valid;
    };

    textarea.addEventListener('input', updateWordFeedback);
    if (form) {
      form.addEventListener('submit', (event) => {
        if (!updateWordFeedback()) {
          event.preventDefault();
          textarea.reportValidity();
          textarea.focus();
        }
      }, true);
    }
    updateWordFeedback();
  });

  document.querySelectorAll('input[type="file"]').forEach((input) => {
    input.addEventListener('change', () => {
      if (input.files[0] && input.files[0].size > 5 * 1024 * 1024) {
        window.alert('Please choose a file no larger than 5 MB.');
        input.value = '';
      }
    });
  });

  // Shared confirmation modal for logout and other explicitly confirmed actions.
  const confirmDialog = document.querySelector('[data-confirm-dialog]');
  if (confirmDialog) {
    const confirmTitle = confirmDialog.querySelector('[data-confirm-title]');
    const confirmDescription = confirmDialog.querySelector('[data-confirm-description]');
    const confirmCancel = confirmDialog.querySelector('[data-confirm-cancel]');
    const confirmAccept = confirmDialog.querySelector('[data-confirm-accept]');
    let pendingConfirmation = null;
    let confirmationReturnFocus = null;
    let confirmationCloseTimer = 0;

    const confirmationMessage = (form) => (
      form.dataset.confirm || 'Please confirm that you want to continue.'
    );

    const openConfirmation = (form, submitter) => {
      const isLogout = form.hasAttribute('data-confirm-logout');
      if (confirmDialog.open) return false;

      pendingConfirmation = { form, submitter };
      confirmationReturnFocus = submitter || document.activeElement;
      confirmTitle.textContent = isLogout ? 'Sign out of Artisan Market?' : 'Confirm this action';
      confirmDescription.textContent = isLogout
        ? (form.dataset.confirm || 'You will be signed out of this browser session. You can sign in again at any time.')
        : confirmationMessage(form);
      confirmCancel.textContent = isLogout ? 'Cancel' : 'Keep working';
      confirmAccept.textContent = isLogout ? 'Confirm logout' : 'Confirm action';
      confirmAccept.classList.toggle('site-dialog-button--danger', isLogout);
      confirmDialog.removeAttribute('data-closing');
      confirmDialog.showModal();
      confirmCancel.focus();
      return true;
    };

    const closeConfirmation = (confirmed) => {
      if (!confirmDialog.open || confirmDialog.hasAttribute('data-closing')) return;
      const action = pendingConfirmation;
      pendingConfirmation = null;
      confirmDialog.setAttribute('data-closing', 'true');
      window.clearTimeout(confirmationCloseTimer);

      const finishClose = () => {
        confirmationCloseTimer = 0;
        if (confirmDialog.open) confirmDialog.close(confirmed ? 'confirmed' : 'cancelled');
        confirmDialog.removeAttribute('data-closing');
        if (!confirmed) {
          if (confirmationReturnFocus && confirmationReturnFocus.isConnected) {
            confirmationReturnFocus.focus({ preventScroll: true });
          }
          confirmationReturnFocus = null;
          return;
        }
        confirmationReturnFocus = null;
        if (!action || !action.form.isConnected) return;
        action.form.dataset.confirmed = 'true';
        const submitter = action.submitter && action.submitter.form === action.form
          ? action.submitter
          : undefined;
        if (typeof action.form.requestSubmit === 'function') {
          try {
            if (submitter) action.form.requestSubmit(submitter);
            else action.form.requestSubmit();
          } catch (_error) {
            delete action.form.dataset.confirmed;
            action.form.submit();
          }
        } else {
          delete action.form.dataset.confirmed;
          action.form.submit();
        }
      };

      const delay = prefersReducedMotion() ? 0 : 170;
      if (delay) confirmationCloseTimer = window.setTimeout(finishClose, delay);
      else finishClose();
    };

    document.addEventListener('submit', (event) => {
      const form = event.target;
      if (!(form instanceof HTMLFormElement) || !form.matches('.js-confirm-form')) return;
      if (form.dataset.confirmed === 'true') {
        delete form.dataset.confirmed;
        return;
      }
      if (typeof confirmDialog.showModal !== 'function') {
        if (!window.confirm(confirmationMessage(form))) event.preventDefault();
        return;
      }
      event.preventDefault();
      if (!confirmDialog.open) openConfirmation(form, event.submitter);
    });

    confirmCancel.addEventListener('click', () => closeConfirmation(false));
    confirmAccept.addEventListener('click', () => closeConfirmation(true));
    confirmDialog.addEventListener('cancel', (event) => {
      event.preventDefault();
      closeConfirmation(false);
    });
    confirmDialog.addEventListener('click', (event) => {
      if (event.target === confirmDialog) closeConfirmation(false);
    });
    confirmDialog.addEventListener('close', () => {
      window.clearTimeout(confirmationCloseTimer);
      confirmationCloseTimer = 0;
      confirmDialog.removeAttribute('data-closing');
      pendingConfirmation = null;
    });
    window.addEventListener('pagehide', () => window.clearTimeout(confirmationCloseTimer));
  }

  // Only Django's shared response stack is temporary. Chat logs, notification
  // records, inline validation and persistent page content are not tagged here.
  const flashTimers = new WeakMap();
  const activeFlashNotices = new Set();

  const clearFlashNoticeTimers = (notice) => {
    const timers = flashTimers.get(notice);
    if (timers) {
      window.clearTimeout(timers.dismissTimer);
      window.clearTimeout(timers.removeTimer);
      if (timers.animationHandler) notice.removeEventListener('animationend', timers.animationHandler);
      flashTimers.delete(notice);
    }
    activeFlashNotices.delete(notice);
  };

  const cleanFlashTree = (node) => {
    if (!node || node.nodeType !== Node.ELEMENT_NODE) return;
    if (node.matches('[data-flash-message]')) clearFlashNoticeTimers(node);
    node.querySelectorAll('[data-flash-message]').forEach(clearFlashNoticeTimers);
  };

  const pruneFlashStack = (stack) => {
    if (stack && stack.isConnected && !stack.querySelector('[data-flash-message]')) stack.remove();
  };

  const removeFlashNotice = (notice) => {
    if (!notice || !notice.isConnected) {
      if (notice) clearFlashNoticeTimers(notice);
      return;
    }
    if (notice.dataset.flashState === 'dismissing') return;
    const timers = flashTimers.get(notice) || {};
    window.clearTimeout(timers.dismissTimer);
    notice.dataset.flashState = 'dismissing';
    notice.classList.add('is-dismissing');

    const finish = () => {
      const stack = notice.closest('[data-flash-stack]');
      clearFlashNoticeTimers(notice);
      if (notice.isConnected) notice.remove();
      pruneFlashStack(stack);
    };
    const onAnimationEnd = (event) => {
      if (event.target === notice) finish();
    };
    notice.addEventListener('animationend', onAnimationEnd);
    timers.animationHandler = onAnimationEnd;
    timers.removeTimer = window.setTimeout(finish, prefersReducedMotion() ? 20 : 300);
    flashTimers.set(notice, timers);
  };

  const initializeFlashTree = (node) => {
    if (!node || node.nodeType !== Node.ELEMENT_NODE) return;
    const notices = [];
    if (node.matches('[data-flash-message]')) notices.push(node);
    notices.push(...node.querySelectorAll('[data-flash-message]'));
    notices.forEach((notice) => {
      if (flashTimers.has(notice)) return;
      const timers = { dismissTimer: window.setTimeout(() => removeFlashNotice(notice), 5000), removeTimer: 0 };
      flashTimers.set(notice, timers);
      activeFlashNotices.add(notice);
    });
  };

  document.querySelectorAll('[data-flash-stack]').forEach(initializeFlashTree);
  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-flash-dismiss]');
    if (button) removeFlashNotice(button.closest('[data-flash-message]'));
  });

  if (document.body && typeof MutationObserver === 'function') {
    const flashObserver = new MutationObserver((mutations) => {
      mutations.forEach((mutation) => {
        mutation.removedNodes.forEach(cleanFlashTree);
        mutation.addedNodes.forEach(initializeFlashTree);
        const changedElement = mutation.target.nodeType === Node.ELEMENT_NODE ? mutation.target : null;
        pruneFlashStack(changedElement && changedElement.closest('[data-flash-stack]'));
      });
    });
    flashObserver.observe(document.body, { childList: true, subtree: true });
    window.addEventListener('pagehide', () => {
      flashObserver.disconnect();
      activeFlashNotices.forEach(clearFlashNoticeTimers);
    });
  }

  // The post-login escrow notice is deliberately blocking until the user acknowledges it.
  const paymentNoticeDialog = document.querySelector('[data-payment-notice-dialog]');
  if (
    paymentNoticeDialog
    && document.body.dataset.paymentNoticeAuto === 'true'
    && typeof paymentNoticeDialog.showModal === 'function'
  ) {
    paymentNoticeDialog.addEventListener('cancel', (event) => event.preventDefault());
    window.requestAnimationFrame(() => {
      if (!paymentNoticeDialog.open) paymentNoticeDialog.showModal();
      const acknowledgeButton = paymentNoticeDialog.querySelector('.payment-notice-acknowledge');
      if (acknowledgeButton) acknowledgeButton.focus({ preventScroll: true });
    });
  }

  // A role-aware five-step onboarding tour. Only one native modal step exists
  // at a time; the browser supplies focus containment and Escape semantics.
  const tourDialog = document.querySelector('[data-tour-dialog]');
  if (tourDialog && typeof tourDialog.showModal === 'function') {
    const tourTitle = tourDialog.querySelector('[data-tour-title]');
    const tourDescription = tourDialog.querySelector('[data-tour-description]');
    const tourProgress = tourDialog.querySelector('[data-tour-progress]');
    const tourCard = tourDialog.querySelector('[data-tour-card]');
    const tourSpotlight = tourDialog.querySelector('[data-tour-spotlight]');
    const shade = Object.fromEntries(
      ['top', 'left', 'right', 'bottom'].map((side) => [side, tourDialog.querySelector(`[data-tour-shade="${side}"]`)]),
    );
    const previousButton = tourDialog.querySelector('[data-tour-previous]');
    const nextButton = tourDialog.querySelector('[data-tour-next]');
    const skipButton = tourDialog.querySelector('[data-tour-skip]');
    const finishButton = tourDialog.querySelector('[data-tour-finish]');
    const userKey = document.body.dataset.tourUser || 'guest';
    const storagePrefix = `artisan-market:onboarding:v1:${userKey}`;
    const role = document.body.dataset.tourRole || 'guest';
    const isPlatformManager = document.body.dataset.tourManager === 'true';
    const MAX_TOUR_STEPS = 5;
    let tourSteps = [];
    let activeStep = 0;
    let returnFocus = null;
    let tourCloseTimer = 0;
    let targetRefreshTimer = 0;

    const readTourValue = (key) => {
      try { return window.localStorage.getItem(`${storagePrefix}:${key}`); } catch (_error) { /* use session storage */ }
      try { return window.sessionStorage.getItem(`${storagePrefix}:${key}`); } catch (_error) { return null; }
    };
    const writeTourValue = (key, value) => {
      try { window.localStorage.setItem(`${storagePrefix}:${key}`, value); return; } catch (_error) { /* use session storage */ }
      try { window.sessionStorage.setItem(`${storagePrefix}:${key}`, value); } catch (_error) { /* storage can be disabled */ }
    };

    const stepsFor = (userRole, manager) => {
      const discovery = {
        target: 'discovery',
        title: 'Start with discovery',
        description: 'Browse live categories and public artisan profiles. The six homepage carousel themes are editorial illustrations, separate from live catalogue data.',
      };
      const paymentAgreement = {
        target: 'payment-agreement',
        title: 'Payment follows agreement',
        description: 'Agree on scope, price and timing before payment. After an offer is accepted, the customer pays through the marketplace and provider-verified funds are held in escrow while work is underway. The artisan submits completion for review; customer confirmation starts payout-release processing. Eligible disputes can pause the relevant funds.',
      };
      const community = {
        target: 'activity',
        title: 'Find updates and support',
        description: 'Use the navigation for notifications and support. The help control can reopen this tour whenever you need it.',
      };

      if (userRole === 'customer') {
        return [
          discovery,
          paymentAgreement,
          {
            target: 'transactions',
            title: 'Request, message and book',
            description: 'Create a service request, compare artisan responses, then continue with the artisan you choose. Offers, bookings and secure payment stay connected to the conversation.',
          },
          {
            target: 'account',
            title: 'Keep your customer details current',
            description: 'Your customer profile holds the contact and location details artisans need to coordinate with you. Complete any requested fields before a protected action.',
          },
          community,
        ];
      }
      if (userRole === 'artisan') {
        return [
          {
            ...discovery,
            description: 'See how customers browse the live catalogue and public artisan profiles. Homepage editorial slides are visual examples, not service listings.',
          },
          paymentAgreement,
          {
            target: 'transactions',
            title: 'Respond and move work forward',
            description: 'Review matched requests, respond to customers and continue in the conversation. An accepted offer leads into the booking and payment workflow.',
          },
          {
            target: 'account',
            title: 'Profile and verification',
            description: 'Use Account to maintain your artisan details, verification status and payout information. Verification and service eligibility remain controlled by the marketplace.',
          },
          community,
        ];
      }
      if (userRole === 'staff') {
        return [
          {
            ...discovery,
            description: 'The public catalogue and artisan profiles are separate from staff tools; the homepage carousel remains editorial content.',
          },
          paymentAgreement,
          {
            target: 'support',
            title: 'Support workflows',
            description: 'Use the case queue and refund workspace available to your role. API permissions continue to govern every support action.',
          },
          {
            target: manager ? 'manager' : 'support',
            title: manager ? 'Manager overview' : 'Your staff access',
            description: manager
              ? 'The manager overview provides aggregate marketplace analytics. The separate support desk remains available for permitted case operations.'
              : 'Support navigation reflects your assigned responsibilities. Protected operations are checked by the API, not just by what appears in the menu.',
          },
          community,
        ];
      }
      return [
        discovery,
        paymentAgreement,
        {
          target: 'transactions',
          title: 'Requests and conversations',
          description: 'Sign in when you are ready to send a protected request or message. Offers, bookings and payments continue through the marketplace workflow.',
        },
        {
          target: 'account',
          title: 'Set up your account',
          description: 'Create an account and complete the details requested for your chosen role. Customer and artisan setup steps are kept separate.',
        },
        community,
      ];
    };

    const targetSelectors = {
      discovery: [
        '[data-tour-target="discovery"]',
        '[data-menu-toggle]',
        '[data-landing-menu]',
        '#main-content',
      ],
      'profiles-services': [
        '[data-tour-target="profiles-services"]',
        '#artisan-discovery-filters',
        '#service-category-filter',
        '.artisan-discovery-profile-hero',
        '[data-tour-target="discovery"]',
        '[data-menu-toggle]',
        '[data-landing-menu]',
        '#main-content',
      ],
      'payment-agreement': [
        '[data-tour-target=\"payment-agreement\"]',
        '[data-payment-section]',
        '#payments',
        '.authenticated-home-hero',
        '.support-workspace-hero',
        '.support-page-hero',
        '.manager-dashboard',
        '#main-content',
      ],
      transactions: [
        '[data-tour-target="transactions"]',
        '#how-it-works',
        '[data-menu-toggle]',
        '[data-landing-menu]',
        '#main-content',
      ],
      account: [
        '[data-tour-target="account"]',
        '.customer-profile-form-card',
        '.artisan-profile-page',
        '[data-menu-toggle]',
        '[data-landing-menu]',
        '#main-content',
      ],
      support: [
        '[data-tour-target="support"]',
        '[data-menu-toggle]',
        '[data-landing-menu]',
        '#main-content',
      ],
      activity: [
        '[data-tour-target="notifications"]',
        '[data-tour-target="support"]',
        '[data-onboarding-open]',
        '[data-menu-toggle]',
        '[data-landing-menu]',
        '#main-content',
      ],
      manager: [
        '[data-tour-target="manager"]',
        '.manager-dashboard',
        '[data-menu-toggle]',
        '[data-landing-menu]',
        '#main-content',
      ],
    };

    const isVisible = (element) => {
      if (!element || !element.isConnected) return false;
      const style = window.getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity) !== 0
        && rect.width > 0 && rect.height > 0;
    };

    const findTourTarget = () => {
      const step = tourSteps[activeStep];
      if (!step) return null;
      const selectors = targetSelectors[step.target] || ['#main-content'];
      for (const selector of selectors) {
        const elements = document.querySelectorAll(selector);
        for (const element of elements) {
          if (isVisible(element)) return element;
        }
      }
      return null;
    };

    const placeShade = (element, left, top, width, height) => {
      if (!element) return;
      element.style.left = `${Math.max(0, left)}px`;
      element.style.top = `${Math.max(0, top)}px`;
      element.style.width = `${Math.max(0, width)}px`;
      element.style.height = `${Math.max(0, height)}px`;
    };

    const positionTourCard = (spot) => {
      if (!tourCard) return;
      tourCard.style.left = '';
      tourCard.style.top = '';
      if (window.innerWidth <= 640) return;
      const width = tourCard.offsetWidth || 420;
      const height = tourCard.offsetHeight || 290;
      const left = Math.max(16, Math.min(spot.left, window.innerWidth - width - 16));
      let top = spot.bottom + 18;
      if (top + height > window.innerHeight - 16) top = spot.top - height - 18;
      top = Math.max(16, Math.min(top, window.innerHeight - height - 16));
      tourCard.style.left = `${left}px`;
      tourCard.style.top = `${top}px`;
    };

    const layoutTourOverlay = () => {
      if (!tourDialog.open) return;
      const viewportWidth = window.innerWidth;
      const viewportHeight = window.innerHeight;
      const target = findTourTarget();
      if (!target) {
        tourSpotlight.hidden = true;
        placeShade(shade.top, 0, 0, viewportWidth, viewportHeight);
        placeShade(shade.left, 0, 0, 0, 0);
        placeShade(shade.right, 0, 0, 0, 0);
        placeShade(shade.bottom, 0, 0, 0, 0);
        positionTourCard({ left: 16, top: 16, bottom: 16 });
        return;
      }

      const rect = target.getBoundingClientRect();
      const left = Math.max(8, Math.min(rect.left - 7, viewportWidth - 8));
      const top = Math.max(8, Math.min(rect.top - 7, viewportHeight - 8));
      const right = Math.max(left, Math.min(rect.right + 7, viewportWidth - 8));
      const bottom = Math.max(top, Math.min(rect.bottom + 7, viewportHeight - 8));
      const width = right - left;
      const height = bottom - top;

      tourSpotlight.hidden = false;
      placeShade(shade.top, 0, 0, viewportWidth, top);
      placeShade(shade.left, 0, top, left, height);
      placeShade(shade.right, right, top, viewportWidth - right, height);
      placeShade(shade.bottom, 0, bottom, viewportWidth, viewportHeight - bottom);
      positionTourCard({ left, top, bottom });
    };

    const renderTourStep = ({ scroll = true } = {}) => {
      const step = tourSteps[activeStep];
      if (!step) return;
      tourTitle.textContent = step.title;
      tourDescription.textContent = step.description;
      tourProgress.textContent = `Step ${activeStep + 1} of ${tourSteps.length}`;
      previousButton.hidden = activeStep === 0;
      nextButton.hidden = activeStep === tourSteps.length - 1;
      finishButton.hidden = activeStep !== tourSteps.length - 1;
      tourDialog.dataset.currentStep = String(activeStep + 1);

      const target = findTourTarget();
      if (scroll && target) {
        const rect = target.getBoundingClientRect();
        if (rect.top < 16 || rect.bottom > window.innerHeight - 16) {
          target.scrollIntoView({
            behavior: prefersReducedMotion() ? 'auto' : 'smooth',
            block: 'center',
            inline: 'nearest',
          });
        }
      }
      window.requestAnimationFrame(layoutTourOverlay);
      window.clearTimeout(targetRefreshTimer);
      targetRefreshTimer = window.setTimeout(layoutTourOverlay, prefersReducedMotion() ? 0 : 260);
    };

    const closeTour = (status) => {
      if (!tourDialog.open || tourDialog.hasAttribute('data-closing')) return;
      if (status) writeTourValue('status', status);
      tourDialog.setAttribute('data-closing', 'true');
      window.clearTimeout(tourCloseTimer);
      window.clearTimeout(targetRefreshTimer);
      const finishClose = () => {
        tourCloseTimer = 0;
        if (tourDialog.open) tourDialog.close(status || 'closed');
        tourDialog.removeAttribute('data-closing');
        if (returnFocus && returnFocus.isConnected) returnFocus.focus({ preventScroll: true });
        returnFocus = null;
      };
      const delay = prefersReducedMotion() ? 0 : 180;
      if (delay) tourCloseTimer = window.setTimeout(finishClose, delay);
      else finishClose();
    };

    const startTour = () => {
      if (tourDialog.open) return;
      tourSteps = stepsFor(role, isPlatformManager).slice(0, MAX_TOUR_STEPS);
      activeStep = 0;
      returnFocus = document.activeElement;
      writeTourValue('offered', 'true');
      tourDialog.removeAttribute('data-closing');
      tourDialog.showModal();
      renderTourStep();
      tourTitle.focus({ preventScroll: true });
    };

    document.addEventListener('click', (event) => {
      if (event.target.closest('[data-onboarding-open]')) {
        event.preventDefault();
        startTour();
      }
    });
    previousButton.addEventListener('click', () => {
      if (activeStep > 0) {
        activeStep -= 1;
        renderTourStep();
      }
    });
    nextButton.addEventListener('click', () => {
      if (activeStep < tourSteps.length - 1) {
        activeStep += 1;
        renderTourStep();
      }
    });
    skipButton.addEventListener('click', () => closeTour('skipped'));
    finishButton.addEventListener('click', () => closeTour('completed'));
    tourDialog.addEventListener('cancel', (event) => {
      event.preventDefault();
      closeTour('skipped');
    });
    tourDialog.addEventListener('close', () => {
      window.clearTimeout(tourCloseTimer);
      window.clearTimeout(targetRefreshTimer);
      tourCloseTimer = 0;
      tourDialog.removeAttribute('data-closing');
      tourDialog.removeAttribute('data-current-step');
    });

    let overlayFrame = 0;
    const refreshTourOverlay = () => {
      if (!tourDialog.open || overlayFrame) return;
      overlayFrame = window.requestAnimationFrame(() => {
        overlayFrame = 0;
        layoutTourOverlay();
      });
    };
    window.addEventListener('resize', refreshTourOverlay, { passive: true });
    window.addEventListener('scroll', refreshTourOverlay, { passive: true });
    window.addEventListener('pagehide', () => {
      window.clearTimeout(tourCloseTimer);
      window.clearTimeout(targetRefreshTimer);
      if (overlayFrame) window.cancelAnimationFrame(overlayFrame);
    });

    const shouldAutoOffer = document.body.dataset.tourAuto === 'true';
    if (shouldAutoOffer && !readTourValue('status') && !readTourValue('offered')) {
      writeTourValue('offered', 'true');
      window.setTimeout(() => {
        if (!tourDialog.open) startTour();
      }, 700);
    }
  }
})();
