document.addEventListener('DOMContentLoaded', () => {
  const paymentSelect = document.querySelector('[data-support-payment-select]');
  const amountInput = document.querySelector('[data-support-refund-amount]');
  const balanceNote = document.querySelector('[data-payment-balance-note]');

  if (!paymentSelect || !amountInput) return;

  const updateRefundLimit = () => {
    const option = paymentSelect.selectedOptions && paymentSelect.selectedOptions[0];
    const remaining = option && option.dataset.remaining;
    if (!remaining) {
      amountInput.removeAttribute('max');
      if (balanceNote) balanceNote.textContent = 'Choose a payment to see its currently refundable balance.';
      return;
    }

    amountInput.max = remaining;
    if (balanceNote) {
      const parsed = Number(remaining);
      const formatted = Number.isFinite(parsed)
        ? new Intl.NumberFormat('en-NG', { style: 'currency', currency: 'NGN', maximumFractionDigits: 2 }).format(parsed)
        : `₦${remaining}`;
      balanceNote.textContent = `Currently refundable balance: ${formatted}. The API will recheck the available balance when submitted.`;
    }
  };

  paymentSelect.addEventListener('change', updateRefundLimit);
  updateRefundLimit();
});
