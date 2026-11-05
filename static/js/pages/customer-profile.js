document.addEventListener('DOMContentLoaded', () => {
  const dialog = document.querySelector('[data-notification-dialog]');
  const openButton = document.querySelector('[data-notification-dialog-open]');
  if (!dialog || !openButton) return;

  const closeButtons = dialog.querySelectorAll('[data-notification-dialog-close]');
  const setExpanded = (expanded) => openButton.setAttribute('aria-expanded', String(expanded));

  const openDialog = () => {
    if (typeof dialog.showModal === 'function') {
      if (!dialog.open) dialog.showModal();
    } else {
      dialog.setAttribute('open', '');
      dialog.classList.add('is-open');
    }
    setExpanded(true);
  };

  const closeDialog = () => {
    if (typeof dialog.close === 'function' && dialog.open) {
      dialog.close();
    } else {
      dialog.removeAttribute('open');
      dialog.classList.remove('is-open');
      setExpanded(false);
      openButton.focus();
    }
  };

  openButton.addEventListener('click', openDialog);
  closeButtons.forEach((button) => button.addEventListener('click', closeDialog));
  dialog.addEventListener('close', () => {
    dialog.classList.remove('is-open');
    setExpanded(false);
    openButton.focus();
  });
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) closeDialog();
  });
});
