from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q, Subquery
from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse, OpenApiTypes, extend_schema, inline_serializer
from rest_framework import generics, serializers, status
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from booking.models import Booking
from core.audit import audit_request_action
from home.profile_completion import RequireCompleteCustomerProfile, RequireCompleteMarketplaceProfile
from core.idempotency import begin, finish, idempotent_action
from payments.models import EscrowPayment
from payments.tasks import initialize_prepared_payment
from .models import Conversation, ConversationReadState, Message, Offer
from .permissions import IsConversationParticipant
from .serializers import ConversationCreateSerializer, ConversationDetailSerializer, ConversationDisputeSerializer, ConversationListSerializer, ConversationRequestPatchSerializer, ConversationStartSerializer, CounterOfferSerializer, ConversationMessageCreateResponseSerializer, MessageCreateSerializer, MessageSerializer, OfferCreateSerializer, OfferSerializer
from .services import _booking_for_conversation, accept_offer, apply_request_patch, create_offer, decline_offer, get_conversation_for_user, hydrate_conversation, mark_conversation_read, open_conversation_dispute, send_message, start_conversation, start_direct_conversation

MessagePageSerializer = inline_serializer(name="ConversationMessagePage", fields={"count": serializers.IntegerField(), "next": serializers.URLField(allow_null=True), "previous": serializers.URLField(allow_null=True), "results": MessageSerializer(many=True)})
OfferPageSerializer = inline_serializer(name="ConversationOfferPage", fields={"count": serializers.IntegerField(), "next": serializers.URLField(allow_null=True), "previous": serializers.URLField(allow_null=True), "results": OfferSerializer(many=True)})

def _require_key(request):
    key = request.headers.get("Idempotency-Key", "").strip()
    if not key: return None, Response({"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "Idempotency-Key is required for this operation."}, status=status.HTTP_400_BAD_REQUEST)
    return key, None

class ConversationCreateView(APIView):
    permission_classes = [IsAuthenticated, RequireCompleteMarketplaceProfile]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "conversation_start"

    @extend_schema(
        tags=["Conversations"],
        request=None,
        responses={200: ConversationListSerializer(many=True)},
    )
    def get(self, request, *args, **kwargs):
        return ConversationListView.as_view()(request._request, *args, **kwargs)

    @extend_schema(
        tags=["Conversations"],
        request=ConversationCreateSerializer,
        parameters=[OpenApiParameter("Idempotency-Key", OpenApiTypes.STR, OpenApiParameter.HEADER, required=True)],
        responses={201: ConversationDetailSerializer, 200: ConversationDetailSerializer, 400: OpenApiResponse(description="Invalid conversation payload."), 403: OpenApiResponse(description="Conversation is not allowed.")},
    )
    @idempotent_action("conversation.create", resource_type="Conversation")
    def post(self, request):
        serializer = ConversationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        kind = data.get("conversation_type", Conversation.ConversationType.DIRECT)

        if kind == Conversation.ConversationType.SERVICE:
            conversation, created = start_conversation(
                user=request.user,
                artisan_id=data["artisan_id"],
                service_id=data.get("service_id"),
            )
        else:
            participant_ids = list(dict.fromkeys(data.get("participants") or []))
            if request.user.id not in participant_ids:
                participant_ids.insert(0, request.user.id)
            if len(participant_ids) != 2:
                return Response({"detail": "A direct conversation must contain exactly two users."}, status=status.HTTP_400_BAD_REQUEST)
            target_id = participant_ids[1] if participant_ids[0] == request.user.id else participant_ids[0]
            from django.contrib.auth import get_user_model
            User = get_user_model()
            try:
                target = User.objects.get(pk=target_id)
            except User.DoesNotExist:
                return Response({"detail": "The selected participant was not found."}, status=status.HTTP_404_NOT_FOUND)
            conversation, created = start_direct_conversation(user=request.user, target_user=target)
            initial_message = data.get("initial_message", "").strip()
            if initial_message and created:
                key = request.headers.get("Idempotency-Key", "").strip()
                send_message(conversation=conversation, user=request.user, body=initial_message, idempotency_key=f"{key}:initial")

        hydrate_conversation(conversation, user=request.user, unread_count=True)
        response = Response(ConversationDetailSerializer(conversation, context={"request": request}).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
        response._conversation_created = created
        return response


class ConversationDirectArtisanView(APIView):
    permission_classes = [IsAuthenticated, RequireCompleteMarketplaceProfile]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "conversation_start"

    @extend_schema(
        tags=["Conversations"],
        request=inline_serializer(name="DirectArtisanMessage", fields={"initial_message": serializers.CharField(required=False, allow_blank=True, max_length=5000)}),
        parameters=[OpenApiParameter("Idempotency-Key", OpenApiTypes.STR, OpenApiParameter.HEADER, required=True)],
        responses={201: ConversationDetailSerializer, 200: ConversationDetailSerializer},
    )
    @idempotent_action("conversation.direct_artisan.create", resource_type="Conversation")
    def post(self, request, artisan_id):
        message = str(request.data.get("initial_message", "") or "").strip()
        conversation, created = start_direct_conversation(user=request.user, artisan_id=artisan_id)
        if message and created:
            key = request.headers.get("Idempotency-Key", "").strip()
            send_message(conversation=conversation, user=request.user, body=message, idempotency_key=f"{key}:initial")
        hydrate_conversation(conversation, user=request.user, unread_count=True)
        return Response(ConversationDetailSerializer(conversation, context={"request": request}).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class ConversationStartView(APIView):
    permission_classes = [IsAuthenticated, RequireCompleteCustomerProfile]; throttle_classes = [ScopedRateThrottle]; throttle_scope = "conversation_start"
    @extend_schema(tags=["Conversations"], request=ConversationStartSerializer, responses={201: ConversationDetailSerializer, 200: ConversationDetailSerializer, 400: OpenApiResponse(description="Invalid artisan or service."), 403: OpenApiResponse(description="Customer or artisan eligibility failure.")})
    def post(self, request):
        serializer = ConversationStartSerializer(data=request.data); serializer.is_valid(raise_exception=True)
        conversation, created = start_conversation(user=request.user, artisan_id=serializer.validated_data["artisan_id"], service_id=serializer.validated_data.get("service_id")); hydrate_conversation(conversation, user=request.user, unread_count=True)
        audit_request_action(request=request, action="CONVERSATION_STARTED" if created else "CONVERSATION_RESUMED", resource_type="Conversation", resource_id=str(conversation.id), metadata={"artisan_id": str(conversation.artisan_id)})
        return Response(ConversationDetailSerializer(conversation, context={"request": request}).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

class ConversationListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]; serializer_class = ConversationListSerializer; ordering = ["-updated_at"]
    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Conversation.objects.none()
        user = self.request.user; now = timezone.now(); latest_message = Message.objects.filter(conversation=OuterRef("pk")).order_by("-created_at"); latest_booking = Booking.objects.filter(request_id=OuterRef("service_request_id")).order_by("-created_at"); active_offer = Offer.objects.filter(conversation=OuterRef("pk"), status=Offer.Status.PROPOSED, expires_at__gt=now); payment = EscrowPayment.objects.filter(booking__request_id=OuterRef("service_request_id"), addon_order__isnull=True).order_by("-created_at")
        return Conversation.objects.select_related("customer__artisan_profile", "customer__customer_profile", "artisan__user__artisan_profile", "artisan__user__customer_profile").prefetch_related("participants__artisan_profile", "participants__customer_profile").filter(Q(participants=user) | Q(customer=user) | Q(artisan__user=user)).distinct().annotate(last_message_at=Subquery(latest_message.values("created_at")[:1]), last_message_sender_id=Subquery(latest_message.values("sender_id")[:1]), last_message_body=Subquery(latest_message.values("body")[:1]), last_message_type=Subquery(latest_message.values("message_type")[:1]), booking_status=Subquery(latest_booking.values("status")[:1]), payment_status=Subquery(payment.values("status")[:1]), has_active_offer=Exists(active_offer)).order_by("-updated_at")
    @extend_schema(tags=["Conversations"], responses={200: ConversationListSerializer(many=True)})
    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        conversations = list(page if page is not None else queryset)
        ids = [conversation.id for conversation in conversations]
        read_map = {
            row.conversation_id: row.last_read_at
            for row in ConversationReadState.objects.filter(user=request.user, conversation_id__in=ids)
        } if ids else {}

        unread_filter = Q()
        for conversation in conversations:
            read_at = read_map.get(conversation.id)
            message_filter = Q(conversation_id=conversation.id)
            if read_at is not None:
                message_filter &= Q(created_at__gt=read_at)
            unread_filter |= message_filter

        unread_counts = {}
        if ids:
            unread_counts = dict(
                Message.objects.filter(unread_filter)
                .exclude(sender=request.user)
                .values("conversation_id")
                .annotate(message_count=Count("pk"))
                .values_list("conversation_id", "message_count")
            )

        for conversation in conversations:
            booking_status = getattr(conversation, "booking_status", None)
            payment_status = getattr(conversation, "payment_status", None)
            if booking_status == Booking.Status.DISPUTED or payment_status == EscrowPayment.Status.DISPUTED:
                state = "DISPUTED"
            elif booking_status == Booking.Status.COMPLETED:
                state = "COMPLETED"
            elif booking_status in {Booking.Status.IN_PROGRESS, Booking.Status.COMPLETION_REQUESTED} or payment_status == EscrowPayment.Status.HELD:
                state = "ACTIVE"
            elif booking_status in {Booking.Status.REQUESTED, Booking.Status.ACCEPTED} or payment_status in {
                EscrowPayment.Status.PAYMENT_PENDING,
                EscrowPayment.Status.RELEASE_PENDING,
                EscrowPayment.Status.REFUND_PENDING,
            }:
                state = "BOOKING"
            elif booking_status in {Booking.Status.CANCELLED, Booking.Status.REJECTED}:
                state = "CLOSED"
            elif getattr(conversation, "has_active_offer", False):
                state = "NEGOTIATING"
            else:
                state = "OPEN"

            conversation._conversation_state = state
            conversation._last_message_at = getattr(conversation, "last_message_at", None)
            read_at = read_map.get(conversation.id)
            latest_sender = getattr(conversation, "last_message_sender_id", None)
            # Keep the legacy boolean semantics: the latest message must be from another participant.
            conversation._unread = bool(
                conversation._last_message_at
                and (not read_at or conversation._last_message_at > read_at)
                and latest_sender != request.user.id
            )
            # The numeric count follows the per-user read watermark used by message retrieval.
            conversation._unread_count = int(unread_counts.get(conversation.id, 0))

        payload = [ConversationListSerializer(item, context={"request": request}).data for item in conversations]
        return self.get_paginated_response(payload) if page is not None else Response(payload)

class ConversationDetailView(APIView):
    permission_classes = [IsAuthenticated, IsConversationParticipant]
    @extend_schema(tags=["Conversations"], responses={200: ConversationDetailSerializer, 404: OpenApiResponse(description="Conversation not found.")})
    def get(self, request, conversation_id):
        conversation = get_conversation_for_user(user=request.user, conversation_id=conversation_id); hydrate_conversation(conversation, user=request.user, unread_count=True); return Response(ConversationDetailSerializer(conversation, context={"request": request}).data)

class ConversationMessageListCreateView(APIView):
    permission_classes = [IsAuthenticated, IsConversationParticipant, RequireCompleteMarketplaceProfile]; throttle_classes = [ScopedRateThrottle]; throttle_scope = "conversation_message"; parser_classes = [MultiPartParser, FormParser, JSONParser]
    @extend_schema(tags=["Conversations"], responses={200: MessagePageSerializer}, parameters=[OpenApiParameter("limit", OpenApiTypes.INT, OpenApiParameter.QUERY, description="Maximum messages to return (max 100)."), OpenApiParameter("offset", OpenApiTypes.INT, OpenApiParameter.QUERY, description="Number of messages to skip.")])
    def get(self, request, conversation_id):
        conversation = get_conversation_for_user(user=request.user, conversation_id=conversation_id); qs = Message.objects.filter(conversation=conversation).select_related("sender").prefetch_related("attachments").order_by("-created_at"); paginator = LimitOffsetPagination(); paginator.default_limit = 50; paginator.max_limit = 100; page = paginator.paginate_queryset(qs, request, view=self); return paginator.get_paginated_response(MessageSerializer(page, many=True, context={"request": request}).data)
    @extend_schema(tags=["Conversations"], request=MessageCreateSerializer, parameters=[OpenApiParameter("Idempotency-Key", OpenApiTypes.STR, OpenApiParameter.HEADER, required=True)], responses={201: ConversationMessageCreateResponseSerializer, 400: OpenApiResponse(description="Invalid message or attachment."), 403: OpenApiResponse(description="Not a conversation participant.")}, examples=[OpenApiExample("Text message", request_only=True, value={"body": "The sockets in the bedroom have stopped working."})])
    def post(self, request, conversation_id):
        key, error = _require_key(request)
        if error: return error
        conversation = get_conversation_for_user(user=request.user, conversation_id=conversation_id); serializer = MessageCreateSerializer(data=request.data); serializer.is_valid(raise_exception=True); attachments = serializer.validated_data.get("attachments", request.FILES.getlist("attachments")); message = send_message(conversation=conversation, user=request.user, body=serializer.validated_data.get("body", ""), attachments=attachments, idempotency_key=key); conversation = get_conversation_for_user(user=request.user, conversation_id=conversation_id); hydrate_conversation(conversation, user=request.user, unread_count=True); from .automation import assistant_context; assistant = assistant_context(conversation=conversation, user=request.user, booking=getattr(conversation, "_booking", None), offer=getattr(conversation, "_active_offer", None), payment=getattr(getattr(conversation, "_booking", None), "escrow_payment", None)); data = MessageSerializer(message, context={"request": request}).data; data["assistant"] = assistant; return Response(data, status=status.HTTP_201_CREATED)

class ConversationRequestView(APIView):
    permission_classes = [IsAuthenticated, IsConversationParticipant, RequireCompleteCustomerProfile]; throttle_classes = [ScopedRateThrottle]; throttle_scope = "conversation_request"
    @extend_schema(tags=["Conversations"], request=ConversationRequestPatchSerializer, responses={200: ConversationDetailSerializer, 400: OpenApiResponse(description="Invalid or incomplete request data."), 403: OpenApiResponse(description="Only the customer can edit request details."), 409: OpenApiResponse(description="Request is locked by an active booking.")})
    def patch(self, request, conversation_id):
        conversation = get_conversation_for_user(user=request.user, conversation_id=conversation_id); serializer = ConversationRequestPatchSerializer(data=request.data); serializer.is_valid(raise_exception=True); _, conversation = apply_request_patch(conversation=conversation, user=request.user, payload=serializer.validated_data); hydrate_conversation(conversation, user=request.user, unread_count=True); return Response(ConversationDetailSerializer(conversation, context={"request": request}).data)

class ConversationOfferListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated, IsConversationParticipant]; throttle_classes = [ScopedRateThrottle]; throttle_scope = "conversation_offer"; serializer_class = OfferSerializer; pagination_class = LimitOffsetPagination
    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Offer.objects.none()
        conversation = get_conversation_for_user(user=self.request.user, conversation_id=self.kwargs["conversation_id"]); return conversation.offers.select_related("created_by", "parent_offer").order_by("-created_at")
    @extend_schema(tags=["Conversations"], responses={200: OfferPageSerializer})
    def get(self, request, *args, **kwargs): return super().get(request, *args, **kwargs)
    @extend_schema(tags=["Conversations"], request=OfferCreateSerializer, parameters=[OpenApiParameter("Idempotency-Key", OpenApiTypes.STR, OpenApiParameter.HEADER, required=True)], responses={201: OfferSerializer, 400: OpenApiResponse(description="Invalid offer."), 403: OpenApiResponse(description="Only the artisan can create the initial offer.")})
    def post(self, request, conversation_id):
        key, error = _require_key(request)
        if error: return error
        conversation = get_conversation_for_user(user=request.user, conversation_id=conversation_id); serializer = OfferCreateSerializer(data=request.data); serializer.is_valid(raise_exception=True); offer = create_offer(conversation=conversation, user=request.user, payload=serializer.validated_data, idempotency_key=key); return Response(OfferSerializer(offer).data, status=status.HTTP_201_CREATED)

class ConversationOfferCounterView(APIView):
    permission_classes = [IsAuthenticated, IsConversationParticipant]; throttle_classes = [ScopedRateThrottle]; throttle_scope = "conversation_counter"
    @extend_schema(tags=["Conversations"], request=CounterOfferSerializer, parameters=[OpenApiParameter("Idempotency-Key", OpenApiTypes.STR, OpenApiParameter.HEADER, required=True)], responses={201: OfferSerializer})
    def post(self, request, conversation_id):
        key, error = _require_key(request)
        if error: return error
        conversation = get_conversation_for_user(user=request.user, conversation_id=conversation_id); serializer = CounterOfferSerializer(data=request.data); serializer.is_valid(raise_exception=True); data = dict(serializer.validated_data); parent_id = data.pop("parent_offer_id"); offer = create_offer(conversation=conversation, user=request.user, payload=data, parent_offer_id=parent_id, idempotency_key=key); return Response(OfferSerializer(offer).data, status=status.HTTP_201_CREATED)

class ConversationOfferAcceptView(APIView):
    permission_classes = [IsAuthenticated, IsConversationParticipant]; throttle_classes = [ScopedRateThrottle]; throttle_scope = "conversation_offer"
    @extend_schema(tags=["Conversations"], request=None, parameters=[OpenApiParameter("Idempotency-Key", OpenApiTypes.STR, OpenApiParameter.HEADER, required=True)], responses={200: ConversationDetailSerializer, 400: OpenApiResponse(description="Offer cannot be accepted."), 403: OpenApiResponse(description="Only the customer can accept an offer."), 409: OpenApiResponse(description="The operation is already being processed.")})
    def post(self, request, conversation_id, offer_id):
        key, error = _require_key(request)
        if error: return error
        conversation = get_conversation_for_user(user=request.user, conversation_id=conversation_id)
        with transaction.atomic():
            record, created, cached = begin(actor=request.user, operation="conversation.offer.accept", key=key, payload={"conversation_id": str(conversation_id), "offer_id": str(offer_id)}, resource_type="Conversation", resource_id=str(conversation_id))
            if not created: return Response(cached[1], status=cached[0])
            booking, _, _ = accept_offer(conversation=conversation, user=request.user, offer_id=offer_id); hydrate_conversation(conversation, user=request.user, unread_count=True); data = ConversationDetailSerializer(conversation, context={"request": request}).data; finish(record, status_code=200, body=data, resource_type="Booking", resource_id=str(booking.id)); audit_request_action(request=request, action="OFFER_ACCEPTED", resource_type="Offer", resource_id=str(offer_id), metadata={"booking_id": str(booking.id)})
        return Response(data)

class ConversationOfferDeclineView(APIView):
    permission_classes = [IsAuthenticated, IsConversationParticipant]; throttle_classes = [ScopedRateThrottle]; throttle_scope = "conversation_offer"
    @extend_schema(tags=["Conversations"], request=None, parameters=[OpenApiParameter("Idempotency-Key", OpenApiTypes.STR, OpenApiParameter.HEADER, required=True)], responses={200: OfferSerializer, 400: OpenApiResponse(description="Offer cannot be declined."), 403: OpenApiResponse(description="Only the customer can decline an offer."), 409: OpenApiResponse(description="The operation is already being processed.")})
    def post(self, request, conversation_id, offer_id):
        key, error = _require_key(request)
        if error: return error
        conversation = get_conversation_for_user(user=request.user, conversation_id=conversation_id)
        with transaction.atomic():
            record, created, cached = begin(actor=request.user, operation="conversation.offer.decline", key=key, payload={"conversation_id": str(conversation_id), "offer_id": str(offer_id)}, resource_type="Conversation", resource_id=str(conversation_id))
            if not created: return Response(cached[1], status=cached[0])
            offer = decline_offer(conversation=conversation, user=request.user, offer_id=offer_id)
            data = OfferSerializer(offer).data
            finish(record, status_code=200, body=data, resource_type="Offer", resource_id=str(offer.id))
            audit_request_action(request=request, action="OFFER_DECLINED", resource_type="Offer", resource_id=str(offer.id), metadata={"conversation_id": str(conversation_id)})
        return Response(data)

class ConversationReadView(APIView):
    permission_classes = [IsAuthenticated, IsConversationParticipant]; throttle_classes = [ScopedRateThrottle]; throttle_scope = "conversation_read"
    @extend_schema(tags=["Conversations"], request=None, responses={200: OpenApiResponse(description="Conversation marked as read.")})
    def post(self, request, conversation_id):
        conversation = get_conversation_for_user(user=request.user, conversation_id=conversation_id); mark_conversation_read(conversation=conversation, user=request.user); return Response({"status": "read", "unread_count": 0})

class ConversationPaymentInitializeView(APIView):
    permission_classes = [IsAuthenticated, IsConversationParticipant]; throttle_classes = [ScopedRateThrottle]; throttle_scope = "payment_initialize"
    @extend_schema(tags=["Conversations"], request=None, parameters=[OpenApiParameter("Idempotency-Key", OpenApiTypes.STR, OpenApiParameter.HEADER, required=True)], responses={202: OpenApiResponse(description="Payment initialization queued."), 403: OpenApiResponse(description="Only the customer can initialize payment."), 409: OpenApiResponse(description="Payment is not ready for initialization." )})
    def post(self, request, conversation_id):
        key, error = _require_key(request)
        if error: return error
        conversation = get_conversation_for_user(user=request.user, conversation_id=conversation_id); booking = _booking_for_conversation(conversation)
        if conversation.customer_id != request.user.id: return Response({"detail": "Only the customer can initialize payment."}, status=status.HTTP_403_FORBIDDEN)
        if not booking or booking.status != Booking.Status.ACCEPTED: return Response({"detail": "Booking is not ready for payment."}, status=status.HTTP_409_CONFLICT)
        payment = EscrowPayment.objects.filter(booking=booking, addon_order__isnull=True).order_by("created_at").first()
        if payment is None: return Response({"detail": "Escrow payment is not prepared."}, status=status.HTTP_409_CONFLICT)
        if payment.status != EscrowPayment.Status.PAYMENT_PENDING: return Response({"detail": "Payment is not awaiting initialization.", "status": payment.status}, status=status.HTTP_409_CONFLICT)
        with transaction.atomic():
            record, created, cached = begin(actor=request.user, operation="conversation.payment.initialize", key=key, payload={"conversation_id": str(conversation_id), "payment_id": str(payment.id)}, resource_type="Payment", resource_id=str(payment.id))
            if not created: return Response(cached[1], status=cached[0])
            data = {"payment_id": str(payment.id), "status": "initialization_pending"}; finish(record, status_code=202, body=data, resource_type="Payment", resource_id=str(payment.id)); transaction.on_commit(lambda payment_id=str(payment.id): initialize_prepared_payment.delay(payment_id)); audit_request_action(request=request, action="PAYMENT_INITIALIZATION_QUEUED", resource_type="Payment", resource_id=str(payment.id), metadata={"booking_id": str(booking.id)})
        return Response(data, status=status.HTTP_202_ACCEPTED)

class ConversationDisputeView(APIView):
    permission_classes = [IsAuthenticated, IsConversationParticipant]; throttle_classes = [ScopedRateThrottle]; throttle_scope = "conversation_dispute"
    @extend_schema(tags=["Conversations"], request=ConversationDisputeSerializer, parameters=[OpenApiParameter("Idempotency-Key", OpenApiTypes.STR, OpenApiParameter.HEADER, required=True)], responses={200: ConversationDetailSerializer, 400: OpenApiResponse(description="Booking is not disputable or reason is invalid."), 403: OpenApiResponse(description="Only the customer can dispute the booking.")})
    def post(self, request, conversation_id):
        key, error = _require_key(request)
        if error: return error
        conversation = get_conversation_for_user(user=request.user, conversation_id=conversation_id); serializer = ConversationDisputeSerializer(data=request.data); serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            record, created, cached = begin(actor=request.user, operation="conversation.dispute.create", key=key, payload={"conversation_id": str(conversation_id), "reason": serializer.validated_data["reason"]}, resource_type="Conversation", resource_id=str(conversation_id))
            if not created: return Response(cached[1], status=cached[0])
            booking, case = open_conversation_dispute(conversation=conversation, user=request.user, reason=serializer.validated_data["reason"].strip()); hydrate_conversation(conversation, user=request.user, unread_count=True); data = ConversationDetailSerializer(conversation, context={"request": request}).data; finish(record, status_code=200, body=data, resource_type="SupportCase", resource_id=str(case.id)); audit_request_action(request=request, action="DISPUTE_OPENED", resource_type="SupportCase", resource_id=str(case.id), metadata={"booking_id": str(booking.id)})
        return Response(data)
