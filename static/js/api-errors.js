/* Shared safe API error presentation for browser-side requests. */
(function (root) {
  'use strict';

  const metadataKeys = new Set([
    'code', 'error_code', 'request_id', 'requestid', 'correlation_id', 'trace_id',
    'trace', 'traceback', 'stack', 'stacktrace', 'debug', 'exception', 'status',
    'status_code', 'headers', 'profile_role'
  ]);
  const messageKeys = new Set(['message', 'detail', 'error', 'non_field_errors', 'non_field_error']);
  const traceLine = /^\s*(?:traceback \(most recent call last\)|file "|(?:at )?\w+(?:error|exception):|stack trace\b)/im;
  const htmlPrefix = /^\s*(?:<!doctype html|<html|<body)/i;

  function safeText(value) {
    if (typeof value !== 'string') return '';
    const text = value.trim().replace(/\s+/g, ' ');
    if (!text || traceLine.test(text) || htmlPrefix.test(text)) return '';
    if (/^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$/.test(text)) return '';
    if (/^(?:internal server error|server error|bad gateway)$/i.test(text)) return '';
    return text;
  }

  function labelFor(key) {
    const label = String(key || '').replace(/[_.]/g, ' ').trim();
    if (!label) return '';
    return label === label.toUpperCase() ? label : label.charAt(0).toUpperCase() + label.slice(1).toLowerCase();
  }

  function collect(value, label) {
    const parts = [];
    if (typeof value === 'string') {
      const text = safeText(value);
      if (text) parts.push(label ? `${label}: ${text}` : text);
    } else if (Array.isArray(value)) {
      value.forEach((item) => parts.push(...collect(item, label)));
    } else if (value && typeof value === 'object') {
      if (typeof value.field === 'string' && value.message !== undefined) {
        return collect(value.message, labelFor(value.field));
      }
      Object.keys(value).forEach((key) => {
        const normalized = key.trim().toLowerCase().replace(/-/g, '_');
        if (metadataKeys.has(normalized) || normalized.startsWith('trace')) return;
        if (messageKeys.has(normalized)) {
          parts.push(...collect(value[key], label));
          return;
        }
        if (normalized === 'missing_fields') {
          const values = Array.isArray(value[key]) ? value[key] : [value[key]];
          const fieldNames = values.filter((item) => typeof item === 'string' && item.trim()).map(labelFor);
          if (fieldNames.length) {
            const fieldLabel = label ? `${label} · ${labelFor(key)}` : labelFor(key);
            parts.push(fieldLabel ? `${fieldLabel}: ${fieldNames.join(', ')}` : fieldNames.join(', '));
          }
          return;
        }
        const childLabel = labelFor(key);
        const combined = label ? (childLabel ? `${label} · ${childLabel}` : label) : childLabel;
        parts.push(...collect(value[key], combined));
      });
    }
    return parts;
  }

  function unique(parts) {
    const output = [];
    const seen = new Set();
    parts.forEach((part) => {
      const key = part.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
      if (key && !seen.has(key)) {
        seen.add(key);
        output.push(part);
      }
    });
    return output;
  }

  function fallback(status, networkError) {
    if (networkError) return 'We couldn’t connect to the marketplace. Check your connection and try again.';
    if (status === 401) return 'Your session may have expired. Please sign in again.';
    if (status === 403) return 'You don’t have permission to perform this action.';
    if (status === 404) return 'The requested item could not be found.';
    if (status === 429) return 'Too many requests. Please wait a moment and try again.';
    if (status === 400) return 'Please check the highlighted fields and try again.';
    if (Number(status) >= 500) return 'The service is temporarily unavailable. Please try again shortly.';
    return 'The request could not be completed. Please try again.';
  }

  function format(payload, status, options) {
    const settings = options || {};
    let networkError = Boolean(settings.networkError);
    if (status && typeof status === 'object') status = status.status;
    if (payload instanceof Error) {
      networkError = networkError || (!status && payload.name === 'TypeError');
      payload = null;
    }
    status = Number(status) || status;
    let primary = [];
    let detailParts = [];
    if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
      for (const key of ['message', 'detail', 'error']) {
        if (payload[key] !== undefined && payload[key] !== null) {
          primary.push(...collect(payload[key], ''));
        }
      }
      if (Object.prototype.hasOwnProperty.call(payload, 'details')) {
        detailParts = collect(payload.details, '');
      } else {
        Object.keys(payload).forEach((key) => {
          const normalized = key.trim().toLowerCase().replace(/-/g, '_');
          if (metadataKeys.has(normalized) || messageKeys.has(normalized) || normalized === 'details') return;
          detailParts.push(...collect({ [key]: payload[key] }, ''));
        });
      }
    } else if (Array.isArray(payload)) {
      detailParts = collect(payload, '');
    } else if (typeof payload === 'string') {
      primary = collect(payload, '');
    }

    primary = unique(primary);
    detailParts = unique(detailParts);
    if (!primary.length && detailParts.length) return detailParts.join(' · ');
    const comparable = (part) => part.replace(/^[^:]+:\s*/, '').toLowerCase();
    const mainText = new Set(primary.map(comparable));
    const rest = detailParts.filter((part) => !mainText.has(comparable(part)));
    const message = [...primary, ...rest].join(' · ');
    return message || fallback(status, networkError);
  }

  async function readResponse(response) {
    const type = (response && response.headers && response.headers.get('content-type')) || '';
    if (!/json/i.test(type)) return null;
    try {
      return await response.json();
    } catch (_error) {
      return null;
    }
  }

  root.ArtisanApiErrors = Object.freeze({ format, readResponse });
})(typeof window !== 'undefined' ? window : globalThis);
