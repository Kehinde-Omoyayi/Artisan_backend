(() => {
  'use strict';

  const endpoint = '/system/promotions/performance-events/';
  const visitorStorageKey = 'artisan-promotion-visitor-v1';
  const csrfMeta = document.querySelector('meta[name="csrf-token"]');
  const csrfToken = csrfMeta ? csrfMeta.getAttribute('content') || '' : '';
  const sentThisPage = new Set();

  function visitorId() {
    try {
      const existing = window.sessionStorage.getItem(visitorStorageKey);
      if (existing && /^[A-Za-z0-9_-]{24,180}$/.test(existing)) return existing;
      const bytes = new Uint8Array(24);
      if (window.crypto && typeof window.crypto.getRandomValues === 'function') {
        window.crypto.getRandomValues(bytes);
      } else {
        for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
      }
      const binary = Array.from(bytes, (value) => String.fromCharCode(value)).join('');
      const key = window.btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
      window.sessionStorage.setItem(visitorStorageKey, key);
      return key;
    } catch (_error) {
      return `tab-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
    }
  }

  function submitEvent(token, eventType, artisanId) {
    if (!token || sentThisPage.has(`${eventType}:${token.slice(-48)}`)) return;
    sentThisPage.add(`${eventType}:${token.slice(-48)}`);
    const payload = {
      token,
      visitor_id: visitorId(),
      event_type: eventType,
    };
    if (artisanId) payload.artisan_id = artisanId;
    fetch(endpoint, {
      method: 'POST',
      credentials: 'same-origin',
      keepalive: true,
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'X-CSRFToken': csrfToken,
      },
      body: JSON.stringify(payload),
    }).catch(() => {});
  }

  function initPromotionTracking() {
    const cards = Array.from(document.querySelectorAll('[data-promotion-impression-token]'));
    const profile = document.querySelector('[data-profile-promotion-token]');
    if (profile) {
      submitEvent(
        profile.getAttribute('data-profile-promotion-token') || '',
        'PROFILE_VISIT',
        profile.getAttribute('data-profile-artisan-id') || '',
      );
    }
    if (!cards.length) return;

    if ('IntersectionObserver' in window) {
      const observer = new IntersectionObserver((entries) => {
        if (document.visibilityState === 'hidden') return;
        entries.forEach((entry) => {
          if (!entry.isIntersecting || entry.intersectionRatio < 0.5) return;
          const card = entry.target;
          observer.unobserve(card);
          submitEvent(
            card.getAttribute('data-promotion-impression-token') || '',
            'IMPRESSION',
            card.getAttribute('data-promotion-artisan-id') || '',
          );
        });
      }, { threshold: [0.5] });
      cards.forEach((card) => observer.observe(card));
      return;
    }

    const visibleEnough = (element) => {
      const rect = element.getBoundingClientRect();
      const height = Math.max(0, Math.min(rect.bottom, window.innerHeight) - Math.max(rect.top, 0));
      const width = Math.max(0, Math.min(rect.right, window.innerWidth) - Math.max(rect.left, 0));
      return height * width >= rect.width * rect.height * 0.5;
    };
    const checkVisibleCards = () => {
      if (document.visibilityState === 'hidden') return;
      cards.forEach((card) => {
        if (card.dataset.promotionImpressionSent || !visibleEnough(card)) return;
        card.dataset.promotionImpressionSent = 'true';
        submitEvent(
          card.getAttribute('data-promotion-impression-token') || '',
          'IMPRESSION',
          card.getAttribute('data-promotion-artisan-id') || '',
        );
      });
    };
    window.addEventListener('scroll', checkVisibleCards, { passive: true });
    window.addEventListener('resize', checkVisibleCards, { passive: true });
    checkVisibleCards();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initPromotionTracking, { once: true });
  } else {
    initPromotionTracking();
  }
})();
