"""Browser step-up view for Django admin staff sessions."""

from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import user_passes_test
from django.contrib.auth.forms import PasswordChangeForm
from django import forms
from django.contrib.auth.models import update_last_login
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from rest_framework.exceptions import Throttled

from .admin_mfa import clear_admin_mfa_pending, ensure_admin_mfa_challenge
from core.audit import audit_request_action
from .generic import revoke_all_sessions, _invalidate_staff_challenges
from .password_policy import is_staff_account
from .staff_mfa import resend_staff_mfa_challenge, verify_staff_mfa_challenge


class AdminStaffMFAVerifyForm(forms.Form):
    code = forms.RegexField(
        regex=r"^[0-9]{6}$",
        min_length=6,
        max_length=6,
        label="Verification code",
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric"}),
    )


def _private(response):
    response["Cache-Control"] = "no-store, private"
    response["Pragma"] = "no-cache"
    response["Referrer-Policy"] = "no-referrer"
    return response


def _challenge_page(request, form, *, status=200):
    response = render(request, "admin/staff_mfa.html", {"form": form}, status=status)
    return _private(response)


@never_cache
@require_http_methods(["GET", "POST"])
def admin_staff_mfa(request):
    if not getattr(request.user, "is_authenticated", False) or not is_staff_account(request.user):
        return _private(redirect(f"{reverse('admin:login')}?next={reverse('admin-staff-mfa')}"))

    try:
        challenge = ensure_admin_mfa_challenge(request)
    except Throttled:
        return _challenge_page(request, AdminStaffMFAVerifyForm(), status=429)

    form = AdminStaffMFAVerifyForm(request.POST or None)
    action = request.POST.get("action", "verify") if request.method == "POST" else "verify"
    if request.method == "POST" and action == "resend":
        try:
            replacement, retry_after, sent = resend_staff_mfa_challenge(
                challenge_id=challenge.pk,
                request=request,
            )
        except Throttled:
            messages.warning(request, "Too many staff sign-in codes were requested. Wait before trying again.")
        else:
            if replacement is None:
                clear_admin_mfa_pending(request.session)
                messages.error(request, "The staff sign-in challenge expired. Start the admin sign-in again.")
                return _private(redirect(reverse("admin:login")))
            request.session["admin_mfa_challenge_id"] = str(replacement.pk)
            request.session["admin_mfa_masked_email"] = request.session.get("admin_mfa_masked_email", "")
            request.session["admin_mfa_resend_after"] = int(retry_after)
            request.session.modified = True
            messages.success(
                request,
                "A sign-in code was sent to the registered staff email."
                if sent
                else "A sign-in code was recently sent. Check the registered staff email.",
            )
        return _challenge_page(request, form)

    if request.method == "POST" and action == "verify" and form.is_valid():
        result = verify_staff_mfa_challenge(
            challenge_id=challenge.pk,
            code=form.cleaned_data["code"],
            request=request,
        )
        if result is None:
            messages.error(request, "That code is invalid or expired. Check it and try again, or request a new code.")
            return _challenge_page(request, form)

        user, now = result
        update_last_login(None, user)
        request.session.cycle_key()
        clear_admin_mfa_pending(request.session)
        request.session["admin_mfa_authenticated_at"] = int(now.timestamp())
        request.session["admin_mfa_user_id"] = str(user.pk)
        request.session.modified = True
        messages.success(request, "Staff verification complete. Django admin is available for this session.")
        return _private(redirect(reverse("admin:index")))

    return _challenge_page(request, form)


@never_cache
@require_http_methods(["GET", "POST"])
@user_passes_test(lambda user: bool(user.is_active and (user.is_staff or user.is_superuser)), login_url="/admin/login/")
def admin_staff_password_change(request):
    form = PasswordChangeForm(user=request.user, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        user = get_user_model().objects.get(pk=request.user.pk)
        revoke_all_sessions(user)
        _invalidate_staff_challenges(user)
        update_session_auth_hash(request, user)
        request.session.pop("admin_mfa_authenticated_at", None)
        request.session.pop("admin_mfa_user_id", None)
        request.session.modified = True
        audit_request_action(
            request=request,
            action="ADMIN_USER_PASSWORD_CHANGED",
            resource_type="CustomUser",
            resource_id=str(user.pk),
            metadata={"origin": "django_admin_password_change"},
        )
        messages.success(request, "Your password was changed. Verify your staff sign-in again to continue.")
        return _private(redirect(reverse("admin:index")))
    response = render(request, "admin/staff_password_change.html", {"form": form})
    return _private(response)
