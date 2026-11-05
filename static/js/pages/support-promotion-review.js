(() => {
  const form = document.querySelector('.support-promotion-decision-form');
  if (!form) return;
  form.addEventListener('submit', (event) => {
    const decision = form.querySelector('[name="decision"]')?.value;
    if (decision === 'APPROVED' && !window.confirm('Approve only this promotion product and target? This will not activate a campaign or process payment.')) {
      event.preventDefault();
    }
  });
})();
