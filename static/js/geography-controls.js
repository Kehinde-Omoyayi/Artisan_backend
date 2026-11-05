/* Local ISO country/first-level region, catalogue-location and phone controls. */
(function (root, document) {
  'use strict';
  const geo = root.ArtisanGeography;
  if (!geo || !document) return;

  const countries = Array.isArray(geo.countries) ? geo.countries : [];
  const byCode = new Map(countries.map((country) => [String(country.code).toUpperCase(), country]));
  const byName = new Map(countries.map((country) => [String(country.name).trim().toLowerCase(), country]));
  const callingCodes = new Map();
  countries.forEach((country) => {
    if (!country.callingCode) return;
    const list = callingCodes.get(country.callingCode) || [];
    list.push(country.name);
    callingCodes.set(country.callingCode, list);
  });

  function countryFor(value) {
    const raw = String(value || '').trim();
    if (!raw) return null;
    return byCode.get(raw.toUpperCase()) || byName.get(raw.toLowerCase()) || null;
  }

  function attachDatalist(input, idPrefix, values) {
    if (!input) return null;
    const id = `${idPrefix}-${input.id || Math.random().toString(36).slice(2)}`;
    let list = document.getElementById(id);
    if (!list) {
      list = document.createElement('datalist');
      list.id = id;
      input.insertAdjacentElement('afterend', list);
    }
    input.setAttribute('list', id);
    list.replaceChildren(...values.map((item) => {
      const option = document.createElement('option');
      option.value = typeof item === 'string' ? item : item.label;
      return option;
    }));
    return list;
  }

  function locationRecords(select) {
    try {
      if (select.dataset.locationRecords) {
        const parsed = JSON.parse(select.dataset.locationRecords);
        if (Array.isArray(parsed)) return parsed;
      }
    } catch (_error) {
      // Fall back to the server-rendered catalogue options below.
    }
    return Array.from(select.options || []).filter((option) => option.value).map((option) => ({
      id: String(option.value),
      country: option.dataset.country || '',
      state: option.dataset.state || '',
      area: option.dataset.area || '',
      service_ids: String(option.dataset.serviceIds || '').split(',').filter(Boolean),
    }));
  }

  function regionOptions(countryValue, extraStates) {
    const country = countryFor(countryValue);
    const values = country ? (geo.regionsByCountry[country.code] || []).map((region) => region.label || region.name) : [];
    (extraStates || []).forEach((state) => {
      const text = String(state || '').trim();
      if (text && !values.some((existing) => existing.toLowerCase() === text.toLowerCase())) values.push(text);
    });
    return values.sort((a, b) => a.localeCompare(b));
  }

  function bindGeographyPair(countryInput, stateInput, areaInput) {
    if (!countryInput) return;
    attachDatalist(countryInput, 'geo-countries', countries.map((country) => country.name));
    const group = countryInput.dataset.geographyCountry || '';
    const locationSelect = countryInput.form && countryInput.form.querySelector('[data-location-select]');
    const locationSearch = countryInput.form && countryInput.form.querySelector('[data-location-search]');
    const records = locationSelect ? locationRecords(locationSelect) : [];

    if (!countryInput.value.trim() && countryInput.dataset.geographyDefault) {
      const fallback = byCode.get(countryInput.dataset.geographyDefault.toUpperCase());
      if (fallback) countryInput.value = fallback.name;
    }

    function refresh() {
      const typedCountry = countryInput.value.trim();
      const normalizedCountry = (countryFor(typedCountry) && countryFor(typedCountry).name) || typedCountry;
      const typedState = stateInput ? stateInput.value.trim() : '';
      const serviceId = locationSelect ? String(locationSelect.dataset.serviceId || '') : '';
      const scopedRecords = records.filter((item) => {
        const serviceIds = Array.isArray(item.service_ids) ? item.service_ids.map(String) : [];
        return !serviceId || !serviceIds.length || serviceIds.includes(serviceId);
      });
      const matchingRecords = scopedRecords.filter((item) => {
        const countryMatches = !normalizedCountry || String(item.country || '').trim().toLowerCase() === normalizedCountry.toLowerCase();
        const stateMatches = !typedState || String(item.state || '').trim().toLowerCase().includes(typedState.toLowerCase()) || typedState.toLowerCase().includes(String(item.state || '').trim().toLowerCase());
        return countryMatches && stateMatches;
      });
      if (stateInput) {
        const states = scopedRecords.filter((item) => !normalizedCountry || String(item.country || '').trim().toLowerCase() === normalizedCountry.toLowerCase()).map((item) => item.state);
        attachDatalist(stateInput, 'geo-states', regionOptions(normalizedCountry, states));
      }
      if (areaInput) {
        const areas = Array.from(new Set(matchingRecords.map((item) => String(item.area || '').trim()).filter(Boolean))).sort((a, b) => a.localeCompare(b));
        attachDatalist(areaInput, 'geo-areas', areas);
      }
      if (locationSelect) refreshLocationSelect(locationSelect, normalizedCountry, typedState, areaInput && areaInput.value, locationSearch && locationSearch.value);
    }
    countryInput.addEventListener('input', refresh);
    countryInput.addEventListener('change', refresh);
    if (stateInput) {
      stateInput.addEventListener('input', refresh);
      stateInput.addEventListener('change', refresh);
    }
    if (areaInput) {
      areaInput.addEventListener('input', refresh);
      areaInput.addEventListener('change', refresh);
    }
    if (locationSearch) {
      locationSearch.addEventListener('input', refresh);
      locationSearch.addEventListener('change', refresh);
    }
    refresh();
  }

  function refreshLocationSelect(select, countryValue, stateValue, areaValue, searchValue) {
    const records = locationRecords(select);
    if (!records.length) return;
    const countryText = String(countryValue || '').trim();
    const country = ((countryFor(countryText) && countryFor(countryText).name) || countryText).toLowerCase();
    const state = String(stateValue || '').trim().toLowerCase();
    const area = String(areaValue || '').trim().toLowerCase();
    const search = String(searchValue || '').trim().toLowerCase();
    const serviceId = String(select.dataset.serviceId || '');
    const anywhereValue = select.dataset.locationAnywhereValue || '__anywhere__';
    const recordById = new Map(records.map((item) => [String(item.id), item]));
    let selectedStillAvailable = false;
    Array.from(select.options).forEach((option) => {
      if (!option.value) return;
      if (option.value === anywhereValue) {
        option.hidden = false;
        option.disabled = false;
        if (select.value === option.value) selectedStillAvailable = true;
        return;
      }
      const record = recordById.get(String(option.value));
      if (!record) {
        option.hidden = true;
        option.disabled = true;
        return;
      }
      const recordState = String(record.state || '').trim().toLowerCase();
      const recordArea = String(record.area || '').trim().toLowerCase();
      const countryMatches = !country || String(record.country || '').trim().toLowerCase() === country;
      const stateMatches = !state || recordState.includes(state) || state.includes(recordState);
      const areaMatches = !area || recordArea.includes(area);
      const optionServiceIds = String(option.dataset.serviceIds || '').split(',').filter(Boolean);
      const serviceMatches = !serviceId || !optionServiceIds.length || optionServiceIds.includes(serviceId);
      const searchText = `${recordArea} ${recordState} ${String(record.country || '').trim().toLowerCase()}`;
      const searchMatches = !search || searchText.includes(search);
      const visible = countryMatches && stateMatches && areaMatches && serviceMatches && searchMatches;
      option.hidden = !visible;
      option.disabled = !visible;
      if (visible && option.value === select.value) selectedStillAvailable = true;
    });
    if (!selectedStillAvailable && select.value) select.value = '';
  }

  function bindPhone(form) {
    const codeSelect = form.querySelector('[data-phone-calling-code]');
    const numberInput = form.querySelector('[data-phone-number]');
    if (!codeSelect || !numberInput) return;
    Array.from(codeSelect.options).forEach((option) => {
      const names = callingCodes.get(option.value) || [];
      if (!names.length) return;
      const shortNames = names.slice(0, 4);
      const suffix = names.length > shortNames.length ? ` and ${names.length - shortNames.length} more` : '';
      option.textContent = `${shortNames.join(' / ')}${suffix} (${option.value})`;
    });
    const ng = countries.find((country) => country.code === 'NG');
    if (!codeSelect.value) codeSelect.value = ng ? ng.callingCode : '+234';

    function validate() {
      const local = numberInput.value.trim();
      if (!local) {
        numberInput.setCustomValidity('');
        return true;
      }
      const library = root.libphonenumber || {};
      const parse = library.parsePhoneNumberFromString || library.parsePhoneNumber;
      if (typeof parse !== 'function') return true;
      const code = codeSelect.value || '+234';
      let candidate = local;
      if (!candidate.startsWith('+')) {
        let digits = candidate.replace(/\D/g, '');
        if (code === '+234' && digits.startsWith('0')) digits = digits.slice(1);
        candidate = `${code}${digits}`;
      }
      let valid = false;
      try {
        const parsed = parse(candidate);
        valid = Boolean(parsed && parsed.isValid());
      } catch (_error) {
        valid = false;
      }
      numberInput.setCustomValidity(valid ? '' : 'Enter a valid phone number for the selected calling code.');
      return valid;
    }
    numberInput.addEventListener('input', () => numberInput.setCustomValidity(''));
    numberInput.addEventListener('change', validate);
    codeSelect.addEventListener('change', validate);
    form.addEventListener('submit', (event) => {
      if (!validate()) {
        event.preventDefault();
        numberInput.reportValidity();
      }
    }, true);
  }

  document.querySelectorAll('form').forEach((form) => {
    const countryInputs = Array.from(form.querySelectorAll('[data-geography-country]'));
    countryInputs.forEach((countryInput) => {
      const group = countryInput.dataset.geographyCountry || '';
      const stateInput = Array.from(form.querySelectorAll('[data-geography-state]')).find((input) => input.dataset.geographyState === group);
      const areaInput = form.querySelector('[data-location-area-filter]');
      bindGeographyPair(countryInput, stateInput, areaInput);
    });
    bindPhone(form);
  });
})(typeof window !== 'undefined' ? window : globalThis, typeof document !== 'undefined' ? document : null);
