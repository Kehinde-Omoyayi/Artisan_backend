/* Booking event popups.
 *
 * Decisions (a counter offer, a booking that can now be paid, a failed payment)
 * open a modal with the action inside. Plain confirmations become a toast that
 * disappears on its own. Either way the event is acknowledged once, on the
 * server, so a refresh never shows it again while it stays in the notification
 * list and in the booking history.
 */
(function () {
  'use strict';

  var host = document.querySelector('[data-booking-events]');
  var toastStack = document.querySelector('[data-booking-toasts]');
  if (!host || !toastStack) return;

  var endpoint = host.getAttribute('data-popups-url') || '/notifications/popups/';
  var ackTemplate = host.getAttribute('data-ack-url') || '/notifications/0/acknowledge/';
  var acknowledged = {};
  var shown = {};

  function csrfToken() {
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : '';
  }

  function acknowledge(id) {
    if (!id || acknowledged[id]) return Promise.resolve();
    acknowledged[id] = true;
    var url = ackTemplate.replace(/0\/acknowledge\/$/, encodeURIComponent(id) + '/acknowledge/');
    return fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'X-CSRFToken': csrfToken(),
        'Accept': 'application/json'
      }
    }).catch(function () {
      // The server keeps it unread; the next page load simply tries again.
      delete acknowledged[id];
    });
  }

  function details(notification) {
    var data = notification && notification.data ? notification.data : {};
    return {
      title: notification.title || 'Booking update',
      message: notification.message || '',
      actionUrl: data.action_url || '',
      actionLabel: data.action_label || 'Open',
      popup: data.popup === 'modal' ? 'modal' : 'toast',
      eventKey: notification.event_key || '',
      offerId: data.offer_id || '',
      conversationId: data.conversation_id || '',
      offerAmount: data.offer_amount || '',
      id: notification.id
    };
  }

  function canAcceptInline(item) {
    return isOfferDecision(item) && !!item.offerId && !!item.conversationId
      && /^(counter_offer_received|offer_received)/.test(item.eventKey);
  }

  function csrfFromMeta() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') || '' : '';
  }

  /* Accepting an offer straight from the popup: the decision lives inside the
     modal, and the same server endpoint validates it as the full page would. */
  function offerAcceptForm(item) {
    var form = document.createElement('form');
    form.method = 'post';
    form.className = 'booking-event-inline-action';
    form.action = '/discover/conversations/' + encodeURIComponent(item.conversationId) + '/';

    function hidden(name, value) {
      var input = document.createElement('input');
      input.type = 'hidden';
      input.name = name;
      input.value = value;
      form.appendChild(input);
    }
    hidden('csrfmiddlewaretoken', csrfFromMeta());
    hidden('action', 'accept');
    hidden('offer_id', item.offerId);

    var submit = document.createElement('button');
    submit.type = 'submit';
    submit.className = 'button button-primary';
    submit.textContent = item.offerAmount
      ? 'Accept offer · ₦' + item.offerAmount
      : 'Accept offer';
    form.appendChild(submit);
    return form;
  }

  function isOfferDecision(item) {
    return /^(counter_offer_received|offer_received|offer_revision_requested)/.test(item.eventKey);
  }

  function isPaymentDecision(item) {
    return /^(booking_accepted|payment_initialized|payment_failed)/.test(item.eventKey);
  }

  function buildModal(item) {
    var dialog = document.createElement('dialog');
    dialog.className = 'booking-event-dialog';
    dialog.setAttribute('aria-labelledby', 'booking-event-title');

    var card = document.createElement('section');
    card.className = 'booking-event-card';

    var eyebrow = document.createElement('p');
    eyebrow.className = 'booking-event-eyebrow';
    eyebrow.textContent = isOfferDecision(item) ? 'Offer' : isPaymentDecision(item) ? 'Payment' : 'Booking';

    var title = document.createElement('h2');
    title.id = 'booking-event-title';
    title.textContent = item.title;

    var body = document.createElement('p');
    body.className = 'booking-event-message';
    body.textContent = item.message;

    var next = document.createElement('p');
    next.className = 'booking-event-next';
    next.textContent = canAcceptInline(item)
      ? 'Accepting books the work at this price. Review first if you want the full quote.'
      : isOfferDecision(item)
        ? 'Open the offer to accept it, decline it, or ask for a change.'
        : 'Open the booking to continue. Nothing is charged without your confirmation.';

    var actions = document.createElement('div');
    actions.className = 'booking-event-actions';

    var dismiss = document.createElement('button');
    dismiss.type = 'button';
    dismiss.className = 'button button-secondary';
    dismiss.textContent = 'Later';

    actions.appendChild(dismiss);

    var canAccept = canAcceptInline(item);
    if (canAccept) {
      var acceptForm = offerAcceptForm(item);
      acceptForm.classList.add('booking-event-accept');
      actions.appendChild(acceptForm);
    }

    var open = document.createElement('a');
    open.className = canAccept ? 'button button-secondary' : 'button button-primary';
    open.href = item.actionUrl || '/';
    open.textContent = canAccept ? 'Review first' : item.actionLabel;

    actions.appendChild(open);

    card.appendChild(eyebrow);
    card.appendChild(title);
    card.appendChild(body);
    card.appendChild(next);
    card.appendChild(actions);
    dialog.appendChild(card);

    function close() {
      if (dialog.open) dialog.close();
      if (dialog.parentNode) dialog.parentNode.removeChild(dialog);
    }

    dismiss.addEventListener('click', function () {
      acknowledge(item.id);
      close();
    });
    open.addEventListener('click', function () {
      // Marked read before navigation so it never re-opens on the next page.
      acknowledge(item.id);
    });
    dialog.addEventListener('cancel', function (event) {
      event.preventDefault();
      acknowledge(item.id);
      close();
    });
    return { dialog: dialog, close: close };
  }

  function showToast(item) {
    var toast = document.createElement('div');
    toast.className = 'booking-toast';
    toast.setAttribute('role', 'status');

    var title = document.createElement('strong');
    title.textContent = item.title;
    var message = document.createElement('span');
    message.textContent = item.message;

    toast.appendChild(title);
    toast.appendChild(message);

    if (item.actionUrl) {
      var link = document.createElement('a');
      link.href = item.actionUrl;
      link.textContent = item.actionLabel;
      link.addEventListener('click', function () { acknowledge(item.id); });
      toast.appendChild(link);
    }

    toastStack.appendChild(toast);
    acknowledge(item.id);
    window.setTimeout(function () {
      toast.classList.add('is-leaving');
      window.setTimeout(function () {
        if (toast.parentNode) toast.parentNode.removeChild(toast);
      }, 260);
    }, 6000);
  }

  var modalQueue = [];
  var pendingToasts = [];
  var modalOpen = false;

  function flushToasts() {
    if (modalOpen || modalQueue.length) return;
    var queue = pendingToasts;
    pendingToasts = [];
    queue.forEach(showToast);
  }

  function queueToast(item) {
    pendingToasts.push(item);
    flushToasts();
  }

  function nextModal() {
    if (modalOpen) return;
    var item = modalQueue.shift();
    if (!item) {
      flushToasts();
      return;
    }
    modalOpen = true;
    var ui = buildModal(item);
    document.body.appendChild(ui.dialog);
    if (typeof ui.dialog.showModal === 'function') ui.dialog.showModal();
    else ui.dialog.setAttribute('open', 'open');
    var settled = false;
    var finish = function () {
      if (settled) return;
      settled = true;
      modalOpen = false;
      if (ui.dialog.parentNode) ui.dialog.parentNode.removeChild(ui.dialog);
      nextModal();
    };
    ui.dialog.addEventListener('close', finish);
    var observer = new MutationObserver(function () {
      if (!ui.dialog.isConnected) { observer.disconnect(); finish(); }
    });
    observer.observe(document.body, { childList: true });
  }

  function render(list) {
    (list || []).forEach(function (notification) {
      var item = details(notification);
      if (acknowledged[item.id] || shown[item.id]) return;
      shown[item.id] = true;
      if (item.popup === 'modal') { modalQueue.push(item); }
      else { queueToast(item); }
    });
    nextModal();
  }

  function load() {
    return fetch(endpoint, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function (response) { return response.ok ? response.json() : []; })
      .then(render)
      .catch(function () { /* no popups is not an error */ });
  }

  /* A quote that arrives while the other person is sitting in the chat must
     appear on its own -- waiting for a page refresh is not "immediate". The
     feed is re-read on a short timer, and at once when the tab comes back, so
     both the artisan and the customer see the itemised request without doing
     anything. Nothing is ever shown twice in the same visit. */
  var POLL_MS = 8000;
  var pollTimer = null;

  function schedule() {
    window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(tick, POLL_MS);
  }

  function refresh() {
    window.clearTimeout(pollTimer);
    return load().then(schedule, schedule);
  }

  function tick() {
    if (document.visibilityState === 'hidden') {
      schedule();  /* a background tab asks again later; it never burns requests */
      return;
    }
    refresh();
  }

  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') refresh();
  });
  window.addEventListener('focus', function () {
    if (!modalOpen) refresh();
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', refresh);
  } else {
    refresh();
  }
})();
