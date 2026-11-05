document.addEventListener('DOMContentLoaded', () => {
  const categorySelect = document.querySelector('select[name="category_name"]');
  const serviceSelect = document.querySelector('select[name="service_name"]');
  if (categorySelect && serviceSelect) {
    const serviceGroups = Array.from(serviceSelect.querySelectorAll('optgroup'));
    const placeholder = Array.from(serviceSelect.options).find((option) => option.value === '');
    const filterServices = ({ resetSelection = false } = {}) => {
      const selectedCategory = categorySelect.value;
      let hasServices = false;
      serviceGroups.forEach((group) => {
        const isVisible = Boolean(selectedCategory) && group.label === selectedCategory;
        group.hidden = !isVisible;
        group.disabled = !isVisible;
        if (isVisible) hasServices = true;
      });
      if (placeholder) placeholder.textContent = selectedCategory ? 'Select a service' : 'Choose a category first';
      if (resetSelection) serviceSelect.value = '';
      const selected = serviceSelect.selectedOptions[0];
      const selectedGroup = selected && selected.parentElement;
      const selectedCategoryForService = selectedGroup && selectedGroup.tagName === 'OPTGROUP' ? selectedGroup.label : '';
      if (serviceSelect.value && selectedCategoryForService !== selectedCategory) serviceSelect.value = '';
      serviceSelect.disabled = !selectedCategory || !hasServices;
    };
    categorySelect.addEventListener('change', () => filterServices({ resetSelection: true }));
    filterServices();
  }

  const page = document.querySelector('[data-artisan-page="request-detail"]');
  if (!page || page.dataset.focusResponders !== 'true') return;
  const section = document.getElementById('responders');
  if (!section) return;

  const reducedMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  window.requestAnimationFrame(() => {
    section.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'start' });
  });
});
