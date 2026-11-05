(() => {
  const initBvnFlow = () => {
    const page = document.querySelector('[data-artisan-page="bvn-verification"]');
    if (!page) return;

    const inputStep = page.querySelector('[data-bvn-step="input"]');
    const progressStep = page.querySelector('[data-bvn-step="progress"]');
    const otpStep = page.querySelector('[data-bvn-step="otp"]');
    const resultStep = page.querySelector('[data-bvn-step="result"]');
    const progressTitle = page.querySelector('[data-bvn-progress-title]');
    const flowMessage = page.querySelector('[data-bvn-flow-message]');
    const otpNote = page.querySelector('[data-bvn-otp-note]');
    const codeFeedback = page.querySelector('[data-bvn-code-feedback]');
    const resultTitle = page.querySelector('[data-bvn-result-title]');
    const resultStatus = page.querySelector('[data-bvn-result-status]');
    const resultMessage = page.querySelector('[data-bvn-result-message]');
    const resultGuidance = page.querySelector('[data-bvn-result-guidance]');
    const resultMark = page.querySelector('[data-bvn-result-mark]');
    const retryButton = page.querySelector('[data-bvn-retry]');
    const codeActions = page.querySelector('[data-bvn-code-actions]');
    const resendButton = page.querySelector('[data-bvn-resend]');
    const methodForm = page.querySelector('[data-bvn-method-form]');
    const otpForm = page.querySelector('[data-bvn-otp-form]');
    const startForm = page.querySelector('[data-bvn-start-form]');
    const stepIndicators = Array.from(page.querySelectorAll('[data-bvn-step-indicator]'));
    const apiErrors = window.ArtisanApiErrors;
    const readApiPayload = (response) => apiErrors ? apiErrors.readResponse(response) : response.json().catch(() => null);
    const formatApiError = (payload, status, options) => apiErrors
      ? apiErrors.format(payload, status, options)
      : 'The request could not be completed. Please try again.';

    let stage = page.dataset.flowStage || 'bvn';
    let polling = false;
    let stopped = false;
    let requestInProgress = false;
    let autoRequestAttempted = false;
    let sendFailed = false;
    let otpRequiredBaseline = '';
    let pollTimer = null;
    let pollCount = 0;
    const statusUrl = page.dataset.statusUrl || '';
    const methodUrl = page.dataset.methodUrl || '';
    const hasOtpErrors = page.dataset.hasOtpErrors === 'true';
    const csrfInput = methodForm && methodForm.querySelector('[name="csrfmiddlewaretoken"]');
    const csrfToken = csrfInput ? csrfInput.value : '';

    const setStage = (nextStage) => {
      stage = nextStage;
      page.dataset.flowStage = nextStage;
      if (inputStep) inputStep.hidden = nextStage !== 'bvn';
      if (progressStep) progressStep.hidden = !['initiating', 'requesting_otp', 'checking_result'].includes(nextStage);
      if (otpStep) otpStep.hidden = nextStage !== 'otp';
      if (resultStep) resultStep.hidden = nextStage !== 'result';
      if (codeActions) codeActions.hidden = nextStage !== 'otp' && !(nextStage === 'requesting_otp' && sendFailed);
      if (resendButton) resendButton.textContent = nextStage === 'otp' ? 'Resend email code' : 'Try requesting the email code again';
      updateStepIndicators();
    };

    const updateStepIndicators = () => {
      const current = stage === 'bvn' || stage === 'initiating'
        ? 'bvn'
        : stage === 'requesting_otp'
          ? 'email'
          : stage === 'otp'
            ? 'otp'
            : stage === 'checking_result'
              ? 'otp'
              : 'result';
      const order = ['bvn', 'email', 'otp'];
      const currentIndex = order.indexOf(current);
      stepIndicators.forEach((indicator) => {
        const name = indicator.dataset.bvnStepIndicator;
        const index = order.indexOf(name);
        indicator.classList.toggle('is-current', name === current);
        indicator.classList.toggle('is-complete', current === 'result' || (index >= 0 && currentIndex >= 0 && index < currentIndex));
      });
    };

    const setProgress = (title, message) => {
      if (progressTitle && title) progressTitle.textContent = title;
      if (flowMessage && message) flowMessage.textContent = message;
    };

    const showOtp = (message, feedback = '') => {
      sendFailed = false;
      otpRequiredBaseline = '';
      setStage('otp');
      if (otpNote && message) otpNote.textContent = message;
      if (codeFeedback) codeFeedback.textContent = feedback;
    };

    const showResult = (status, message, guidance, retryable, successful = false) => {
      stopped = true;
      otpRequiredBaseline = '';
      if (pollTimer) window.clearTimeout(pollTimer);
      setStage('result');
      if (resultTitle) resultTitle.textContent = successful ? 'Verification complete' : 'Verification outcome';
      if (resultStatus) resultStatus.textContent = status ? `API status: ${status}` : '';
      if (resultMessage) resultMessage.textContent = message || '';
      if (resultGuidance) resultGuidance.textContent = guidance || '';
      if (retryButton) retryButton.hidden = !retryable;
      if (resultStep) resultStep.classList.toggle('is-error', !successful);
      if (resultMark) resultMark.textContent = successful ? '✓' : '!';
    };

    const displayResponseMessage = (payload, status) => {
      const emptyPayload = payload == null || (Array.isArray(payload) && !payload.length)
        || (typeof payload === 'object' && !Array.isArray(payload) && !Object.keys(payload).length);
      return emptyPayload && !(Number(status) >= 400) ? '' : formatApiError(payload, status);
    };

    const sendEmailCode = async (automatic) => {
      if (!methodForm || !methodUrl || requestInProgress) return false;
      requestInProgress = true;
      sendFailed = false;
      if (automatic) autoRequestAttempted = true;
      setStage('requesting_otp');
      setProgress('Requesting your email code', automatic
        ? 'The existing API is requesting the code automatically.'
        : 'The existing API is requesting another email code.');
      if (codeFeedback) codeFeedback.textContent = '';

      try {
        const body = new URLSearchParams(new FormData(methodForm));
        const response = await fetch(methodUrl, {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Accept': 'application/json',
            'X-Requested-With': 'XMLHttpRequest',
            'X-CSRFToken': csrfToken,
            'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
          },
          body,
        });
        const payload = await readApiPayload(response);
        const message = displayResponseMessage(payload, response.status);

        if (!response.ok) {
          sendFailed = true;
          setStage('requesting_otp');
          setProgress('Email code could not be requested', message || 'The request could not be confirmed. Try again below.');
          if (codeFeedback) codeFeedback.textContent = message || 'The email code could not be requested. Please try again.';
          return false;
        }

        setStage('requesting_otp');
        setProgress('Waiting for your email code', message || 'The request was submitted. The code field will appear when the API confirms the email code was sent.');
        return true;
      } catch (_error) {
        sendFailed = true;
        setStage('requesting_otp');
        const errorMessage = formatApiError(null, null, { networkError: true });
        setProgress('Email code could not be requested', errorMessage);
        if (codeFeedback) codeFeedback.textContent = errorMessage;
        return false;
      } finally {
        requestInProgress = false;
      }
    };

    const handleStatus = async (data) => {
      const status = String(data && data.status || '').toUpperCase();
      const message = String(data && data.message || '');
      const method = String(data && data.method || '').toUpperCase();
      if (!status) {
        if (stage === 'initiating' || stage === 'requesting_otp' || stage === 'checking_result') {
          setProgress('Checking verification status', message || 'The status is not available yet. We’ll check again.');
          return true;
        }
        return false;
      }

      if (status === 'VERIFIED') {
        showResult(status, message, 'Your verification is complete. The other method is unavailable for a verified profile.', false, true);
        return false;
      }

      if (status === 'REJECTED') {
        showResult(status, message, 'A new verification attempt is available. Review the message above, then start again with BVN or return to the verification center.', true);
        return false;
      }

      if (status === 'SERVICE_ERROR') {
        showResult(status, message, 'The API reports a service failure. You can start another BVN attempt.', true);
        return false;
      }

      if (status === 'SUSPENDED') {
        showResult(status, message, 'A new verification attempt is not available while verification is suspended.', false);
        return false;
      }

      if (status === 'OTP_REJECTED') {
        showOtp(message || 'The code was not accepted. Enter the latest six-digit email code to continue.');
        return false;
      }

      if (status === 'OTP_SENT') {
        if (stage === 'checking_result') {
          setProgress('Checking your code', flowMessage && flowMessage.textContent || 'Your submitted code is being checked.');
          return true;
        }
        showOtp(message || 'The email code was sent. Enter the six-digit code here.');
        return false;
      }

      if (status === 'OTP_REQUIRED') {
        if (stage === 'initiating' || (stage === 'bvn' && !autoRequestAttempted)) {
          otpRequiredBaseline = message;
          setProgress('Requesting your email code', message || 'The API is ready to request your email code.');
          return await sendEmailCode(true);
        }
        if (stage === 'checking_result') {
          showOtp(message || 'Enter the latest six-digit email code to continue.');
          return false;
        }
        if (stage === 'requesting_otp') {
          const lowerMessage = message.toLowerCase();
          const explicitSendFailure = lowerMessage.includes('unable to send') || lowerMessage.includes('could not send');
          const changedMessage = otpRequiredBaseline && message && message !== otpRequiredBaseline;
          if (sendFailed || explicitSendFailure || changedMessage) {
            sendFailed = true;
            setStage('requesting_otp');
            setProgress('Email code could not be sent', message || 'The code has not been confirmed as sent. You can try requesting it again.');
            if (codeFeedback) codeFeedback.textContent = message || 'The code has not been confirmed as sent. Try again below.';
            return false;
          }
          setProgress('Waiting for your email code', message || 'The request is being processed. The code field will appear when the API confirms it was sent.');
          return true;
        }
        showOtp(message || 'The code request is active. Enter the six-digit email code when it arrives.');
        return false;
      }

      if (status === 'PENDING') {
        if (method === 'NIN') {
          showResult(status, message, 'A NIN verification attempt is still processing. A new request is not available until it finishes.', false);
          return false;
        }
        if (stage === 'bvn') setStage('initiating');
        if (stage === 'checking_result') {
          setProgress('Checking your code', message || 'Your submitted code is being checked.');
        } else if (stage === 'requesting_otp') {
          setProgress('Waiting for your email code', message || 'The email code request is being processed.');
        } else {
          setProgress('Starting verification', message || 'Your BVN verification request is being processed.');
        }
        return true;
      }

      if (status === 'UNVERIFIED') {
        if (stage === 'bvn') return false;
        showResult(status, message || 'No verification request is currently active.', 'You can start a new BVN verification attempt.', true);
        return false;
      }

      if (status === 'RATE_LIMITED') {
        showResult(status, message, 'Follow the API message before attempting another request.', false);
        return false;
      }

      if (stage === 'initiating' || stage === 'requesting_otp' || stage === 'checking_result') {
        setProgress('Checking verification status', message || 'The API is still processing your request.');
        return true;
      }
      if (stage === 'bvn') return false;
      showOtp(message || 'Enter the six-digit email code to continue.');
      return false;
    };

    const pollStatus = async () => {
      if (stopped || polling || !statusUrl) return;
      polling = true;
      pollCount += 1;
      let continuePolling = false;
      try {
        const response = await fetch(statusUrl, {
          method: 'GET',
          credentials: 'same-origin',
          headers: { 'Accept': 'application/json' },
        });
        const data = await readApiPayload(response);
        if (!response.ok || !data) {
          const errorMessage = displayResponseMessage(data, response.status);
          if (response.status === 401 || response.status === 403) {
            showResult('', errorMessage, 'Return to the verification center and sign in again before continuing.', false);
            return;
          }
          if (stage === 'otp') {
            if (codeFeedback) codeFeedback.textContent = errorMessage;
          } else if (stage !== 'bvn') {
            setProgress('Checking verification status', errorMessage);
          }
          continuePolling = response.status >= 500 || response.status === 429 || !data;
          return;
        }
        continuePolling = await handleStatus(data);
      } catch (_error) {
        const errorMessage = formatApiError(null, null, { networkError: true });
        if (stage === 'otp') {
          if (codeFeedback) codeFeedback.textContent = errorMessage;
        } else if (stage !== 'bvn') {
          setProgress('Checking verification status', errorMessage);
        }
        continuePolling = stage !== 'bvn' && stage !== 'otp';
      } finally {
        polling = false;
        if (continuePolling && !stopped) {
          const delay = pollCount < 12 ? 3000 : 7000;
          pollTimer = window.setTimeout(pollStatus, delay);
        }
      }
    };

    if (resendButton) {
      resendButton.addEventListener('click', () => {
        sendEmailCode(false).then((queued) => {
          if (!queued) return;
          stopped = false;
          pollCount = 0;
          pollStatus();
        });
      });
    }

    if (retryButton) {
      retryButton.addEventListener('click', () => {
        stopped = true;
        if (pollTimer) window.clearTimeout(pollTimer);
        if (startForm) startForm.reset();
        setStage('bvn');
        autoRequestAttempted = false;
        pollCount = 0;
        stopped = false;
      });
    }

    if (startForm) {
      startForm.addEventListener('submit', (event) => {
        const submitButton = event.submitter;
        if (submitButton) submitButton.disabled = true;
      });
    }
    if (otpForm) {
      otpForm.addEventListener('submit', (event) => {
        const submitButton = event.submitter;
        if (submitButton) submitButton.disabled = true;
      });
    }

    if (hasOtpErrors) stage = 'otp';
    setStage(stage);
    pollStatus();
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initBvnFlow, { once: true });
  } else {
    initBvnFlow();
  }
})();
