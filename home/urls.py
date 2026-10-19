from django.urls import path
from .views import *
from .manager_views import ManagerOverviewView, ManagerSnapshotScheduleView
from .staff_mfa_views import StaffMFAVerifyView, StaffMFAResendView
from .staff_security_views import (
    StaffPasswordChangeView,
    StaffPasswordResetCompleteView,
    StaffPasswordResetInitiateView,
    StaffPasswordResetVerifyView,
    StaffSecurityQuestionOptionsView,
    StaffSecurityQuestionSetupInitiateView,
    StaffSecurityQuestionsView,
    StaffSecuritySetupCompleteView,
)

urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path("verify-otp/", VerifyOTPView.as_view(), name="verify-otp"),
    path("request-otp/", RequestOTPView.as_view(), name="request-otp"),
    path("login/", LoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("forgot-password/", ForgotPasswordView.as_view(), name="forgot-password"),
    path("verify-reset-otp/", VerifyResetOTPView.as_view(),name="verify-reset-otp"),
    path("reset-password/", ResetPasswordView.as_view(), name="reset-password"),
    path("change-password/", StaffPasswordChangeView.as_view(), name="change-password"),
    path("staff-security-question-options/", StaffSecurityQuestionOptionsView.as_view(), name="staff-security-question-options"),
    path("staff-security-questions/", StaffSecurityQuestionsView.as_view(), name="staff-security-questions"),
    path("staff-password-resets/<int:user_id>/initiate/", StaffPasswordResetInitiateView.as_view(), name="staff-password-reset-initiate"),
    path("staff-password-resets/<int:user_id>/security-setup/", StaffSecurityQuestionSetupInitiateView.as_view(), name="staff-security-setup-initiate"),
    path("staff-password-resets/security-setup/complete/", StaffSecuritySetupCompleteView.as_view(), name="staff-security-setup-complete"),
    path("staff-password-resets/verify/", StaffPasswordResetVerifyView.as_view(), name="staff-password-reset-verify"),
    path("staff-password-resets/complete/", StaffPasswordResetCompleteView.as_view(), name="staff-password-reset-complete"),
    path("staff-mfa/verify/", StaffMFAVerifyView.as_view(), name="staff-mfa-verify"),
    path("staff-mfa/resend/", StaffMFAResendView.as_view(), name="staff-mfa-resend"),
    path("token/refresh/", MarketplaceTokenRefreshView.as_view(), name="token-refresh"),
    path("manager/overview/", ManagerOverviewView.as_view(), name="manager-overview"),
    path("manager/snapshot-schedule/", ManagerSnapshotScheduleView.as_view(), name="manager-snapshot-schedule"),

]
