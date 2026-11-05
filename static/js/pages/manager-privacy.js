(() => {
  const hideButton = document.querySelector('[data-manager-privacy-hide]');
  if (!hideButton) return;

  const hidePage = () => {
    hideButton.disabled = true;
    const privateWorkspace = document.querySelector(
      '.manager-dashboard, .support-promotion-management'
    );
    if (privateWorkspace) {
      privateWorkspace.replaceChildren();
      privateWorkspace.setAttribute('aria-busy', 'true');
    }
    window.location.replace(window.location.pathname);
  };

  const hideForm = hideButton.closest('form');
  if (hideForm) {
    hideForm.addEventListener('submit', (event) => {
      event.preventDefault();
      hidePage();
    });
  } else {
    hideButton.addEventListener('click', hidePage);
  }
  window.addEventListener('pageshow', (event) => {
    if (event.persisted && document.querySelector('[data-manager-privacy-hide]')) {
      hidePage();
    }
  });
})();
