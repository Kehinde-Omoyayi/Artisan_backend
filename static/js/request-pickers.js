/**
 * Customer request pickers.
 *
 * Category, Service, Country, State, Area, Preferred date and Urgency are the same
 * glass pickers used by the discovery filters. The date is a custom animated
 * calendar rather than the browser's native control.
 *
 * All values are written into the real Django form fields, so validation and the
 * API payload are unchanged.
 */
(() => {
  const root = document.querySelector('[data-request-pickers]');
  if (!root) return;

  const EMPTY_CATALOGUE = { categories: [], services: [], locations: [] };

  const readCatalogue = () => {
    const node = document.getElementById('request-catalogue-data');
    if (!node) return EMPTY_CATALOGUE;
    try {
      const value = JSON.parse(node.textContent);
      return value && typeof value === 'object' && !Array.isArray(value) ? value : EMPTY_CATALOGUE;
    } catch (error) {
      return EMPTY_CATALOGUE;
    }
  };

  const data = readCatalogue();
  const locations = data.locations || [];
  const TODAY = data.today || '';
  const services = (data.services || []).filter((item) => item && item.name);
  const categories = (data.categories || []).map((item) => item.name).filter(Boolean);

  const form = root.closest('form');
  const field = (name) => (form ? form.querySelector(`[name="${name}"]`) : null);

  const inputs = {
    category: field('category_name'),
    service: field('service_name'),
    institution: field('institution'),
    country: field('location_country_filter'),
    state: field('location_state_filter'),
    area: field('location_area_filter'),
    date: field('preferred_date'),
    urgency: field('urgency'),
  };
  if (!inputs.category || !inputs.service) return;

  const rows = {};
  root.querySelectorAll('[data-request-row]').forEach((row) => {
    rows[row.dataset.pickKey] = row;
  });

  const state = {
    category: inputs.category.value || '',
    service: inputs.service.value || '',
    institution: '',
    country: inputs.country ? inputs.country.value : '',
    state: inputs.state ? inputs.state.value : '',
    area: inputs.area ? inputs.area.value : '',
    date: inputs.date ? inputs.date.value : '',
    urgency: inputs.urgency ? (inputs.urgency.value || 'NORMAL') : 'NORMAL',
  };

  const institutions = (data.institutions || []).filter((item) => item && item.id && item.name);

  const URGENCY = [
    { value: 'LOW', label: 'Low' },
    { value: 'NORMAL', label: 'Normal' },
    { value: 'HIGH', label: 'High' },
    { value: 'EMERGENCY', label: 'Emergency' },
  ];

  const uniqueSorted = (values) =>
    [...new Set(values.filter(Boolean))].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));

  const closeRows = (except) => {
    Object.values(rows).forEach((row) => {
      if (!row || row === except) return;
      row.classList.remove('is-open');
      const head = row.querySelector('[data-pick-toggle]');
      const body = row.querySelector('[data-pick-body]');
      if (head) head.setAttribute('aria-expanded', 'false');
      if (body) body.hidden = true;
    });
  };

  const openRow = (key) => {
    const row = rows[key];
    if (!row) return;
    closeRows(row);
    row.classList.add('is-open');
    const head = row.querySelector('[data-pick-toggle]');
    const body = row.querySelector('[data-pick-body]');
    if (head) head.setAttribute('aria-expanded', 'true');
    if (body) body.hidden = false;
  };

  const setValue = (key, label, placeholder) => {
    const row = rows[key];
    if (!row) return;
    const target = row.querySelector('[data-pick-value]');
    if (!target) return;
    target.textContent = label || placeholder || '';
    target.classList.toggle('is-placeholder', !label);
  };

  const renderOptions = (key, items, onPick, selected) => {
    const row = rows[key];
    if (!row) return;
    const list = row.querySelector('[data-pick-options]');
    const empty = row.querySelector('[data-pick-empty]');
    if (!list) return;
    list.textContent = '';
    let search = row.querySelector('[data-request-option-search]');
    if (!search) {
      search = document.createElement('input');
      search.type = 'search';
      search.dataset.requestOptionSearch = '';
      search.placeholder = 'Search options…';
      search.setAttribute('aria-label', `Search ${key} options`);
      search.style.cssText = 'box-sizing:border-box;width:100%;padding:10px;margin:0 0 8px;border:1px solid #e3d2c1;border-radius:10px;background:#fffdf9';
      list.before(search);
      search.addEventListener('input', () => {
        const query = search.value.trim().toLocaleLowerCase();
        let visible = 0;
        list.querySelectorAll('.request-pick-option').forEach((option) => {
          option.hidden = !option.textContent.toLocaleLowerCase().includes(query);
          if (!option.hidden) visible += 1;
        });
        if (empty) empty.hidden = visible > 0;
      });
    }
    search.value = '';
    search.hidden = items.length < 12;

    items.forEach((item) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'request-pick-option';
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

  /* ------------------------------------------------------------- catalogue */

  const servicesFor = (categoryName) =>
    services
      .filter((item) => !categoryName || item.category_name === categoryName)
      .map((item) => item.name)
      .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));

  const geography = data.geography || {};
  const countries = uniqueSorted(Object.keys(geography).concat(locations.map((item) => item.country)));
  const statesFor = (countryName) => countryName ? uniqueSorted(
    Object.keys(geography[countryName] || {}).concat(
      locations.filter((item) => item.country === countryName).map((item) => item.state)
    )) : [];
  const areasFor = (countryName, stateName) => countryName && stateName ? uniqueSorted(
    ((geography[countryName] || {})[stateName] || []).concat(
      locations.filter((item) => item.country === countryName && item.state === stateName).map((item) => item.area)
    )) : [];

  const renderCategory = () => {
    renderOptions(
      'category',
      uniqueSorted(categories).map((name) => ({ value: name, label: name })),
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
    setValue('category', state.category, 'Choose a category');
  };

  const renderService = () => {
    const names = servicesFor(state.category);
    if (!state.service) {
      const target = rows.service && rows.service.querySelector('[data-pick-value]');
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
    setValue('service', state.service, state.category ? 'Choose a service' : 'Choose a category first');
  };

  const renderCountry = () => {
    renderOptions(
      'country',
      countries.map((name) => ({ value: name, label: name })),
      (item) => {
        state.country = item.value;
        state.state = '';
        state.area = '';
        if (inputs.country) inputs.country.value = state.country;
        if (inputs.state) inputs.state.value = '';
        if (inputs.area) inputs.area.value = '';
        setValue('country', state.country, 'All countries');
        setValue('state', '', 'All states');
        setValue('area', '', 'All areas');
        renderState();
        renderArea();
        renderInstitution();
      },
      state.country,
    );
    setValue('country', state.country, 'All countries');
  };

  const renderState = () => {
    renderOptions(
      'state',
      statesFor(state.country).map((name) => ({ value: name, label: name })),
      (item) => {
        state.state = item.value;
        state.area = '';
        if (inputs.state) inputs.state.value = state.state;
        if (inputs.area) inputs.area.value = '';
        setValue('state', state.state, 'All states');
        setValue('area', '', 'All areas');
        renderArea();
        renderInstitution();
      },
      state.state,
    );
    setValue('state', state.state, 'All states');
  };

  const renderArea = () => {
    const names = areasFor(state.country, state.state);
    if (!state.area) {
      const target = rows.area && rows.area.querySelector('[data-pick-value]');
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
        if (inputs.area) inputs.area.value = state.area;
        setValue('area', state.area, 'All areas');
        renderInstitution();
      },
      state.area,
    );
    setValue('area', state.area, state.state ? 'All areas' : 'Choose a state first');
  };

  /* ---------------------------------------------------------- institution */

  const institutionLabel = (item) => {
    const abbr = item.abbreviation ? ` (${item.abbreviation})` : '';
    const place = [item.area, item.state].filter(Boolean).join(', ');
    return `${item.name}${abbr}${place ? ' · ' + place : ''}`;
  };

  const filteredInstitutions = () => {
    const list = institutions.filter((item) => {
      if (state.country && String(item.country || '').toLowerCase() !== state.country.toLowerCase()) return false;
      if (state.state && String(item.state || '').toLowerCase() !== state.state.toLowerCase()) return false;
      if (state.area && String(item.area || '').toLowerCase() !== state.area.toLowerCase()) return false;
      return true;
    });
    // Alphabetical with Nigeria first, regardless of the current filter.
    return [...list].sort((a, b) => {
      const aNigeria = String(a.country || '').toLowerCase() === 'nigeria' ? 0 : 1;
      const bNigeria = String(b.country || '').toLowerCase() === 'nigeria' ? 0 : 1;
      if (aNigeria !== bNigeria) return aNigeria - bNigeria;
      return a.name.localeCompare(b.name, undefined, { sensitivity: 'base' })
        || String(a.state || '').localeCompare(String(b.state || ''), undefined, { sensitivity: 'base' });
    });
  };

  const renderInstitution = () => {
    if (!rows.institution) return;
    const options = [{ value: '', label: 'None — skip this step' }].concat(
      filteredInstitutions().map((item) => ({ value: String(item.id), label: institutionLabel(item) }))
    );
    renderOptions(
      'institution',
      options,
      (item) => {
        if (!item.value) {
          state.institution = '';
          if (inputs.institution) inputs.institution.value = '';
          setValue('institution', '', 'Optional');
          return;
        }
        const record = institutions.find((entry) => String(entry.id) === item.value);
        if (!record) return;
        state.institution = String(record.id);
        if (inputs.institution) inputs.institution.value = institutionLabel(record);
        setValue('institution', institutionLabel(record), 'Optional');

        // Picking an institution fills the geographic pickers from its record.
        state.country = record.country || state.country;
        state.state = record.state || state.state;
        state.area = record.area || state.area;
        if (inputs.country) inputs.country.value = state.country;
        if (inputs.state) inputs.state.value = state.state;
        if (inputs.area) inputs.area.value = state.area;
        setValue('country', state.country, 'All countries');
        setValue('state', state.state, 'All states');
        setValue('area', state.area, 'All areas');
        renderCountry();
        renderState();
        renderArea();
        renderInstitution();
      },
      state.institution,
    );
  };

  const renderUrgency = () => {
    const current = (state.urgency || 'NORMAL').toUpperCase();
    renderOptions(
      'urgency',
      URGENCY.map((item) => ({ value: item.value, label: item.label })),
      (item) => {
        state.urgency = item.value;
        if (inputs.urgency) inputs.urgency.value = item.value;
        setValue('urgency', item.label, 'Normal');
      },
      current,
    );
    const label = (URGENCY.find((item) => item.value === current) || URGENCY[1]).label;
    setValue('urgency', label, 'Normal');
    if (inputs.urgency && !inputs.urgency.value) inputs.urgency.value = 'NORMAL';
  };

  /* -------------------------------------------------------------- calendar */

  const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December'];
  const grid = root.querySelector('[data-calendar-grid]');
  const title = root.querySelector('[data-calendar-title]');
  let cursor = state.date ? new Date(`${state.date}T00:00:00`) : new Date();
  if (Number.isNaN(cursor.getTime())) cursor = new Date();

  const iso = (date) =>
    `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;

  const pretty = (value) => {
    if (!value) return '';
    const parsed = new Date(`${value}T00:00:00`);
    if (Number.isNaN(parsed.getTime())) return value;
    return parsed.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
  };

  const renderCalendar = () => {
    if (!grid) return;
    const todayIso = TODAY || iso(new Date());
    const year = cursor.getFullYear();
    const month = cursor.getMonth();

    if (title) title.textContent = `${MONTHS[month]} ${year}`;
    grid.textContent = '';

    const first = new Date(year, month, 1);
    const startOffset = first.getDay();
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const prevDays = new Date(year, month, 0).getDate();

    const makeDay = (day, { muted = false, date = null } = {}) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'request-calendar-day';
      button.setAttribute('role', 'gridcell');
      button.textContent = String(day);
      if (muted) button.classList.add('is-muted');
      const value = date || iso(new Date(year, month, day));
      if (value === todayIso) button.classList.add('is-today');
      if (value === state.date) button.classList.add('is-selected');
      if (value < todayIso) {
        button.disabled = true;
        button.classList.add('is-disabled');
        button.setAttribute('aria-disabled', 'true');
        grid.appendChild(button);
        return;
      }
      button.addEventListener('click', () => {
        state.date = value;
        if (inputs.date) inputs.date.value = value;
        setValue('date', pretty(value), 'Choose a date');
        renderCalendar();
        closeRows();
      });
      grid.appendChild(button);
    };

    for (let i = startOffset - 1; i >= 0; i -= 1) {
      makeDay(prevDays - i, { muted: true, date: iso(new Date(year, month - 1, prevDays - i)) });
    }
    for (let day = 1; day <= daysInMonth; day += 1) makeDay(day);
    const total = startOffset + daysInMonth;
    const trailing = (7 - (total % 7)) % 7;
    for (let i = 1; i <= trailing; i += 1) {
      makeDay(i, { muted: true, date: iso(new Date(year, month + 1, i)) });
    }
  };

  const shiftMonth = (delta) => {
    cursor = new Date(cursor.getFullYear(), cursor.getMonth() + delta, 1);
    renderCalendar();
  };

  const prev = root.querySelector('[data-calendar-prev]');
  const next = root.querySelector('[data-calendar-next]');
  const todayButton = root.querySelector('[data-calendar-today]');
  const clearButton = root.querySelector('[data-calendar-clear]');

  if (prev) prev.addEventListener('click', () => shiftMonth(-1));
  if (next) next.addEventListener('click', () => shiftMonth(1));
  if (todayButton) {
    todayButton.addEventListener('click', () => {
      const todayValue = TODAY || iso(new Date());
      const anchor = new Date(`${todayValue}T00:00:00`);
      cursor = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
      state.date = todayValue;
      if (inputs.date) inputs.date.value = state.date;
      setValue('date', pretty(state.date), 'Choose a date');
      renderCalendar();
      closeRows();
    });
  }
  if (clearButton) {
    clearButton.addEventListener('click', () => {
      state.date = '';
      if (inputs.date) inputs.date.value = '';
      setValue('date', '', 'Choose a date');
      renderCalendar();
    });
  }

  /* ---------------------------------------------------------- interactions */

  root.querySelectorAll('[data-pick-toggle]').forEach((head) => {
    head.addEventListener('click', () => {
      const row = head.closest('[data-request-row]');
      if (!row) return;
      const key = row.dataset.pickKey;
      if (row.classList.contains('is-open')) {
        closeRows();
      } else {
        openRow(key);
        if (key === 'date') renderCalendar();
      }
    });
  });

  document.addEventListener('click', (event) => {
    if (!(event.target instanceof Element)) return;
    if (event.target.closest('[data-pick-toggle]') || event.target.closest('[data-pick-body]')) return;
    closeRows();
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeRows();
  });

  // Never submit a request without a date or a location.
  if (form) {
    form.addEventListener('submit', (event) => {
      if (!state.date) {
        event.preventDefault();
        openRow('date');
        renderCalendar();
        return;
      }
      if (!state.country || !state.state || !state.area) {
        event.preventDefault();
        openRow(!state.country ? 'country' : (!state.state ? 'state' : 'area'));
      }
    });
  }

  /* ---------------------------------------------------------------- paint */

  renderCategory();
  renderService();
  renderCountry();
  renderState();
  renderArea();
  renderInstitution();
  renderUrgency();
  renderCalendar();
  setValue('date', pretty(state.date), 'Choose a date');
})();

/**
 * Responder filter block ("Your choice" / "Artisans who responded").
 * Same glass picker language as the request form, but it submits a GET form.
 */
(() => {
  const block = document.querySelector('[data-responder-filters]');
  if (!block) return;

  const readCatalogue = () => {
    const node = document.getElementById('request-catalogue-data');
    if (!node) return { locations: [] };
    try {
      const parsed = JSON.parse(node.textContent);
      return Array.isArray(parsed) ? { locations: [] } : parsed;
    } catch (error) {
      return { locations: [] };
    }
  };

  const locations = readCatalogue().locations || [];
  const uniqueSorted = (values) =>
    [...new Set(values.filter(Boolean))].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));

  const rows = [...block.querySelectorAll('[data-request-row]')];
  const byKey = {};
  rows.forEach((row) => { byKey[row.dataset.pickKey] = row; });

  const setLabel = (row, label, placeholder) => {
    const target = row && row.querySelector('[data-pick-value]');
    if (!target) return;
    target.textContent = label || placeholder || '';
    target.classList.toggle('is-placeholder', !label);
  };

  const closeAll = (except) => {
    rows.forEach((row) => {
      if (row === except) return;
      row.classList.remove('is-open');
      const head = row.querySelector('[data-pick-toggle]');
      const body = row.querySelector('[data-pick-body]');
      if (head) head.setAttribute('aria-expanded', 'false');
      if (body) body.hidden = true;
    });
  };

  const open = (row) => {
    closeAll(row);
    row.classList.add('is-open');
    const head = row.querySelector('[data-pick-toggle]');
    const body = row.querySelector('[data-pick-body]');
    if (head) head.setAttribute('aria-expanded', 'true');
    if (body) body.hidden = false;
  };

  const lists = {
    state: uniqueSorted(locations.map((item) => item.state)).map((name) => ({ value: name, label: name })),
    area: uniqueSorted(locations.map((item) => item.area)).map((name) => ({ value: name, label: name })),
    rating: [
      { value: '', label: 'Any rating' },
      { value: '3', label: '3.0 and above' },
      { value: '4', label: '4.0 and above' },
      { value: '4.5', label: '4.5 and above' },
    ],
    ordering: [
      { value: 'newest', label: 'Most recent' },
      { value: 'rating', label: 'Highest rated' },
      { value: 'experience', label: 'Most experienced' },
      { value: 'completion', label: 'Completion rate' },
    ],
  };

  const placeholders = {
    state: 'Any state', area: 'Any area', rating: 'Any rating', ordering: 'Most recent',
  };

  rows.forEach((row) => {
    const key = row.dataset.pickKey;
    const hidden = row.querySelector('[data-responder-input]');
    const list = row.querySelector('[data-responder-options]');
    const options = lists[key] || [];

    if (list) {
      list.textContent = '';
      options.forEach((option) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'request-pick-option';
        button.setAttribute('role', 'option');
        if (hidden && hidden.value === option.value) {
          button.classList.add('is-selected');
          button.setAttribute('aria-selected', 'true');
        }
        button.textContent = option.label;
        button.addEventListener('click', () => {
          if (hidden) hidden.value = option.value;
          setLabel(row, option.label, placeholders[key]);
          list.querySelectorAll('.request-pick-option').forEach((item) => {
            item.classList.remove('is-selected');
            item.setAttribute('aria-selected', 'false');
          });
          button.classList.add('is-selected');
          button.setAttribute('aria-selected', 'true');
          closeAll();
        });
        list.appendChild(button);
      });
    }

    const head = row.querySelector('[data-pick-toggle]');
    if (head) {
      head.addEventListener('click', () => {
        if (row.classList.contains('is-open')) closeAll();
        else open(row);
      });
    }

    if (hidden) {
      const current = options.find((option) => option.value === hidden.value);
      if (current) setLabel(row, current.value ? current.label : '', placeholders[key]);
    }
  });

  document.addEventListener('click', (event) => {
    if (!(event.target instanceof Element)) return;
    if (event.target.closest('[data-responder-filters]')) return;
    closeAll();
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeAll();
  });
})();
