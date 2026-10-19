from urllib.parse import unquote

from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm
from django.http import Http404
from django.urls import reverse
from django.utils.html import escape
from django.utils.translation import gettext as _

from core.audit import audit_request_action
from .generic import revoke_all_sessions, suspend_user, unsuspend_user
from .models import *


admin.site.register(CustomerProfile)
admin.site.register(Service)
admin.site.register(ServiceCategory)
admin.site.register(ArtisanService)


@admin.register(Locations)
class LocationsAdmin(admin.ModelAdmin):
    list_display = ["country", "state", "area"]
    list_filter = ["country", "state"]
    search_fields = ["country", "state", "area"]


@admin.register(ArtisanProfile)
class ArtisanProfileAdmin(admin.ModelAdmin):
    list_display = ["business_name", "user", "profile_status", "social_media_1_platform", "social_media_2_platform"]
    list_filter = ["profile_status", "social_media_1_platform", "social_media_2_platform"]
    search_fields = ["business_name", "user__email", "user__first_name", "user__last_name"]
    fieldsets = [
        ("Profile", {"fields": ("user", "business_name", "bio", "years_experience", "profile_photo", "profile_status")}),
        ("Registered location", {"fields": ("country", "state", "area")}),
        ("Professional links", {"fields": (
            "social_media_1_platform", "social_media_1_url",
            "social_media_2_platform", "social_media_2_url", "portfolio_url",
        )}),
        ("Marketplace metrics", {"fields": (
            "average_rating", "completion_rate", "cancellation_rate", "created_at", "updated_at",
        )}),
    ]
    readonly_fields = ["average_rating", "completion_rate", "cancellation_rate", "created_at", "updated_at"]


@admin.register(Institution)
class InstitutionAdmin(admin.ModelAdmin):
    list_display = ["name", "campus", "abbreviation", "location", "is_active"]
    list_filter = ["is_active", "location__country", "location__state"]
    search_fields = ["name", "campus", "abbreviation", "location__area", "location__state", "location__country"]
    autocomplete_fields = ["location"]


class CustomUserCreationForm(UserCreationForm):
    class Meta:
        model = CustomUser
        fields = ("email", "first_name", "last_name")

    def clean_email(self):
        email = self.cleaned_data["email"].strip().casefold()
        if CustomUser.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("A user with this email already exists.")
        return email


class CustomUserChangeForm(UserChangeForm):
    class Meta:
        model = CustomUser
        fields = "__all__"


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    """Retain Django admin while making account and privilege changes fail closed."""

    form = CustomUserChangeForm
    add_form = CustomUserCreationForm
    change_password_form = AdminPasswordChangeForm
    ordering = ("email",)
    list_display = ("email", "first_name", "last_name", "is_active", "is_staff", "security_status")
    list_filter = ("is_active", "is_staff", "is_superuser", "security_status")
    search_fields = ("email", "first_name", "last_name")
    actions = ("suspend_users", "unsuspend_users")
    filter_horizontal = ("groups", "user_permissions")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (_("Personal information"), {"fields": ("first_name", "last_name", "phone_number")}),
        (_("Account state (use explicit actions to change)"), {
            "fields": ("is_active", "security_status", "password_changed_at", "token_version"),
        }),
        (_("Django privilege metadata (read only)"), {
            "fields": ("is_staff", "is_superuser", "groups", "user_permissions"),
        }),
        (_("Important dates"), {"fields": ("last_login", "created_at", "updated_at")}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "first_name", "last_name", "password1", "password2"),
        }),
    )
    readonly_fields = (
        "is_active", "security_status", "password_changed_at", "token_version",
        "is_staff", "is_superuser", "groups", "user_permissions",
        "last_login", "created_at", "updated_at",
    )

    def has_module_permission(self, request):
        return bool(request.user.is_superuser)

    def has_view_permission(self, request, obj=None):
        return bool(request.user.is_superuser)

    def has_add_permission(self, request):
        return bool(request.user.is_superuser)

    def has_change_permission(self, request, obj=None):
        return bool(request.user.is_superuser)

    def has_delete_permission(self, request, obj=None):
        return bool(request.user.is_superuser)

    @admin.action(description="Suspend selected accounts and revoke sessions")
    def suspend_users(self, request, queryset):
        changed = 0
        for user in queryset.select_related():
            if user.is_superuser:
                continue
            suspend_user(user)
            self.log_change(request, user, "Suspended account; revoked marketplace sessions and pending security challenges.")
            audit_request_action(
                request=request,
                action="ADMIN_USER_SUSPENDED",
                resource_type="CustomUser",
                resource_id=str(user.pk),
                metadata={"origin": "django_admin"},
            )
            changed += 1
        self.message_user(request, f"Suspended {changed} account(s).", level=messages.SUCCESS)

    @admin.action(description="Restore selected accounts and revoke old sessions")
    def unsuspend_users(self, request, queryset):
        changed = 0
        for user in queryset.select_related():
            unsuspend_user(user)
            self.log_change(request, user, "Restored account; revoked old marketplace sessions and pending security challenges.")
            audit_request_action(
                request=request,
                action="ADMIN_USER_RESTORED",
                resource_type="CustomUser",
                resource_id=str(user.pk),
                metadata={"origin": "django_admin"},
            )
            changed += 1
        self.message_user(request, f"Restored {changed} account(s).", level=messages.SUCCESS)

    def user_change_password(self, request, id, form_url=""):
        user = self.get_object(request, unquote(str(id)))
        old_password = user.password if user is not None else None
        if user is None and request.method != "POST":
            raise Http404(_("User with primary key %(key)r does not exist.") % {"key": escape(id)})
        response = super().user_change_password(request, id, form_url)
        if request.method == "POST" and user is not None:
            user.refresh_from_db()
            if user.password != old_password:
                revoke_all_sessions(user)
                from .generic import _invalidate_staff_challenges

                _invalidate_staff_challenges(user)
                request.session.pop("admin_mfa_authenticated_at", None)
                request.session.pop("admin_mfa_user_id", None)
                request.session.modified = True
                audit_request_action(
                    request=request,
                    action="ADMIN_USER_PASSWORD_CHANGED",
                    resource_type="CustomUser",
                    resource_id=str(user.pk),
                    metadata={"origin": "django_admin"},
                )
        return response
