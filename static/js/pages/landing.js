(() => {
  const body = document.body;
  const hero = document.querySelector('[data-hero]');
  const title = document.querySelector('[data-hero-title]');
  const description = document.querySelector('[data-hero-description]');
  const image = document.querySelector('[data-hero-image]');
  const slideSource = document.querySelector('[data-slide-source]');
  const slides = slideSource
    ? Array.from(slideSource.querySelectorAll('[data-carousel-slide]')).map((node) => ({
        name: node.dataset.name || '',
        title: node.dataset.title || '',
        description: node.dataset.description || '',
        image640: node.getAttribute('data-image-640') || '',
        image1440: node.getAttribute('data-image-1440') || '',
      }))
    : [];

  const controls = document.querySelector('[data-hero-controls]');
  const previousButton = document.querySelector('[data-hero-previous]');
  const nextButton = document.querySelector('[data-hero-next]');
  const pauseButton = document.querySelector('[data-hero-pause]');
  const controlDivider = controls && controls.querySelector('.landing-hero__control-divider');
  const counter = document.querySelector('[data-hero-counter]');
  const announcement = document.querySelector('[data-hero-announcement]');
  const paymentSection = document.querySelector('[data-payment-section]');
  const menuButton = document.querySelector('[data-landing-menu]');
  const navigation = document.querySelector('[data-landing-nav]');

  const ROTATION_MS = window.ArtisanMotion?.heroRotationMs || 2000;
  const TITLE_TYPE_MS = window.ArtisanMotion?.titleTypeMs || 350;
  const DESCRIPTION_TYPE_MS = window.ArtisanMotion?.descriptionTypeMs || 380;
  const ERASE_MS = 235;
  const IMAGE_FADE_MS = 235;
  const ROTATION_LEAD_MS = Math.max(0, ROTATION_MS - ERASE_MS);
  const motionPreference = window.matchMedia
    ? window.matchMedia('(prefers-reduced-motion: reduce)')
    : { matches: false };

  let reducedMotion = Boolean(motionPreference.matches) || document.documentElement.classList.contains('experience-motion-off');
  let currentIndex = slides.length ? 0 : -1;
  let userPaused = false;
  let documentVisible = document.visibilityState !== 'hidden';
  let heroVisible = true;
  let cycleTimer = 0;
  let operationId = 0;
  let revealObserver = null;
  let paymentObserver = null;
  let visibilityFrame = 0;

  const initialTitle = title ? title.textContent.trim() : '';
  const initialDescription = description ? description.textContent.trim() : '';

  function clearCycleTimer() {
    if (cycleTimer) {
      window.clearTimeout(cycleTimer);
      cycleTimer = 0;
    }
  }

  function canRotate() {
    return slides.length > 1
      && !userPaused
      && !reducedMotion
      && documentVisible
      && heroVisible;
  }

  function updateCounter() {
    if (!counter || !slides.length || currentIndex < 0) return;
    const current = String(currentIndex + 1).padStart(2, '0');
    const total = String(slides.length).padStart(2, '0');
    counter.textContent = `${current} / ${total}`;
  }

  function setPhoto(slide, animate, operation = operationId) {
    if (!image || !slide || !slide.image640 || !slide.image1440) return;
    if (animate) image.classList.add('is-fading');
    image.sizes = '(max-width: 760px) 92vw, (max-width: 1200px) 48vw, 560px';
    image.srcset = `${slide.image640} 640w, ${slide.image1440} 1440w`;
    image.src = slide.image640;

    if (!animate) {
      image.classList.remove('is-fading');
      return;
    }

    const reveal = () => {
      if (operation === operationId) image.classList.remove('is-fading');
    };
    if (typeof image.decode === 'function') {
      image.decode().then(reveal).catch(reveal);
    } else {
      image.addEventListener('load', reveal, { once: true });
      window.setTimeout(reveal, IMAGE_FADE_MS + 100);
    }
  }

  function typeText(element, text, duration, operation) {
    if (!element) return Promise.resolve();
    const characters = Array.from(text);
    if (reducedMotion || !characters.length) {
      element.textContent = text;
      return Promise.resolve();
    }

    element.textContent = '';
    return new Promise((resolve) => {
      const started = performance.now();
      const draw = (now) => {
        if (operation !== operationId) {
          resolve();
          return;
        }
        const progress = Math.min(1, Math.max(0, (now - started) / duration));
        const visibleCharacters = Math.min(characters.length, Math.floor(progress * characters.length));
        element.textContent = characters.slice(0, visibleCharacters).join('');
        if (progress >= 1) {
          element.textContent = text;
          resolve();
          return;
        }
        window.requestAnimationFrame(draw);
      };
      window.requestAnimationFrame(draw);
    });
  }

  function eraseText(element, duration, operation) {
    if (!element) return Promise.resolve();
    const characters = Array.from(element.textContent);
    if (!characters.length) return Promise.resolve();

    return new Promise((resolve) => {
      const started = performance.now();
      const draw = (now) => {
        if (operation !== operationId) {
          resolve();
          return;
        }
        const progress = Math.min(1, Math.max(0, (now - started) / duration));
        const remaining = Math.max(0, characters.length - Math.floor(progress * characters.length));
        element.textContent = characters.slice(0, remaining).join('');
        if (progress >= 1) {
          element.textContent = '';
          resolve();
          return;
        }
        window.requestAnimationFrame(draw);
      };
      window.requestAnimationFrame(draw);
    });
  }

  function settleCurrentSlide() {
    operationId += 1;
    if (image) image.classList.remove('is-fading');
    const slide = currentIndex >= 0 ? slides[currentIndex] : null;
    if (title) title.textContent = slide ? slide.title : initialTitle;
    if (description) description.textContent = slide ? slide.description : initialDescription;
  }

  function scheduleNext() {
    clearCycleTimer();
    if (!canRotate()) return;
    cycleTimer = window.setTimeout(() => {
      cycleTimer = 0;
      changeSlide((currentIndex + 1) % slides.length);
    }, ROTATION_LEAD_MS);
  }

  function isInViewport(element) {
    if (!element) return false;
    const rectangle = element.getBoundingClientRect();
    return rectangle.bottom > 0
      && rectangle.top < window.innerHeight
      && rectangle.right > 0
      && rectangle.left < window.innerWidth;
  }

  function syncPaymentOrbit() {
    if (!paymentSection) return;
    paymentSection.classList.toggle(
      'is-in-view',
      !reducedMotion && documentVisible && isInViewport(paymentSection),
    );
  }

  function syncHeroVisibility() {
    if (!hero) return;
    const nowVisible = isInViewport(hero);
    if (nowVisible === heroVisible) return;
    heroVisible = nowVisible;
    if (!heroVisible) {
      clearCycleTimer();
      settleCurrentSlide();
    } else {
      scheduleNext();
    }
  }

  function changeSlide(nextIndex, { manual = false } = {}) {
    if (!slides.length || !title || !description) return;
    clearCycleTimer();

    const normalizedIndex = (nextIndex + slides.length) % slides.length;
    if (normalizedIndex === currentIndex) {
      scheduleNext();
      return;
    }

    const slide = slides[normalizedIndex];
    const operation = ++operationId;

    const applySlide = () => {
      if (operation !== operationId) return;
      currentIndex = normalizedIndex;
      title.textContent = '';
      description.textContent = '';
      setPhoto(slide, !reducedMotion, operation);
      updateCounter();
      if (manual && announcement) announcement.textContent = `${slide.name}. ${slide.description}`;
      Promise.all([
        typeText(title, slide.title, TITLE_TYPE_MS, operation),
        typeText(description, slide.description, DESCRIPTION_TYPE_MS, operation),
      ]);
      scheduleNext();
    };

    if (reducedMotion || !documentVisible) {
      currentIndex = normalizedIndex;
      title.textContent = slide.title;
      description.textContent = slide.description;
      setPhoto(slide, false, operation);
      updateCounter();
      if (manual && announcement) announcement.textContent = `${slide.name}. ${slide.description}`;
      scheduleNext();
      return;
    }

    if (image) image.classList.add('is-fading');
    Promise.all([
      eraseText(title, ERASE_MS, operation),
      eraseText(description, ERASE_MS, operation),
      new Promise((resolve) => window.setTimeout(resolve, IMAGE_FADE_MS)),
    ]).then(applySlide);

  }

  function updateMotionControls() {
    if (!controls || slides.length < 2) return;
    controls.hidden = false;
    if (pauseButton) {
      pauseButton.hidden = reducedMotion;
      pauseButton.setAttribute('aria-pressed', String(userPaused));
      pauseButton.textContent = userPaused ? 'Resume rotation' : 'Pause rotation';
    }
    if (controlDivider) controlDivider.hidden = reducedMotion;
  }

  if (controls && slides.length > 1) {
    controls.hidden = false;
    updateCounter();
    updateMotionControls();
  }

  if (slides.length > 1) {
    previousButton?.addEventListener('click', () => {
      changeSlide((currentIndex - 1 + slides.length) % slides.length, { manual: true });
    });
    nextButton?.addEventListener('click', () => {
      changeSlide((currentIndex + 1) % slides.length, { manual: true });
    });
    pauseButton?.addEventListener('click', () => {
      userPaused = !userPaused;
      updateMotionControls();
      if (userPaused) clearCycleTimer();
      else scheduleNext();
    });
  }

  function finishOrAnimateInitialCopy() {
    if (!title || !description) return;
    if (reducedMotion || !documentVisible) {
      settleCurrentSlide();
      return;
    }
    const operation = ++operationId;
    const activeSlide = currentIndex >= 0 ? slides[currentIndex] : null;
    const fullTitle = activeSlide ? activeSlide.title : initialTitle;
    const fullDescription = activeSlide ? activeSlide.description : initialDescription;
    title.textContent = '';
    description.textContent = '';
    Promise.all([
      typeText(title, fullTitle, TITLE_TYPE_MS, operation),
      typeText(description, fullDescription, DESCRIPTION_TYPE_MS, operation),
    ]);
  }

  finishOrAnimateInitialCopy();
  scheduleNext();

  document.addEventListener('visibilitychange', () => {
    documentVisible = document.visibilityState !== 'hidden';
    if (!documentVisible) {
      clearCycleTimer();
      settleCurrentSlide();
    } else {
      scheduleNext();
    }
    syncPaymentOrbit();
  });

  if (hero && 'IntersectionObserver' in window) {
    const heroObserver = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        const nowVisible = entry.isIntersecting && entry.intersectionRatio > 0.06;
        if (nowVisible === heroVisible) return;
        heroVisible = nowVisible;
        if (!heroVisible) {
          clearCycleTimer();
          settleCurrentSlide();
        } else {
          scheduleNext();
        }
      });
    }, { threshold: [0, 0.06, 0.2] });
    heroObserver.observe(hero);
  }

  function handleMotionPreference(event) {
    reducedMotion = Boolean(event.matches) || document.documentElement.classList.contains('experience-motion-off');
    updateMotionControls();
    if (reducedMotion) {
      body.classList.remove('motion-ready');
      clearCycleTimer();
      settleCurrentSlide();
    } else {
      enableScrollReveal();
      syncPaymentOrbit();
      scheduleNext();
    }
  }

  window.addEventListener('experience-motion-change', event => handleMotionPreference({matches:event.detail.reduced}));

  if (typeof motionPreference.addEventListener === 'function') {
    motionPreference.addEventListener('change', handleMotionPreference);
  } else if (typeof motionPreference.addListener === 'function') {
    motionPreference.addListener(handleMotionPreference);
  }

  if (paymentSection && 'IntersectionObserver' in window) {
    paymentObserver = new IntersectionObserver(syncPaymentOrbit, { threshold: [0, 0.01, 0.2] });
    paymentObserver.observe(paymentSection);
  }

  const revealTargets = Array.from(document.querySelectorAll('[data-reveal]'));
  function enableScrollReveal() {
    if (!('IntersectionObserver' in window) || reducedMotion || !revealTargets.length) return;
    if (!revealObserver) {
      revealObserver = new IntersectionObserver((entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.classList.add('is-visible');
          } else {
            entry.target.dataset.motionDirection = entry.boundingClientRect.top < 0 ? 'up' : 'down';
            entry.target.classList.remove('is-visible');
          }
        });
      }, { threshold: 0.12, rootMargin: '0px 0px -6% 0px' });
      revealTargets.forEach((target) => revealObserver.observe(target));
    }
    body.classList.add('motion-ready');
  }

  if (!reducedMotion) enableScrollReveal();

  if (!('IntersectionObserver' in window)) {
    const syncFallbackVisibility = () => {
      if (visibilityFrame) return;
      visibilityFrame = window.requestAnimationFrame(() => {
        visibilityFrame = 0;
        syncHeroVisibility();
        syncPaymentOrbit();
      });
    };
    window.addEventListener('scroll', syncFallbackVisibility, { passive: true });
    window.addEventListener('resize', syncFallbackVisibility);
    syncFallbackVisibility();
  }

  if (menuButton && navigation) {
    body.classList.add('landing-nav-ready');
    const closeNavigation = ({ returnFocus = false } = {}) => {
      navigation.classList.remove('is-open');
      menuButton.setAttribute('aria-expanded', 'false');
      menuButton.setAttribute('aria-label', 'Open navigation');
      if (returnFocus) menuButton.focus();
    };

    menuButton.addEventListener('click', () => {
      const willOpen = menuButton.getAttribute('aria-expanded') !== 'true';
      menuButton.setAttribute('aria-expanded', String(willOpen));
      menuButton.setAttribute('aria-label', willOpen ? 'Close navigation' : 'Open navigation');
      navigation.classList.toggle('is-open', willOpen);
    });

    navigation.addEventListener('click', (event) => {
      if (event.target.closest('a')) closeNavigation();
    });

    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && navigation.classList.contains('is-open')) {
        closeNavigation({ returnFocus: true });
      }
    });

    document.addEventListener('click', (event) => {
      if (!navigation.classList.contains('is-open')) return;
      if (!navigation.contains(event.target) && !menuButton.contains(event.target)) closeNavigation();
    });
  }
})();
