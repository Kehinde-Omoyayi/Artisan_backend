from django.core.cache import cache


RESULT_TTL = 7 * 24 * 60 * 60


def _provider_message(result):
    message = result.get("message")

    if isinstance(message, str) and message.strip():
        return message.strip()

    data = result.get("data") or {}
    message = data.get("message")

    if isinstance(message, str) and message.strip():
        return message.strip()

    error = result.get("error")

    if isinstance(error, str) and error.strip():
        return error.strip()

    errors = result.get("errors")

    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, str) and item.strip():
                return item.strip()

            if isinstance(item, dict):
                message = (
                    item.get("message")
                    or item.get("detail")
                    or item.get("error")
                )

                if isinstance(message, str) and message.strip():
                    return message.strip()

    return ""


def _service_message(method):
    method = method.upper()

    return (
        f"We’re sorry, but we were unable to complete your "
        f"{method} verification because our verification service "
        f"could not process your request at this time. "
        f"Your information has not been successfully verified. "
        f"Please try again later. We apologise for the inconvenience."
    )


def _rate_limit_message(method):
    method = method.upper()

    return (
        f"Your {method} verification could not be completed because "
        f"the verification service is temporarily processing too many "
        f"of your requests. Please wait a little while before trying again. "
        f"We apologise for the inconvenience."
    )


def _not_found_message(method):
    method = method.upper()

    if method == "NIN":
        return (
            "We were unable to verify your NIN because no matching "
            "identity record could be found for the information "
            "provided. Please confirm that your NIN is correct "
            "and try again."
        )

    return (
        "We were unable to verify your BVN because no matching "
        "identity record could be found for the information provided. "
        "Please confirm that your BVN is correct and try again."
    )


def _lookup_configuration_message(method):
    method = method.upper()

    return (
        f"We’re sorry, but we were unable to complete your "
        f"{method} verification because our verification service "
        f"could not process this verification request at the moment. "
        f"Your information was not successfully verified. "
        f"Please try again later. We apologise for the inconvenience."
    )


def _mismatch_message(method):
    method = method.upper()

    return (
        f"We were unable to verify your {method} because the "
        f"information provided could not be matched with the "
        f"available identity records. Please confirm your details "
        f"and try again."
    )


def _invalid_information_message(method):
    method = method.upper()

    return (
        f"We were unable to process your {method} verification "
        f"because the information provided could not be accepted. "
        f"Please check your details carefully and try again."
    )


def _otp_invalid_message():
    return (
        "We were unable to complete your BVN verification because "
        "the verification code provided could not be accepted. "
        "Please confirm that you entered the latest code sent to "
        "your email and try again."
    )


def _otp_send_failed_message():
    return (
        "We were unable to send your BVN verification code at this "
        "time. Please try again shortly. We apologise for the "
        "inconvenience."
    )


def classify_nin_result(result):
    http_status = int(
        result.get("_http_status") or 0
    )

    provider_status = str(
        result.get("status") or "FAILED"
    ).lower()

    provider_message = _provider_message(result)
    text = provider_message.lower()

    if http_status == 202:
        return {
            "status": "PENDING",
            "title": "NIN verification submitted",
            "message": (
                "Your NIN verification request has been received "
                "and is now being processed. Please check your "
                "verification status after 1 minute."
            ),
            "retryable": False,
        }

    if (
        "lookup app" in text
        or "lookup product" in text
    ):
        return {
            "status": "SERVICE_ERROR",
            "title": "NIN verification unavailable",
            "message": _lookup_configuration_message("NIN"),
            "retryable": True,
        }

    if http_status == 429:
        return {
            "status": "RATE_LIMITED",
            "title": "NIN verification temporarily unavailable",
            "message": _rate_limit_message("NIN"),
            "retryable": True,
        }

    if http_status == 404:
        return {
            "status": "REJECTED",
            "title": "NIN verification could not be completed",
            "message": _not_found_message("NIN"),
            "retryable": True,
        }

    if http_status in {401, 403}:
        return {
            "status": "SERVICE_ERROR",
            "title": "NIN verification unavailable",
            "message": _service_message("NIN"),
            "retryable": True,
        }

    if http_status in {400, 422}:
        if any(
            phrase in text
            for phrase in (
                "no match",
                "no matching",
                "not found",
                "does not exist",
                "record not found",
            )
        ):
            message = _not_found_message("NIN")
        else:
            message = _mismatch_message("NIN")

        return {
            "status": "REJECTED",
            "title": "NIN verification could not be completed",
            "message": message,
            "retryable": True,
        }

    if http_status >= 500:
        return {
            "status": "SERVICE_ERROR",
            "title": "NIN verification unavailable",
            "message": _service_message("NIN"),
            "retryable": True,
        }

    no_match_phrases = (
        "no match",
        "no matching",
        "not found",
        "does not exist",
        "could not find",
        "record not found",
    )

    mismatch_phrases = (
        "mismatch",
        "does not match",
        "not match",
        "incorrect",
        "invalid nin",
        "invalid information",
    )

    if provider_status != "successful":

        if any(
            phrase in text
            for phrase in no_match_phrases
        ):
            return {
                "status": "REJECTED",
                "title": "NIN verification could not be completed",
                "message": _not_found_message("NIN"),
                "retryable": True,
            }

        if any(
            phrase in text
            for phrase in mismatch_phrases
        ):
            return {
                "status": "REJECTED",
                "title": "NIN verification could not be completed",
                "message": _mismatch_message("NIN"),
                "retryable": True,
            }

        return {
            "status": "REJECTED",
            "title": "NIN verification could not be completed",
            "message": (
                "We were unable to verify your NIN using the "
                "information provided. Please confirm that your "
                "details are correct and try again."
            ),
            "retryable": True,
        }

    return {
        "status": "VERIFIED",
        "title": "NIN verification successful",
        "message": (
            "Your NIN verification has been completed successfully. "
            "Your identity has been verified and your artisan profile "
            "can now continue to the next stage."
        ),
        "retryable": False,
    }


def bvn_initiate_message(result):
    http_status = int(
        result.get("_http_status") or 0
    )

    provider_status = str(
        result.get("status") or "FAILED"
    ).lower()

    provider_message = _provider_message(result)
    text = provider_message.lower()

    if (
        "lookup app" in text
        or "lookup product" in text
    ):
        return _lookup_configuration_message("BVN")

    if http_status == 429:
        return _rate_limit_message("BVN")

    if http_status == 404:
        return _not_found_message("BVN")

    if http_status in {401, 403}:
        return _service_message("BVN")

    if http_status in {400, 422}:
        if any(
            phrase in text
            for phrase in (
                "no match",
                "no matching",
                "not found",
                "record not found",
            )
        ):
            return _not_found_message("BVN")

        return _invalid_information_message("BVN")

    if http_status >= 500:
        return _service_message("BVN")

    if provider_status != "successful":
        return (
            "We were unable to start your BVN verification "
            "at this time. Please confirm your details and "
            "try again. We apologise for the inconvenience."
        )

    return (
        "Your BVN verification session has been created "
        "successfully. Please continue with email verification."
    )


def bvn_otp_request_message(result):
    http_status = int(
        result.get("_http_status") or 0
    )

    provider_status = str(
        result.get("status") or "FAILED"
    ).lower()


    if http_status == 429:
        return _rate_limit_message("BVN")

    if http_status >= 500:
        return _service_message("BVN")

    if http_status in {401, 403}:
        return _service_message("BVN")

    if http_status in {400, 404, 422}:
        return _otp_send_failed_message()

    if provider_status != "successful":
        return _otp_send_failed_message()

    return (
        "A BVN verification code has been sent to your email. "
        "Please enter the latest code to complete your verification."
    )


def bvn_otp_result_message(result):
    http_status = int(
        result.get("_http_status") or 0
    )

    provider_status = str(
        result.get("status") or "FAILED"
    ).lower()


    if http_status == 429:
        return _rate_limit_message("BVN")

    if http_status >= 500:
        return _service_message("BVN")

    if http_status in {401, 403}:
        return _service_message("BVN")

    if http_status in {400, 404, 422}:
        return _otp_invalid_message()

    if provider_status != "successful":
        return _otp_invalid_message()

    return (
        "Your BVN verification has been completed successfully. "
        "Your identity has been verified."
    )


def nin_submitted_message():
    return (
        "Your NIN verification request has been received "
        "and is now being processed. Please check your "
        "verification status after 1 minute."
    )


def bvn_submitted_message():
    return (
        "Your BVN verification request has been received "
        "and is now being processed. Please check your "
        "verification status after 1 minute."
    )


def save_verification_result(artisan_id, result):
    cache.set(
        f"verification:result:{artisan_id}",
        result,
        RESULT_TTL,
    )


def get_verification_result(artisan_id):
    return cache.get(
        f"verification:result:{artisan_id}"
    )


def clear_verification_result(artisan_id):
    cache.delete(
        f"verification:result:{artisan_id}"
    )