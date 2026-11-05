/* Offer draft preservation.
 *
 * A failed send must never cost the artisan their work. Every field of an
 * itemised offer is mirrored into localStorage as it is typed, restored when the
 * page comes back — reload, back navigation, offline retry — and cleared only
 * after the server confirms the offer was actually created. The idempotency key
 * travels with the form, so a retry can never create a second offer.
 */
(function () {
  'use strict';

  var PREFIX = 'artisan.offer-draft:';

  function forms() {
    return Array.prototype.slice.call(document.querySelectorAll('form[data-draft-key]'));
  }

  function readDraft(key) {
    try {
      var raw = window.localStorage.getItem(PREFIX + key);
      return raw ? JSON.parse(raw) : null;
    } catch (_) {
      return null;
    }
  }

  function writeDraft(key, data) {
    try {
      window.localStorage.setItem(PREFIX + key, JSON.stringify(data));
    } catch (_) {
      /* private mode or quota — the server-side draft context still covers retries */
    }
  }

  function clearDraft(key) {
    try {
      window.localStorage.removeItem(PREFIX + key);
      window.sessionStorage.removeItem(PREFIX + 'submitted:' + key);
    } catch (_) {
      /* nothing to clear */
    }
  }

  function serialize(form) {
    var data = {};
    Array.prototype.forEach.call(form.elements, function (field) {
      if (!field.name) return;
      if (field.type === 'checkbox') {
        data[field.name] = field.checked;
      } else if (field.type !== 'submit' && field.type !== 'button') {
        data[field.name] = field.value;
      }
    });
    return data;
  }

  function applyDraft(form, data) {
    Object.keys(data).forEach(function (name) {
      var field = form.elements[name];
      if (!field) return;
      if (field instanceof RadioNodeList || (field.length && !field.tagName)) return;
      if (field.type === 'checkbox') {
        field.checked = !!data[name];
      } else if (data[name] !== null && data[name] !== undefined) {
        field.value = data[name];
      }
    });
  }

  function hasVisibleErrors() {
    return !!document.querySelector('[data-offer-error-panel]');
  }

  function markInvalidFields() {
    var node = document.getElementById('offer-field-errors');
    if (!node) return;
    var errors = {};
    try {
      errors = JSON.parse(node.textContent) || {};
    } catch (_) {
      return;
    }
    Object.keys(errors).forEach(function (name) {
      var field = document.querySelector('[name="' + name.replace(/"/g, '') + '"]');
      if (!field) return;
      field.setAttribute('aria-invalid', 'true');
      field.classList.add('field-invalid');
      var message = document.createElement('p');
      message.className = 'field-error';
      message.textContent = errors[name];
      if (field.parentNode) field.parentNode.appendChild(message);
    });
  }

  function init(form) {
    if (form.dataset.draftInit === '1') return;
    form.dataset.draftInit = '1';
    var key = form.getAttribute('data-draft-key');
    if (!key) return;

    var submittedKey = PREFIX + 'submitted:' + key;
    var wasSubmitted = false;
    try {
      wasSubmitted = !!window.sessionStorage.getItem(submittedKey);
    } catch (_) {
      wasSubmitted = false;
    }
    if (wasSubmitted && !hasVisibleErrors()) {
      // The previous send reached the server; this is a fresh offer form.
      clearDraft(key);
    } else {
      var stored = readDraft(key);
      var prefill = null;
      if (!stored && form.getAttribute('data-draft-prefill')) {
        try {
          prefill = JSON.parse(form.getAttribute('data-draft-prefill'));
        } catch (_) {
          prefill = null;
        }
      }
      if (stored) applyDraft(form, stored);
      else if (prefill) {
        // Counter offers start from the current quote, minus what does not
        // belong on a resend.
        if (prefill.labour_amount) form.elements.labour_amount.value = prefill.labour_amount;
        if (prefill.transport_amount) form.elements.transport_amount.value = prefill.transport_amount;
        if (prefill.additional_amount) form.elements.additional_amount.value = prefill.additional_amount;
        if (prefill.additional_charge_details) form.elements.additional_charge_details.value = prefill.additional_charge_details;
        if (prefill.scheduled_start) form.elements.scheduled_start.value = prefill.scheduled_start;
        if (prefill.duration_value) form.elements.duration_value.value = prefill.duration_value;
        if (prefill.duration_unit) form.elements.duration_unit.value = prefill.duration_unit;
        if (prefill.materials_included && form.elements.materials_included) {
          form.elements.materials_included.checked = true;
        }
        if (Array.isArray(prefill.materials) && prefill.materials.length && form.elements.materials_json) {
          form.elements.materials_json.value = JSON.stringify(prefill.materials);
        }
      }
    }

    markInvalidFields();

    form.addEventListener('input', function () { writeDraft(key, serialize(form)); });
    form.addEventListener('change', function () { writeDraft(key, serialize(form)); });
    form.addEventListener('submit', function () {
      writeDraft(key, serialize(form));
      try {
        window.sessionStorage.setItem(submittedKey, String(Date.now()));
      } catch (_) {
        /* the draft is still on disk; nothing else to do */
      }
    });
  }

  function boot() {
    forms().forEach(init);
  }

  boot();
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  }
})();
