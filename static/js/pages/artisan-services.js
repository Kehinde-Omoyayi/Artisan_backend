/**
 * Artisan "My services" form pickers.
 *
 * Category, Service, Country, State, Area and Institution all behave like the
 * public discovery filters: one panel open at a time, a single rotating chevron,
 * options rendered from the marketplace catalogue.
 *
 * Location rules
 *  - "Anywhere" on  -> location_id = __anywhere__ (the API's general offering)
 *  - "Anywhere" off -> Country > State > Area pick any place in the worldwide
 *    geographic catalogue (the same data as the public discovery filters) and
 *    location_id becomes the __geo__ sentinel; the server resolves the names.
 *
 * Every picker writes into the existing hidden Django form fields, so the
 * server-side validation in ArtisanServiceForm is untouched.
 */
(() => {
  const form = document.querySelector('[data-service-picker-form]');
  if (!form) return;

  const readJSON = (id, fallback) => {
    const node = document.getElementById(id);
    if (!node) return fallback;
    try {
      const value = JSON.parse(node.textContent);
      return Array.isArray(value) ? value : fallback;
    } catch (error) {
      return fallback;
    }
  };

  const readJSONObject = (id, fallback) => {
    const node = document.getElementById(id);
    if (!node) return fallback;
    try {
      const value = JSON.parse(node.textContent);
      return value && !Array.isArray(value) && typeof value === 'object' ? value : fallback;
    } catch (error) {
      return fallback;
    }
  };

  const geography = readJSONObject('service-geography-records', {});
  const institutions = readJSON('service-institution-records', []);
  const catalogue = readJSON('service-catalogue-records', []);

  const anywhereValue = form.dataset.anywhereValue || '__anywhere__';
  const geographicValue = form.dataset.geographicValue || '__geo__';
  const maxServices = Number.parseInt(form.dataset.maxServices || '3', 10) || 3;
  const serviceCount = Number.parseInt(form.dataset.serviceCount || '0', 10) || 0;

  const inputs = {
    category: form.querySelector('[name="category_name"]'),
    service: form.querySelector('[name="service_name"]'),
    country: form.querySelector('[name="location_country_filter"]'),
    state: form.querySelector('[name="location_state_filter"]'),
    area: form.querySelector('[name="location_area_filter"]'),
    institution: form.querySelector('[name="institution"]'),
    location: form.querySelector('[name="location_id"]'),
  };
  if (!inputs.location || !inputs.category || !inputs.service) return;

  const rows = {};
  form.querySelectorAll('[data-service-row]').forEach((row) => {
    rows[row.dataset.pickerKey] = row;
  });

  const state = {
    category: inputs.category.value || '',
    service: inputs.service.value || '',
    country: inputs.country.value || '',
    state: inputs.state.value || '',
    area: inputs.area.value || '',
    institution: inputs.institution.value || '',
    anywhere: !inputs.location.value || inputs.location.value === anywhereValue,
  };

  /* --------------------------------------------------------------- helpers */

  const uniqueSorted = (values) =>
    [...new Set(values.filter(Boolean))].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));

  const catalogueServices = catalogue
    .filter((item) => item && item.name)
    .map((item) => ({ name: String(item.name), category: String(item.category_name || item.category || '') }));

  const servicesFor = (categoryName) =>
    catalogueServices
      .filter((item) => !categoryName || item.category === categoryName)
      .map((item) => item.name)
      .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));

  const categories = uniqueSorted(catalogueServices.map((item) => item.category));
  const countries = uniqueSorted(Object.keys(geography));

  const statesFor = (countryName) =>
    countryName ? uniqueSorted(Object.keys(geography[countryName] || {})) : [];

  const areasFor = (countryName, stateName) =>
    countryName && stateName
      ? uniqueSorted((geography[countryName] || {})[stateName] || [])
      : [];

  // A complete geographic pick resolves to the __geo__ sentinel; the server
  // validates the names against the same catalogue and registers them.
  const locationFor = (countryName, stateName, areaName) =>
    countryName && stateName && areaName
      ? { id: geographicValue, country: countryName, state: stateName, area: areaName }
      : null;

  const closeRows = (except) => {
    Object.values(rows).forEach((row) => {
      if (!row || row === except) return;
      row.classList.remove('is-open');
      const head = row.querySelector('[data-picker-toggle]');
      const body = row.querySelector('[data-picker-body]');
      if (head) head.setAttribute('aria-expanded', 'false');
      if (body) body.hidden = true;
    });
  };

  const setValue = (key, label, placeholder) => {
    const row = rows[key];
    if (!row) return;
    const target = row.querySelector('[data-picker-value]');
    if (!target) return;
    const text = label || placeholder || '';
    target.textContent = text;
    target.classList.toggle('is-placeholder', !label);
  };

  const renderOptions = (key, items, onPick, selected) => {
    const row = rows[key];
    if (!row) return;
    const list = row.querySelector('[data-picker-options]');
    const empty = row.querySelector('[data-picker-empty]');
    if (!list) return;
    list.textContent = '';

    items.forEach((item) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'artisan-pick-option';
      button.setAttribute('role', 'option');
      button.dataset.value = item.value;
      button.setAttribute('aria-selected', String(item.value === selected));
      if (item.value === selected) button.classList.add('is-selected');
      button.textContent = item.label;
      button.addEventListener('click', () => {
        onPick(item);
        closeRows();
      });
      list.appendChild(button);
    });

    if (empty) empty.hidden = items.length > 0;
  };

  /* ---------------------------------------------------------------- render */

  const renderCategory = () => {
    renderOptions(
      'category',
      categories.map((name) => ({ value: name, label: name })),
      (item) => {
        state.category = item.value;
        state.service = '';
        inputs.category.value = item.value;
        inputs.service.value = '';
        setValue('category', item.value, 'Choose a category');
        setValue('service', '', 'Choose a service');
        renderService();
      },
      state.category,
    );
  };

  const renderService = () => {
    const names = servicesFor(state.category);
    const row = rows.service;
    if (row && !state.service) {
      const target = row.querySelector('[data-picker-value]');
      if (target) {
        target.textContent = state.category ? 'Choose a service' : 'Choose a category first';
        target.classList.add('is-placeholder');
      }
    }
    renderOptions(
      'service',
      names.map((name) => ({ value: name, label: name })),
      (item) => {
        state.service = item.value;
        inputs.service.value = item.value;
        setValue('service', item.value, 'Choose a service');
      },
      state.service,
    );
  };

  /* -------------------------------------------------------------- coverage */

  const syncLocation = () => {
    if (state.anywhere) {
      inputs.location.value = anywhereValue;
      return;
    }
    const record = locationFor(state.country, state.state, state.area);
    inputs.location.value = record ? record.id : '';
  };

  const renderCountries = () => {
    renderOptions(
      'country',
      countries.map((name) => ({ value: name, label: name })),
      (item) => {
        state.country = item.value;
        state.state = '';
        state.area = '';
        inputs.country.value = state.country;
        inputs.state.value = '';
        inputs.area.value = '';
        setValue('country', state.country, 'All countries');
        setValue('state', '', 'All states');
        setValue('area', '', 'All areas');
        renderStates();
        renderAreas();
        syncLocation();
        renderInstitutions();
      },
      state.country,
    );
    setValue('country', state.country, 'All countries');
  };

  const renderStates = () => {
    const names = statesFor(state.country);
    renderOptions(
      'state',
      names.map((name) => ({ value: name, label: name })),
      (item) => {
        state.state = item.value;
        state.area = '';
        inputs.state.value = state.state;
        inputs.area.value = '';
        setValue('state', state.state, 'All states');
        setValue('area', '', 'All areas');
        renderAreas();
        syncLocation();
        renderInstitutions();
      },
      state.state,
    );
    setValue('state', state.state, 'All states');
  };

  const renderAreas = () => {
    const names = areasFor(state.country, state.state);
    const row = rows.area;
    if (row && !state.area) {
      const target = row.querySelector('[data-picker-value]');
      if (target) {
        target.textContent = state.state ? 'All areas' : 'Choose a state first';
        target.classList.add('is-placeholder');
      }
    }
    renderOptions(
      'area',
      names.map((name) => ({ value: name, label: name })),
      (item) => {
        state.area = item.value;
        inputs.area.value = state.area;
        setValue('area', state.area, 'All areas');
        syncLocation();
        renderInstitutions();
      },
      state.area,
    );
  };

  const institutionLabel = (item) => {
    const abbr = item.abbreviation ? ` (${item.abbreviation})` : '';
    const place = [item.area, item.state].filter(Boolean).join(', ');
    return `${item.name}${abbr}${place ? ' · ' + place : ''}`;
  };

  const institutionCountry = (item) => String(item.country || '');

  const filteredInstitutions = () => {
    let list = institutions.filter((item) => {
      if (state.country && institutionCountry(item).toLowerCase() !== state.country.toLowerCase()) return false;
      if (state.state && String(item.state || '').toLowerCase() !== String(state.state || '').toLowerCase()) return false;
      if (state.area && String(item.area || '').toLowerCase() !== String(state.area || '').toLowerCase()) return false;
      return true;
    });
    // Alphabetical with Nigeria first
    list = [...list].sort((a, b) => {
      const aNigeria = institutionCountry(a).toLowerCase() === 'nigeria' ? 0 : 1;
      const bNigeria = institutionCountry(b).toLowerCase() === 'nigeria' ? 0 : 1;
      if (aNigeria !== bNigeria) return aNigeria - bNigeria;
      return a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }) || String(a.state || '').localeCompare(String(b.state || ''), undefined, { sensitivity: 'base' });
    });
    return list;
  };

  const renderInstitutions = () => {
    renderOptions(
      'institution',
      filteredInstitutions().map((item) => ({ value: String(item.id), label: institutionLabel(item) })),
      (item) => {
        const record = institutions.find((entry) => String(entry.id) === item.value);
        if (!record) return;
        state.institution = String(record.id);
        inputs.institution.value = institutionLabel(record);
        setValue('institution', institutionLabel(record), 'Optional');

        // Picking an institution fills the geographic pickers from its record.
        state.country = record.country || state.country;
        state.state = record.state || state.state;
        state.area = record.area || state.area;
        inputs.country.value = state.country;
        inputs.state.value = state.state;
        inputs.area.value = state.area;
        setValue('country', state.country, 'All countries');
        renderStates();
        renderAreas();
        syncLocation();
        renderInstitutions();
      },
      state.institution,
    );
  };

  const coverage = form.querySelector('[data-anywhere-toggle]');
  const locationRows = form.querySelector('[data-location-rows]');
  const caption = form.querySelector('[data-anywhere-caption]');

  const renderCoverage = () => {
    if (coverage) coverage.setAttribute('aria-pressed', String(state.anywhere));
    if (locationRows) locationRows.hidden = state.anywhere;
    if (caption) {
      caption.textContent = state.anywhere
        ? 'Available across all locations'
        : 'Served at one registered location';
    }
    if (state.anywhere) closeRows();
    syncLocation();
  };

  if (coverage) {
    coverage.addEventListener('click', () => {
      state.anywhere = !state.anywhere;
      renderCoverage();
    });
  }

  /* ----------------------------------------------------------- interactions */

  form.querySelectorAll('[data-picker-toggle]').forEach((head) => {
    head.addEventListener('click', () => {
      const row = head.closest('[data-service-row]');
      if (!row) return;
      const body = row.querySelector('[data-picker-body]');
      const open = !row.classList.contains('is-open');
      closeRows(row);
      row.classList.toggle('is-open', open);
      head.setAttribute('aria-expanded', String(open));
      if (body) body.hidden = !open;
    });
  });

  document.addEventListener('click', (event) => {
    if (!(event.target instanceof Element)) return;
    // Keep the panel that was just toggled (or an option inside it) open.
    if (event.target.closest('[data-picker-toggle]') || event.target.closest('[data-picker-body]')) return;
    closeRows();
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeRows();
  });

  /* ----------------------------------------------------------------- limit */

  const atLimit = serviceCount >= maxServices;
  form.addEventListener('submit', (event) => {
    const editing = form.querySelector('[name="offering_id"]');
    if (atLimit && !editing) {
      event.preventDefault();
      return;
    }
    if (!state.anywhere && !inputs.location.value) {
      event.preventDefault();
      renderCoverage();
      const countryRow = rows.country;
      if (countryRow) {
        closeRows(countryRow);
        countryRow.classList.add('is-open');
        const head = countryRow.querySelector('[data-picker-toggle]');
        const body = countryRow.querySelector('[data-picker-body]');
        if (head) head.setAttribute('aria-expanded', 'true');
        if (body) body.hidden = false;
      }
    }
  });

  /* ---------------------------------------------------------------- paint */

  renderCategory();
  renderService();
  renderCountries();
  renderStates();
  renderAreas();
  renderInstitutions();
  renderCoverage();

  setValue('category', state.category, 'Choose a category');
  setValue('service', state.service, 'Choose a service');
  setValue('country', state.country, 'All countries');
  setValue('state', state.state, 'All states');
  setValue('area', state.area, 'All areas');
})();