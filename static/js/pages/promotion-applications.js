(() => {
  const form = document.querySelector('[data-promotion-form]');
  if (!form) return;

  const product = form.querySelector('[data-promotion-type]');
  const groups = Array.from(form.querySelectorAll('[data-promotion-group]'));
  const country = form.querySelector('[data-promotion-country]');
  const state = form.querySelector('[data-promotion-state]');
  const pairNode = document.getElementById('promotion-state-pairs');
  let pairs = [];
  try {
    pairs = JSON.parse(pairNode?.textContent || '[]');
  } catch (_) {
    pairs = [];
  }

  const updateStates = () => {
    if (!country || !state) return;
    const selected = state.value;
    const states = Array.from(new Set(
      pairs
        .filter((pair) => pair.country === country.value)
        .map((pair) => pair.state)
    )).sort((a, b) => a.localeCompare(b));
    state.replaceChildren(new Option('Choose a state or region', ''));
    states.forEach((value) => state.add(new Option(value, value)));
    if (states.includes(selected)) state.value = selected;
  };

  const updateGroups = (clearHidden = true) => {
    const selected = product?.value || '';
    groups.forEach((group) => {
      const visible = group.dataset.promotionGroup === selected;
      group.hidden = !visible;
      group.setAttribute('aria-hidden', visible ? 'false' : 'true');
      if (!visible && clearHidden) {
        group.querySelectorAll('select,input,textarea').forEach((control) => {
          if (control.type === 'checkbox' || control.type === 'radio') control.checked = false;
          else control.value = '';
        });
      }
    });
    updateStates();
  };

  product?.addEventListener('change', () => updateGroups(true));
  country?.addEventListener('change', updateStates);
  updateGroups(true);
})();
