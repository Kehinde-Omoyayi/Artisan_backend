document.addEventListener('DOMContentLoaded', () => {
  const openButtons = Array.from(document.querySelectorAll('[data-notification-dialog-open]'));
  const dialogs = new Map();

  openButtons.forEach((openButton) => {
    const dialogId = openButton.getAttribute('aria-controls');
    const dialog = dialogId
      ? document.getElementById(dialogId)
      : document.querySelector('[data-notification-dialog]');
    if (!dialog) return;

    if (!dialogs.has(dialog)) dialogs.set(dialog, []);
    dialogs.get(dialog).push(openButton);
  });

  dialogs.forEach((buttons, dialog) => {
    let activeOpener = null;
    const closeButtons = dialog.querySelectorAll('[data-notification-dialog-close]');

    const resetOpeners = () => {
      buttons.forEach((button) => button.setAttribute('aria-expanded', 'false'));
      const opener = activeOpener;
      activeOpener = null;
      if (opener) opener.focus();
    };

    const openDialog = (openButton) => {
      activeOpener = openButton;
      openButton.setAttribute('aria-expanded', 'true');
      if (typeof dialog.showModal === 'function') {
        if (!dialog.open) dialog.showModal();
      } else {
        dialog.setAttribute('open', '');
        dialog.classList.add('is-open');
      }
    };

    const closeDialog = () => {
      if (typeof dialog.close === 'function' && dialog.open) {
        dialog.close();
      } else {
        dialog.removeAttribute('open');
        dialog.classList.remove('is-open');
        resetOpeners();
      }
    };

    buttons.forEach((openButton) => {
      openButton.addEventListener('click', () => openDialog(openButton));
    });
    closeButtons.forEach((button) => button.addEventListener('click', closeDialog));
    dialog.addEventListener('close', () => {
      dialog.classList.remove('is-open');
      resetOpeners();
    });
    dialog.addEventListener('click', (event) => {
      if (event.target === dialog) closeDialog();
    });
  });
});
