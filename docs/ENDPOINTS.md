# API endpoints

Generated from the URL configuration by `scripts/generate_endpoint_docs.py` — do not edit by hand.

| Route | Methods | View | Permissions | Throttle scope |
|---|---|---|---|---|
| `/api/v1/booking/` | GET, POST | `BookingListCreateView` | IsAuthenticated | — |
| `/api/v1/booking/<uuid:booking_id>/additional-orders/` | POST | `BookingAdditionalOrderCreateView` | IsAuthenticated, IsBookingParticipant | — |
| `/api/v1/booking/<uuid:booking_id>/additional-orders/<uuid:order_id>/offers/<uuid:offer_id>/respond/` | POST | `BookingAdditionalOrderRespondView` | IsAuthenticated, IsBookingParticipant | — |
| `/api/v1/booking/<uuid:pk>/` | GET | `BookingDetailView` | IsAuthenticated, IsBookingParticipant | — |
| `/api/v1/booking/<uuid:pk>/accept/` | POST | `BookingAcceptView` | IsAuthenticated | — |
| `/api/v1/booking/<uuid:pk>/cancel/` | POST | `BookingCancelView` | IsAuthenticated | — |
| `/api/v1/booking/<uuid:pk>/complete/` | POST | `BookingCompleteView` | IsAuthenticated | — |
| `/api/v1/booking/<uuid:pk>/confirm-completion/` | POST | `BookingCustomerCompleteView` | IsAuthenticated | — |
| `/api/v1/booking/<uuid:pk>/dispute/` | POST | `BookingDisputeView` | IsAuthenticated | — |
| `/api/v1/booking/<uuid:pk>/history/` | GET | `BookingHistoryView` | IsAuthenticated | — |
| `/api/v1/booking/<uuid:pk>/reject/` | POST | `BookingRejectView` | IsAuthenticated | — |
| `/api/v1/booking/<uuid:pk>/start/` | POST | `BookingStartView` | IsAuthenticated | — |
| `/api/v1/conversations/` | GET, POST | `ConversationCreateView` | IsAuthenticated | conversation_start |
| `/api/v1/conversations/<uuid:conversation_id>/` | GET | `ConversationDetailView` | IsAuthenticated, IsConversationParticipant | — |
| `/api/v1/conversations/<uuid:conversation_id>/dispute/` | POST | `ConversationDisputeView` | IsAuthenticated, IsConversationParticipant | conversation_dispute |
| `/api/v1/conversations/<uuid:conversation_id>/messages/` | GET, POST | `ConversationMessageListCreateView` | IsAuthenticated, IsConversationParticipant | conversation_message |
| `/api/v1/conversations/<uuid:conversation_id>/offers/` | GET, POST | `ConversationOfferListView` | IsAuthenticated, IsConversationParticipant | conversation_offer |
| `/api/v1/conversations/<uuid:conversation_id>/offers/<uuid:offer_id>/accept/` | POST | `ConversationOfferAcceptView` | IsAuthenticated, IsConversationParticipant | conversation_offer |
| `/api/v1/conversations/<uuid:conversation_id>/offers/<uuid:offer_id>/decline/` | POST | `ConversationOfferDeclineView` | IsAuthenticated, IsConversationParticipant | conversation_offer |
| `/api/v1/conversations/<uuid:conversation_id>/offers/counter/` | POST | `ConversationOfferCounterView` | IsAuthenticated, IsConversationParticipant | conversation_counter |
| `/api/v1/conversations/<uuid:conversation_id>/payment/initialize/` | POST | `ConversationPaymentInitializeView` | IsAuthenticated, IsConversationParticipant | payment_initialize |
| `/api/v1/conversations/<uuid:conversation_id>/read/` | POST | `ConversationReadView` | IsAuthenticated, IsConversationParticipant | conversation_read |
| `/api/v1/conversations/<uuid:conversation_id>/request/` | PATCH | `ConversationRequestView` | IsAuthenticated, IsConversationParticipant | conversation_request |
| `/api/v1/conversations/direct/artisan/<uuid:artisan_id>/` | POST | `ConversationDirectArtisanView` | IsAuthenticated | conversation_start |
| `/api/v1/conversations/inbox/` | GET | `ConversationListView` | IsAuthenticated | — |
| `/api/v1/conversations/start/` | POST | `ConversationStartView` | IsAuthenticated | conversation_start |
| `/api/v1/docs/` | GET | `SpectacularSwaggerView` | AllowAny | — |
| `/api/v1/forgot-password/` | POST | `ForgotPasswordView` | AnonymousOnly | auth_forgot_password |
| `/api/v1/login/` | POST | `LoginView` | AnonymousOnly | auth_login |
| `/api/v1/logout/` | POST | `LogoutView` | IsAuthenticated | — |
| `/api/v1/notifications/` | GET | `NotificationListView` | IsAuthenticated | — |
| `/api/v1/notifications/<int:pk>/read/` | POST | `NotificationReadView` | IsAuthenticated | notification_read |
| `/api/v1/notifications/read-all/` | POST | `NotificationReadAllView` | IsAuthenticated | notification_read |
| `/api/v1/notifications/unread-count/` | GET | `NotificationUnreadCountView` | IsAuthenticated | notification_read |
| `/api/v1/payments/<uuid:payment_id>/` | GET | `PaymentDetailView` | IsAuthenticated | — |
| `/api/v1/payments/additional-orders/<uuid:order_id>/initialize/` | POST | `InitializeAdditionalOrderPaymentView` | IsAuthenticated | payment_initialize |
| `/api/v1/payments/artisan/earnings/summary/` | GET | `ArtisanEarningsSummaryView` | IsAuthenticated | — |
| `/api/v1/payments/booking/<uuid:booking_id>/initialize/` | POST | `InitializeBookingPaymentView` | IsAuthenticated | payment_initialize |
| `/api/v1/payments/flutterwave/callback/` | GET | `FlutterwaveCallbackView` | default (IsAuthenticated) | payment_callback |
| `/api/v1/payments/flutterwave/webhook/` | (function view) | `flutterwave_webhook` | default (IsAuthenticated) | — |
| `/api/v1/payments/payout-account/` | GET, PUT, PATCH | `ArtisanPayoutAccountView` | IsAuthenticated | — |
| `/api/v1/payments/payout-account/<uuid:pk>/verify/` | POST | `ArtisanPayoutAccountStatusView` | SupportFinancePermission | — |
| `/api/v1/profile/artisan/details/` | GET | `ArtisanDiscoveryListView` | IsAuthenticated | — |
| `/api/v1/profile/artisan/details/<uuid:pk>/` | GET | `ArtisanDiscoveryDetailView` | IsAuthenticated | — |
| `/api/v1/profile/artisan/personal/profile/` | GET, PATCH | `ArtisanProfileView` | IsAuthenticated | — |
| `/api/v1/profile/customer/personal/profile/` | GET, PATCH | `CustomerProfileView` | IsAuthenticated | — |
| `/api/v1/redoc/` | GET | `SpectacularRedocView` | AllowAny | — |
| `/api/v1/register/` | POST | `RegisterView` | AnonymousOnly | auth_register |
| `/api/v1/request-otp/` | POST | `RequestOTPView` | AnonymousOnly | auth_request_otp |
| `/api/v1/reset-password/` | POST | `ResetPasswordView` | AllowAny | auth_reset_password |
| `/api/v1/reviews/` | POST | `ReviewCreateView` | IsAuthenticated | — |
| `/api/v1/risk/customer/cancellation-risk/` | GET | `CustomerCancellationRiskView` | IsAuthenticated | — |
| `/api/v1/schema/` | GET | `SpectacularAPIView` | AllowAny | — |
| `/api/v1/services/artisan/profile/<uuid:pk>/activate/` | POST | `ArtisanActivationView` | IsAdminUser | — |
| `/api/v1/services/artisan/requests/` | GET | `ArtisanMatchedServiceRequestListView` | IsAuthenticated | — |
| `/api/v1/services/categories/` | GET | `ServiceCategoryListView` | IsAuthenticated | — |
| `/api/v1/services/get_artisan_services/` | GET, POST | `ArtisanServiceListCreateView` | IsAuthenticated | — |
| `/api/v1/services/get_artisan_services/<int:pk>/` | GET, PUT, PATCH, DELETE | `ArtisanServiceDetailView` | IsAuthenticated | — |
| `/api/v1/services/locations/` | GET, POST | `LocationListCreateView` | IsAuthenticated | — |
| `/api/v1/services/locations/<uuid:pk>/` | GET, PUT, PATCH, DELETE | `LocationDetailView` | IsAuthenticated | — |
| `/api/v1/services/requests/` | GET, POST | `ServiceRequestListCreateView` | IsAuthenticated | — |
| `/api/v1/services/requests/<int:pk>/` | GET, PUT, PATCH | `ServiceRequestDetailView` | IsAuthenticated | — |
| `/api/v1/services/service_cat/` | GET | `ServiceListView` | IsAuthenticated | — |
| `/api/v1/services/verification/` | GET | `ArtisanVerificationStatusView` | IsAuthenticated | — |
| `/api/v1/services/verification/bvn/details/` | POST | `BVNDetailsView` | IsAuthenticated | verification_bvn_details |
| `/api/v1/services/verification/bvn/initiate/` | POST | `BVNInitiateView` | IsAuthenticated | — |
| `/api/v1/services/verification/bvn/method/` | POST | `BVNMethodView` | IsAuthenticated | verification_bvn_method |
| `/api/v1/services/verification/nin/` | POST | `NINVerificationView` | IsAuthenticated | verification_nin |
| `/api/v1/social-auth/<str:provider>/native/complete/` | POST | `NativeSocialCompleteView` | AnonymousOnly | — |
| `/api/v1/social-auth/<str:provider>/native/start/` | POST | `NativeSocialStartView` | AnonymousOnly | — |
| `/api/v1/social-auth/<str:provider>/web/complete/` | POST | `WebSocialCompleteView` | AnonymousOnly | — |
| `/api/v1/social-auth/<str:provider>/web/start/` | POST | `WebSocialStartView` | AnonymousOnly | — |
| `/api/v1/social-auth/complete-profile/` | POST | `NativeSocialCompleteProfileView` | AnonymousOnly | — |
| `/api/v1/social-auth/native/complete-profile/` | POST | `NativeSocialCompleteProfileView` | AnonymousOnly | — |
| `/api/v1/support/cases/` | GET, POST | `SupportCaseListCreateView` | IsAuthenticated | support_create |
| `/api/v1/support/cases/<int:pk>/` | GET | `SupportCaseDetailView` | IsAuthenticated | — |
| `/api/v1/support/cases/<int:pk>/assign/` | POST | `SupportCaseAssignView` | SupportSupervisorPermission | — |
| `/api/v1/support/cases/<int:pk>/claim/` | POST | `SupportCaseClaimView` | SupportAgentPermission | — |
| `/api/v1/support/cases/<int:pk>/close/` | POST | `SupportCaseCloseView` | SupportSupervisorPermission | — |
| `/api/v1/support/cases/<int:pk>/eligible-payments/` | GET | `SupportCaseEligiblePaymentsView` | SupportAgentPermission | — |
| `/api/v1/support/cases/<int:pk>/events/` | GET | `SupportCaseEventsView` | IsAuthenticated | — |
| `/api/v1/support/cases/<int:pk>/messages/` | POST | `SupportCaseMessageView` | IsAuthenticated | support_message |
| `/api/v1/support/cases/<int:pk>/notes/` | POST | `SupportCaseNoteView` | SupportAgentPermission | — |
| `/api/v1/support/cases/<int:pk>/refunds/` | POST | `SupportRefundRequestView` | SupportAgentPermission | support_refund_request |
| `/api/v1/support/cases/<int:pk>/resolve/` | POST | `SupportCaseResolveView` | SupportSupervisorPermission | — |
| `/api/v1/support/cases/capabilities/` | GET | `SupportCapabilitiesView` | IsAuthenticated | — |
| `/api/v1/support/cases/refunds/` | GET | `SupportRefundListView` | SupportRolePermission | — |
| `/api/v1/support/cases/refunds/<uuid:refund_id>/` | GET | `SupportRefundDetailView` | IsAuthenticated | — |
| `/api/v1/support/cases/refunds/<uuid:refund_id>/approve/` | POST | `SupportRefundApproveView` | SupportSupervisorPermission | support_refund_approve |
| `/api/v1/support/cases/refunds/<uuid:refund_id>/execute/` | POST | `SupportRefundExecuteView` | SupportFinancePermission | support_refund_execute |
| `/api/v1/support/cases/refunds/<uuid:refund_id>/reconcile/` | POST | `SupportRefundReconcileView` | SupportFinancePermission | support_refund_reconcile |
| `/api/v1/support/cases/refunds/<uuid:refund_id>/reject/` | POST | `SupportRefundRejectView` | SupportSupervisorPermission | support_refund_reject |
| `/api/v1/support/cases/staff/` | GET | `SupportStaffListView` | SupportSupervisorPermission | — |
| `/api/v1/support/cases/staff/<int:user_id>/roles/` | PUT | `SupportStaffRoleView` | SupportAdminPermission | — |
| `/api/v1/token/refresh/` | POST | `MarketplaceTokenRefreshView` | default (IsAuthenticated) | — |
| `/api/v1/verify-otp/` | POST | `VerifyOTPView` | AnonymousOnly | auth_verify_otp |
| `/api/v1/verify-reset-otp/` | POST | `VerifyResetOTPView` | AllowAny | auth_verify_reset |

## Promotion applications and support review

Artisan promotion applications are separate from payment and campaign activation. The promotion type values are `INSTITUTION`, `AREA`, `STATE`, and `HOMEPAGE`; each application binds to only its own supported target and selected service offerings.

| Route | Methods | Access | Notes |
| --- | --- | --- | --- |
| `/api/v1/promotions/eligibility/` | GET | Authenticated active artisan profile | Returns current profile-completion, verification, Active-status, account-security, and active-service requirements. |
| `/api/v1/promotions/applications/` | GET, POST | Authenticated active artisan profile | List the artisan's applications or submit one. Product-specific targets use institution/location IDs from the existing catalogues; state uses an exact supported country/state pair. Supports up to five private image work samples. |
| `/api/v1/promotions/applications/<uuid:pk>/` | GET, PATCH | Application owner | PATCH creates a new immutable revision and is accepted only for `CHANGES_REQUIRED` applications. Browser-supplied lifecycle or verification status is never written. |
| `/api/v1/promotions/applications/<uuid:pk>/withdraw/` | POST | Application owner | Withdraw an application in an allowed lifecycle state. |
| `/api/v1/promotions/applications/<uuid:application_id>/evidence/<uuid:evidence_id>/` | GET | Application owner or authorized promotion reviewer | Streams private optimized evidence; other authenticated users are denied. |
| `/api/v1/promotions/review/applications/` | GET | Customer Support agent, supervisor, or admin | Queue filters: `status` (defaults to `PENDING_REVIEW`; `ALL` supported), `promotion_type`, `q`, `limit`, and `offset`. Finance-only staff cannot review. |
| `/api/v1/promotions/review/applications/<uuid:pk>/` | GET | Authorized promotion reviewer | Returns immutable revision snapshots, selected services, profile snapshot, evidence links, and applicant-visible decision history. |
| `/api/v1/promotions/review/applications/<uuid:pk>/decision/` | POST | Authorized promotion reviewer | Body: `{"decision":"APPROVED|CHANGES_REQUIRED|REJECTED","reason":"Applicant-facing explanation"}`. Approval rechecks eligibility and exact selected target/service scope. |

Application lifecycle values are `PENDING_REVIEW`, `CHANGES_REQUIRED`, `APPROVED`, `REJECTED`, and `WITHDRAWN`. Submission and decision notifications are sent to the applicant; no staff-only note is included in those notifications. Approval is scoped to the reviewed application only and does not initiate pricing, payment, scheduling, or campaign activation. The artisan page is linked from profile management, and the authorized queue is available in Support Operations.
