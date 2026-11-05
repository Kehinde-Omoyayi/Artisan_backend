/* Itemized offer panel: manages material line rows, recomputes the
 * quote summary client-side (display only — the backend re-derives every
 * amount server-side), and renders the live booking countdown.
 */
(function () {
  'use strict';

  var money = function (value) {
    var n = Number(String(value == null ? '' : value).replace(/,/g, ''));
    return Number.isFinite(n) ? n : 0;
  };
  var fmt = function (n) {
    try {
      return new Intl.NumberFormat('en-NG', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n);
    } catch (_) {
      return n.toFixed(2);
    }
  };
  var text = function (node, value) { if (node) node.textContent = value; };

  function rowTemplate(index) {
    var tr = document.createElement('tr');
    tr.innerHTML =
      '<td data-label="Item"><input class="field-input" type="text" data-material-name maxlength="120" placeholder="Item name" required></td>' +
      '<td data-label="Qty"><input class="field-input" type="number" data-material-qty min="0.01" step="0.01" value="1" required></td>' +
      '<td data-label="Unit"><input class="field-input" type="text" data-material-unit maxlength="40" placeholder="e.g. bags"></td>' +
      '<td data-label="Unit price (₦)"><input class="field-input" type="number" data-material-price min="0" step="0.01" value="0" required></td>' +
      '<td data-label="Line total (₦)"><span class="material-line-value">₦<span data-material-line>0.00</span></span></td>' +
      '<td data-label="Remove"><button class="button button-secondary" type="button" data-material-remove aria-label="Remove material item">×</button></td>';
    return tr;
  }

  function initPanel(panel) {
    if (panel.dataset.offerInit === '1') return;
    panel.dataset.offerInit = '1';
    var form = panel.closest('form');
    if (!form) return;
    var rowsHost = panel.querySelector('[data-materials-rows]');
    var jsonInput = panel.querySelector('[data-materials-json]');
    var amountInput = panel.querySelector('[data-materials-amount]');
    var subtotalNode = panel.querySelector('[data-materials-subtotal]');
    var addButton = panel.querySelector('[data-materials-add]');
    var labourInput = form.querySelector('[data-offer-labour]');
    var transportInput = form.querySelector('[data-offer-transport]');
    var additionalInput = form.querySelector('[data-offer-additional]');
    var summary = form.querySelector('[data-offer-summary]') || {};
    var totalInput = form.querySelector('[data-money-total]');
    var submitButton = form.querySelector('[data-offer-submit]');
    var includedInput = form.querySelector('[data-materials-included]');
    var reviewStatus = form.querySelector('[data-offer-review-status]');

    function rows() {
      return Array.from(rowsHost ? rowsHost.querySelectorAll('tr') : []);
    }

    function collect() {
      return rows().map(function (tr) {
        return {
          name: (tr.querySelector('[data-material-name]').value || '').trim(),
          quantity: tr.querySelector('[data-material-qty]').value || '0',
          unit: (tr.querySelector('[data-material-unit]').value || '').trim(),
          unit_price: tr.querySelector('[data-material-price]').value || '0'
        };
      }).filter(function (item) { return item.name !== ''; });
    }

    function sync() {
      var subtotal = 0;
      rows().forEach(function (tr) {
        var line = money(tr.querySelector('[data-material-qty]').value) * money(tr.querySelector('[data-material-price]').value);
        text(tr.querySelector('[data-material-line]'), fmt(line));
        subtotal += line;
      });
      text(subtotalNode, fmt(subtotal));
      if (jsonInput) jsonInput.value = JSON.stringify(collect());
      if (amountInput) amountInput.value = subtotal.toFixed(2);

      var labour = money(labourInput && labourInput.value);
      var transport = money(transportInput && transportInput.value);
      var additional = money(additionalInput && additionalInput.value);
      // When the materials are included in the price the itemised subtotal is
      // disclosure only: it is never charged a second time.
      var included = !!(includedInput && includedInput.checked);
      var total = labour + transport + additional + (included ? 0 : subtotal);
      if (totalInput) { totalInput.value = total.toFixed(2); totalInput.dataset.manual = 'false'; }
      text(summary.querySelector ? summary.querySelector('[data-summary-labour]') : null, '₦' + fmt(labour));
      text(summary.querySelector ? summary.querySelector('[data-summary-materials]') : null, '₦' + fmt(subtotal) + (included ? ' (included)' : ''));
      text(summary.querySelector ? summary.querySelector('[data-summary-transport]') : null, '₦' + fmt(transport));
      text(summary.querySelector ? summary.querySelector('[data-summary-additional]') : null, '₦' + fmt(additional));
      text(summary.querySelector ? summary.querySelector('[data-summary-total]') : null, '₦' + fmt(total));

      if (submitButton) {
        var ready = labour > 0 && total > 0;
        submitButton.disabled = !ready;
        if (reviewStatus) {
          reviewStatus.textContent = ready
            ? 'Review the payable total, materials list, start date and duration, then send.'
            : 'Labour and a payable total above zero are required before the offer can be sent.';
        }
      }
    }

    function addRow(item) {
      var tr = rowTemplate(rows().length);
      rowsHost.appendChild(tr);
      if (item) {
        tr.querySelector('[data-material-name]').value = item.name || '';
        tr.querySelector('[data-material-qty]').value = item.quantity || '1';
        tr.querySelector('[data-material-unit]').value = item.unit || '';
        tr.querySelector('[data-material-price]').value = item.unit_price || '0';
      }
      tr.querySelector('[data-material-remove]').addEventListener('click', function () {
        tr.remove();
        sync();
      });
      tr.addEventListener('input', sync);
      return tr;
    }

    if (addButton) addButton.addEventListener('click', function () { addRow(null); sync(); });
    if (includedInput) includedInput.addEventListener('change', sync);

    // Prefill rows from a server-prefilled materials source (counter offers).
    var sourceId = panel.getAttribute('data-materials-source');
    var initial = [];
    if (sourceId) {
      try { initial = JSON.parse((document.getElementById(sourceId) || {}).textContent || '[]') || []; } catch (_) { initial = []; }
    } else if (jsonInput && jsonInput.value && jsonInput.value !== '[]') {
      try { initial = JSON.parse(jsonInput.value) || []; } catch (_) { initial = []; }
    }
    if (Array.isArray(initial)) initial.forEach(function (item) { if (item && typeof item === 'object') addRow(item); });

    form.addEventListener('input', function (event) {
      if (event.target && (event.target.matches('[data-offer-labour]') || event.target.matches('[data-offer-transport]') || event.target.matches('[data-offer-additional]'))) {
        sync();
      }
    });
    sync();
  }

  function initCountdown(node) {
    if (node.dataset.countdownInit === '1') return;
    node.dataset.countdownInit = '1';
    var endAttr = node.getAttribute('data-countdown-end') || '';
    var startAttr = node.getAttribute('data-countdown-start') || '';
    var valueNode = node.querySelector('[data-countdown-value]');
    var noteNode = node.querySelector('[data-countdown-note]');
    var label = node.getAttribute('data-duration-label') || '';
    var end = endAttr ? new Date(endAttr) : null;
    var start = startAttr ? new Date(startAttr) : null;
    if (end && isNaN(end.getTime())) end = null;
    if (start && isNaN(start.getTime())) start = null;

    if (!end) {
      if (!label && noteNode) noteNode.textContent = 'No agreed duration was recorded; track progress with the artisan before completion.';
      if (valueNode) valueNode.textContent = '';
      return; // Nothing to count — honest state only.
    }

    function tick() {
      var now = Date.now();
      var elapsed = start ? now - start.getTime() : 0;
      var remaining = end.getTime() - now;
      var fmtSpan = function (ms) {
        var totalMin = Math.floor(Math.abs(ms) / 60000);
        var d = Math.floor(totalMin / 1440);
        var h = Math.floor((totalMin % 1440) / 60);
        var m = totalMin % 60;
        return (d ? d + 'd ' : '') + h + 'h ' + m + 'm';
      };
      if (remaining >= 0) {
        text(valueNode, fmtSpan(remaining) + ' left');
        node.classList.remove('countdown-over');
      } else {
        text(valueNode, 'overtime · ' + fmtSpan(remaining) + ' past agreed duration');
        node.classList.add('countdown-over');
      }
      if (noteNode && label) noteNode.textContent = 'Agreed duration: ' + label + (start ? ' · started ' + start.toLocaleString() : '');
    }
    tick();
    window.setInterval(tick, 30000);
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-materials-panel]').forEach(initPanel);
    document.querySelectorAll('[data-countdown]').forEach(initCountdown);
  });
  if (document.readyState !== 'loading') {
    document.querySelectorAll('[data-materials-panel]').forEach(initPanel);
    document.querySelectorAll('[data-countdown]').forEach(initCountdown);
  }
})();
