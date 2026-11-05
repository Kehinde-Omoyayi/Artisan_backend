document.addEventListener('DOMContentLoaded', () => {
  const form = document.querySelector('[data-support-message-form]');
  if (!form) return;

  const textarea = form.querySelector('textarea[name="message"]');
  const counter = document.getElementById('case-message-count');
  const status = document.getElementById('support-submit-status');
  const submit = form.querySelector('button[type="submit"]');

  const updateCount = () => {
    if (!textarea || !counter) return;
    counter.textContent = `${textarea.value.length.toLocaleString()} / 5,000`;
  };

  updateCount();
  if (textarea) textarea.addEventListener('input', updateCount);

  form.addEventListener('submit', (event) => {
    if (!textarea || textarea.value.trim().length < 2) {
      event.preventDefault();
      if (status) status.textContent = 'Add a short message before sending.';
      if (textarea) textarea.focus();
      return;
    }
    if (submit) {
      submit.disabled = true;
      submit.querySelector('span:first-child').textContent = 'Sending…';
    }
    if (status) status.textContent = 'Sending your message to customer support…';
  });
});
