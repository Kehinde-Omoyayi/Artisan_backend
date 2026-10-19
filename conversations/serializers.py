from datetime import datetime
from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from home.models import Locations, Service, ServiceCategory
from booking.additional_order_serializers import AdditionalOrderSerializer
from support.models import SupportCase
from support.validation import normalize_support_description

from .models import Conversation, ConversationAttachment, Message, Offer


class ConversationStartSerializer(serializers.Serializer):
    artisan_id = serializers.UUIDField()
    service_id = serializers.IntegerField(required=False, allow_null=True)


class ConversationCreateSerializer(serializers.Serializer):
    participants = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        allow_empty=False,
        min_length=1,
        max_length=2,
        help_text="One target user ID, or both participant user IDs. The authenticated user is always a participant.",
    )
    initial_message = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    conversation_type = serializers.ChoiceField(
        choices=Conversation.ConversationType.choices,
        required=False,
        default=Conversation.ConversationType.DIRECT,
    )
    artisan_id = serializers.UUIDField(required=False, allow_null=True)
    service_id = serializers.IntegerField(required=False, allow_null=True)

    def validate(self, attrs):
        kind = attrs.get("conversation_type", Conversation.ConversationType.DIRECT)
        participants = attrs.get("participants") or []
        artisan_id = attrs.get("artisan_id")
        if kind == Conversation.ConversationType.DIRECT and not participants and not artisan_id:
            raise serializers.ValidationError({"participants": "Provide the target participant(s)."})
        if kind == Conversation.ConversationType.SERVICE and not artisan_id:
            raise serializers.ValidationError({"artisan_id": "artisan_id is required for a service conversation."})
        if kind == Conversation.ConversationType.SERVICE and participants:
            raise serializers.ValidationError({"participants": "Do not provide participants for a service conversation."})
        return attrs


class ConversationRequestPatchSerializer(serializers.Serializer):
    service_id = serializers.IntegerField(required=False, allow_null=True)
    category_id = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True, max_length=10000)
    location_id = serializers.UUIDField(required=False, allow_null=True)
    country = serializers.CharField(required=False, allow_blank=True, max_length=100)
    state = serializers.CharField(required=False, allow_blank=True, max_length=100)
    area = serializers.CharField(required=False, allow_blank=True, max_length=150)
    preferred_date = serializers.DateField(required=False, allow_null=True)
    preferred_time_start = serializers.TimeField(required=False, allow_null=True)
    preferred_time_end = serializers.TimeField(required=False, allow_null=True)
    preferred_time_note = serializers.CharField(required=False, allow_blank=True, max_length=100)
    urgency = serializers.ChoiceField(choices=["LOW", "NORMAL", "HIGH", "EMERGENCY"], required=False)
    budget = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True, min_value=Decimal("0"))
    budget_unknown = serializers.BooleanField(required=False)
    additional_details = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    submit_request = serializers.BooleanField(required=False)


class MessageCreateSerializer(serializers.Serializer):
    body = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    attachments = serializers.ListField(child=serializers.FileField(), required=False, write_only=True)


class ConversationDisputeSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=1000, trim_whitespace=True)

    def validate_reason(self, value):
        try:
            return normalize_support_description(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc


class ConversationParticipantSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    business_name = serializers.CharField(allow_blank=True)
    profile_photo = serializers.CharField(allow_null=True)
    role = serializers.CharField()


class ConversationCustomerSummarySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    first_name = serializers.CharField()
    last_name = serializers.CharField()


class ConversationArtisanSummarySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    business_name = serializers.CharField(allow_blank=True)
    profile_photo = serializers.CharField(allow_null=True)


class ConversationJourneyStepSerializer(serializers.Serializer):
    step = serializers.IntegerField()
    key = serializers.CharField()
    title = serializers.CharField()
    description = serializers.CharField()
    complete = serializers.BooleanField()
    current = serializers.BooleanField()
    optional = serializers.BooleanField(required=False)


class ConversationRequestSummarySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    service = serializers.SerializerMethodField()
    category = serializers.CharField()
    location = serializers.CharField()
    preferred_date = serializers.DateField()
    preferred_time_start = serializers.TimeField(allow_null=True)
    preferred_time_end = serializers.TimeField(allow_null=True)
    preferred_time_note = serializers.CharField(allow_blank=True)
    urgency = serializers.CharField()
    budget = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)
    description = serializers.CharField()
    additional_details = serializers.CharField(allow_blank=True)

    def get_service(self, obj) -> str | None:
        service = getattr(obj, "service", None)
        return service.name if service else None


class ConversationDisputeCaseSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = SupportCase
        fields = ["id", "reference", "status", "subject", "created_at"]


class ConversationBookingSummarySerializer(serializers.Serializer):
    id = serializers.UUIDField()
    status = serializers.CharField()
    scheduled_start = serializers.DateTimeField(source="scheduled_time_start")
    scheduled_end = serializers.DateTimeField(source="scheduled_time_end")
    price_agreed = serializers.DecimalField(max_digits=12, decimal_places=2)
    artisan_completed_at = serializers.DateTimeField(allow_null=True)
    customer_completed_at = serializers.DateTimeField(allow_null=True)
    finalized_at = serializers.DateTimeField(allow_null=True)


class ConversationPaymentSummarySerializer(serializers.Serializer):
    id = serializers.UUIDField()
    status = serializers.CharField()
    service_amount = serializers.SerializerMethodField()
    customer_fee = serializers.SerializerMethodField()
    total_amount = serializers.SerializerMethodField()
    authorization_url = serializers.SerializerMethodField()
    verified_at = serializers.DateTimeField(allow_null=True)
    held_at = serializers.DateTimeField(allow_null=True)
    released_at = serializers.DateTimeField(allow_null=True)

    def get_service_amount(self, obj) -> Decimal:
        return Decimal(obj.service_amount_kobo) / Decimal("100")

    def get_customer_fee(self, obj) -> Decimal:
        return Decimal(obj.customer_fee_kobo) / Decimal("100")

    def get_total_amount(self, obj) -> Decimal:
        return Decimal(obj.total_amount_kobo) / Decimal("100")

    def get_authorization_url(self, obj) -> str | None:
        request = self.context.get("request")
        if not request or obj.customer_id != request.user.id:
            return None
        payload = obj.provider_payload if isinstance(obj.provider_payload, dict) else {}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        link = data.get("link")
        return str(link) if link else None


class OfferSerializer(serializers.ModelSerializer):
    creator_id = serializers.IntegerField(source="created_by_id", read_only=True)
    parent_offer_id = serializers.UUIDField(read_only=True, allow_null=True)
    currency = serializers.CharField(default="NGN", read_only=True)

    class Meta:
        model = Offer
        fields = ["id", "conversation", "creator_id", "parent_offer_id", "labour_amount", "materials_amount", "total_amount", "materials_included", "scope", "notes", "scheduled_start", "scheduled_end", "expires_at", "status", "currency", "created_at", "updated_at"]
        read_only_fields = ["id", "conversation", "status", "created_at", "updated_at"]


class OfferCreateSerializer(serializers.Serializer):
    labour_amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0"))
    materials_amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0"))
    total_amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    materials_included = serializers.BooleanField(required=False, default=False)
    scope = serializers.CharField(max_length=5000)
    notes = serializers.CharField(max_length=3000, required=False, allow_blank=True)
    scheduled_start = serializers.DateTimeField()
    scheduled_end = serializers.DateTimeField()
    expires_at = serializers.DateTimeField()


class CounterOfferSerializer(OfferCreateSerializer):
    parent_offer_id = serializers.UUIDField()


class AttachmentSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()

    class Meta:
        model = ConversationAttachment
        fields = ["id", "content_type", "size", "scan_status", "scan_reason", "url", "uploaded_at"]
        read_only_fields = fields

    def get_url(self, obj) -> str | None:
        if getattr(obj, "scan_status", "PENDING") != "CLEAN":
            return None
        request = self.context.get("request")
        url = obj.file.url
        return request.build_absolute_uri(url) if request else url


class MessageSerializer(serializers.ModelSerializer):
    sender_id = serializers.IntegerField(read_only=True)
    sender_name = serializers.SerializerMethodField()
    sender_role = serializers.SerializerMethodField()
    attachments = AttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = Message
        fields = ["id", "conversation", "sender_id", "sender_name", "sender_role", "message_type", "body", "event_code", "metadata", "attachments", "created_at"]
        read_only_fields = fields

    def get_sender_name(self, obj) -> str | None:
        if not obj.sender:
            return None
        return f"{obj.sender.first_name} {obj.sender.last_name}".strip()

    def get_sender_role(self, obj) -> str:
        if not obj.sender:
            return "SYSTEM"
        conversation = obj.conversation
        if conversation.conversation_type == Conversation.ConversationType.SERVICE:
            if conversation.customer_id == obj.sender_id:
                return "customer"
            if conversation.artisan_id and conversation.artisan.user_id == obj.sender_id:
                return "artisan"
        return "artisan" if hasattr(obj.sender, "artisan_profile") else "customer"


class ConversationMessageCreateResponseSerializer(MessageSerializer):
    assistant = serializers.JSONField(read_only=True)

    class Meta(MessageSerializer.Meta):
        fields = MessageSerializer.Meta.fields + ["assistant"]
        read_only_fields = fields


class ConversationListSerializer(serializers.ModelSerializer):
    state = serializers.SerializerMethodField()
    viewer_role = serializers.SerializerMethodField()
    unread = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()
    last_message_at = serializers.SerializerMethodField()
    last_message_preview = serializers.SerializerMethodField()
    last_message_text = serializers.SerializerMethodField()
    conversation_type = serializers.CharField(read_only=True)
    title = serializers.SerializerMethodField()
    counterparty = serializers.SerializerMethodField()
    participants = serializers.SerializerMethodField()
    artisan_name = serializers.SerializerMethodField()
    artisan_business_name = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = ["id", "conversation_type", "viewer_role", "title", "counterparty", "participants", "artisan_name", "artisan_business_name", "state", "unread", "unread_count", "last_message_at", "last_message_preview", "last_message_text", "created_at", "updated_at"]
        read_only_fields = fields

    def _viewer(self):
        request = self.context.get("request")
        return getattr(request, "user", None) if request else None

    def _participant_objects(self, obj):
        cache = getattr(obj, "_participant_cache", None)
        if cache is not None:
            return cache
        cache = list(obj.participants.all())
        if not cache:
            legacy = []
            if obj.customer_id and obj.customer:
                legacy.append(obj.customer)
            if obj.artisan_id and obj.artisan and obj.artisan.user:
                legacy.append(obj.artisan.user)
            cache = legacy
        obj._participant_cache = cache
        return cache

    def _counterparty(self, obj):
        viewer = self._viewer()
        people = self._participant_objects(obj)
        for person in people:
            if not viewer or person.id != viewer.id:
                return person
        return None

    def _role_for_user(self, obj, user):
        if user is None:
            return None
        if obj.conversation_type == Conversation.ConversationType.SERVICE:
            if obj.customer_id == user.id:
                return "customer"
            if obj.artisan_id and obj.artisan.user_id == user.id:
                return "artisan"
        return "artisan" if hasattr(user, "artisan_profile") else "customer"

    def _person_payload(self, user, obj=None):
        if user is None:
            return None
        role = self._role_for_user(obj, user) if obj is not None else ("artisan" if hasattr(user, "artisan_profile") else "customer")
        artisan = getattr(user, "artisan_profile", None) if role == "artisan" else None
        customer = getattr(user, "customer_profile", None) if role == "customer" else None
        profile = artisan or customer
        profile_photo = None
        if profile and profile.profile_photo:
            try:
                profile_photo = profile.profile_photo.url
                request = self.context.get("request")
                if request:
                    profile_photo = request.build_absolute_uri(profile_photo)
            except (ValueError, OSError, NotImplementedError):
                profile_photo = None
        return {
            "id": user.id,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "business_name": getattr(artisan, "business_name", "") if artisan else "",
            "service_label": self._service_label(user, obj) if role == "artisan" else "",
            "profile_photo": profile_photo,
            "role": role,
        }

    def _service_label(self, user, obj=None):
        """First active service title so the inbox can show "Trade · Business" like a chat list.

        The inbox view resolves these in one batched query and passes them through the
        serializer context; other endpoints simply omit the label instead of paying an
        extra query per row.
        """
        labels = self.context.get("service_labels")
        if isinstance(labels, dict):
            return labels.get(user.id, "")
        return ""

    def get_viewer_role(self, obj) -> str | None:
        request = self.context.get("request")
        if not request:
            return None
        if obj.conversation_type == Conversation.ConversationType.DIRECT:
            return "direct" if any(person.id == request.user.id for person in self._participant_objects(obj)) else None
        return self._role_for_user(obj, request.user)

    def get_state(self, obj) -> str: return getattr(obj, "_conversation_state", "OPEN")
    def get_unread(self, obj) -> bool: return bool(getattr(obj, "_unread", False))
    def get_unread_count(self, obj) -> int: return int(getattr(obj, "_unread_count", 0))
    def get_last_message_at(self, obj) -> datetime | None: return getattr(obj, "_last_message_at", None)

    def get_last_message_text(self, obj) -> str:
        """Raw latest message body, without the sender prefix used by last_message_preview."""
        return " ".join(str(getattr(obj, "last_message_body", "") or "").split())

    def get_last_message_preview(self, obj) -> str:
        body = " ".join(str(getattr(obj, "last_message_body", "") or "").split())
        if not body:
            return "Start the service request" if obj.conversation_type == Conversation.ConversationType.SERVICE else "Start a private conversation"
        if len(body) > 100:
            body = f"{body[:97].rstrip()}…"
        message_type = getattr(obj, "last_message_type", None)
        sender_id = getattr(obj, "last_message_sender_id", None)
        if message_type == Message.MessageType.SYSTEM:
            sender = "Artisan Market"
        elif sender_id == getattr(self._viewer(), "id", None):
            sender = "You"
        else:
            counterparty = self._counterparty(obj)
            role = self._role_for_user(obj, counterparty) if counterparty else None
            artisan = getattr(counterparty, "artisan_profile", None) if counterparty and role == "artisan" else None
            sender = getattr(artisan, "business_name", "") or (f"{counterparty.first_name} {counterparty.last_name}".strip() if counterparty else "Participant")
        return f"{sender}: {body}"

    def get_title(self, obj) -> str:
        counterparty = self._counterparty(obj)
        if counterparty is None:
            return "Conversation"
        role = self._role_for_user(obj, counterparty)
        artisan = getattr(counterparty, "artisan_profile", None) if role == "artisan" else None
        return getattr(artisan, "business_name", "") or f"{counterparty.first_name} {counterparty.last_name}".strip() or "Conversation"

    @extend_schema_field(ConversationParticipantSerializer(allow_null=True))
    def get_counterparty(self, obj):
        return self._person_payload(self._counterparty(obj), obj=obj)

    @extend_schema_field(ConversationParticipantSerializer(many=True))
    def get_participants(self, obj):
        return [self._person_payload(person, obj=obj) for person in self._participant_objects(obj)]

    def get_artisan_name(self, obj) -> str:
        if obj.artisan_id and obj.artisan and obj.artisan.user:
            return f"{obj.artisan.user.first_name} {obj.artisan.user.last_name}".strip()
        return ""

    def get_artisan_business_name(self, obj) -> str:
        return obj.artisan.business_name if obj.artisan_id and obj.artisan else ""


class ConversationDetailSerializer(ConversationListSerializer):
    viewer_role = serializers.SerializerMethodField()
    viewer_id = serializers.SerializerMethodField()
    assistant = serializers.SerializerMethodField()
    customer = serializers.SerializerMethodField()
    artisan = serializers.SerializerMethodField()
    service_request = ConversationRequestSummarySerializer(read_only=True, allow_null=True)
    request_draft = serializers.SerializerMethodField()
    booking = serializers.SerializerMethodField()
    payment = serializers.SerializerMethodField()
    additional_orders = serializers.SerializerMethodField()
    active_offer = serializers.SerializerMethodField()
    available_actions = serializers.SerializerMethodField()
    next_question = serializers.SerializerMethodField()
    journey = serializers.SerializerMethodField()
    dispute_case = serializers.SerializerMethodField()

    def get_viewer_role(self, obj) -> str | None:
        request = self.context.get("request")
        if not request:
            return None
        if obj.conversation_type == Conversation.ConversationType.DIRECT:
            return "direct" if obj.participants.filter(pk=request.user.id).exists() else None
        return self._role_for_user(obj, request.user)

    def get_viewer_id(self, obj) -> int | None:
        request = self.context.get("request")
        return request.user.id if request else None

    @extend_schema_field(serializers.JSONField(allow_null=True))
    def get_assistant(self, obj):
        from .automation import assistant_context
        request = self.context.get("request")
        if not request:
            return None
        return assistant_context(conversation=obj, user=request.user, booking=getattr(obj, "_booking", None), offer=getattr(obj, "_active_offer", None), payment=getattr(getattr(obj, "_booking", None), "escrow_payment", None))

    @extend_schema_field(ConversationCustomerSummarySerializer(allow_null=True))
    def get_customer(self, obj):
        if not obj.customer_id or not obj.customer:
            return None
        return {"id": obj.customer_id, "first_name": obj.customer.first_name, "last_name": obj.customer.last_name}

    @extend_schema_field(ConversationArtisanSummarySerializer(allow_null=True))
    def get_artisan(self, obj):
        if not obj.artisan_id or not obj.artisan or not obj.artisan.user:
            return None
        profile_photo = None
        if obj.artisan.profile_photo:
            try:
                profile_photo = obj.artisan.profile_photo.url
                request = self.context.get("request")
                if request:
                    profile_photo = request.build_absolute_uri(profile_photo)
            except (ValueError, OSError, NotImplementedError):
                profile_photo = None
        return {"id": obj.artisan.user_id, "first_name": obj.artisan.user.first_name, "last_name": obj.artisan.user.last_name, "business_name": obj.artisan.business_name, "profile_photo": profile_photo}

    @extend_schema_field(serializers.JSONField(allow_null=True))
    def get_request_draft(self, obj):
        draft = obj.draft if isinstance(obj.draft, dict) else {}
        if not draft:
            return None
        summary = dict(draft)
        service_id = summary.get("service_id")
        if service_id and str(service_id).isdigit():
            service = Service.objects.filter(pk=int(service_id)).select_related("category").first()
            if service:
                summary["service_name"] = service.name
                summary["category_id"] = str(service.category_id)
                summary["category_name"] = service.category.name
        category_id = summary.get("category_id")
        if category_id:
            category_name = ServiceCategory.objects.filter(pk=category_id).values_list("name", flat=True).first()
            if category_name:
                summary["category_name"] = category_name
        location_id = summary.get("location_id")
        if location_id:
            location = Locations.objects.filter(pk=location_id).values("country", "state", "area").first()
            if location:
                summary.update({"country": location["country"], "state": location["state"], "area": location["area"]})
                summary["location_label"] = f"{location['area']}, {location['state']}"
        return summary

    @extend_schema_field(ConversationBookingSummarySerializer(allow_null=True))
    def get_booking(self, obj):
        booking = getattr(obj, "_booking", None)
        return ConversationBookingSummarySerializer(booking).data if booking else None

    @extend_schema_field(ConversationPaymentSummarySerializer(allow_null=True))
    def get_payment(self, obj):
        booking = getattr(obj, "_booking", None)
        payment = getattr(booking, "escrow_payment", None) if booking else None
        return ConversationPaymentSummarySerializer(payment, context=self.context).data if payment else None

    @extend_schema_field(AdditionalOrderSerializer(many=True))
    def get_additional_orders(self, obj):
        booking = getattr(obj, "_booking", None)
        if not booking:
            return []
        orders = booking.additional_orders.select_related("booking__customer__user").prefetch_related("offers", "payment").all()
        return AdditionalOrderSerializer(orders, many=True, context=self.context).data

    @extend_schema_field(ConversationDisputeCaseSummarySerializer(allow_null=True))
    def get_dispute_case(self, obj):
        booking = getattr(obj, "_booking", None)
        if not booking:
            return None
        case = SupportCase.objects.filter(
            booking_id=booking.id,
            case_type=SupportCase.CaseType.DISPUTE,
        ).only("id", "reference", "status", "subject", "created_at").order_by("-created_at").first()
        return ConversationDisputeCaseSummarySerializer(case).data if case else None

    @extend_schema_field(OfferSerializer(allow_null=True))
    def get_active_offer(self, obj):
        offer = getattr(obj, "_active_offer", None)
        return OfferSerializer(offer, context=self.context).data if offer else None

    def get_available_actions(self, obj) -> list[str]: return getattr(obj, "_available_actions", [])

    def get_next_question(self, obj) -> str | None:
        request = self.context.get("request")
        if request and obj.customer_id != request.user.id:
            return None
        return getattr(obj, "_next_question", None)


    @extend_schema_field(ConversationJourneyStepSerializer(many=True))
    def get_journey(self, obj):
        state = getattr(obj, "_conversation_state", "OPEN")
        booking = getattr(obj, "_booking", None)
        offer = getattr(obj, "_active_offer", None)
        payment = getattr(booking, "escrow_payment", None) if booking else None
        booking_status = getattr(booking, "status", None)
        has_booking = booking is not None
        request_complete = bool(has_booking or offer)
        offer_complete = has_booking
        payment_held = bool(payment and payment.status in {"HELD", "RELEASE_PENDING", "RELEASED", "DISPUTED"})
        progress_complete = booking_status in {"COMPLETION_REQUESTED", "COMPLETED", "DISPUTED"}

        addon_orders = list(booking.additional_orders.select_related("payment").all()) if booking else []
        open_addon_count = 0
        unpaid_approved_count = 0
        for order in addon_orders:
            if order.status == "NEGOTIATING":
                open_addon_count += 1
            elif order.status == "APPROVED":
                try:
                    addon_payment = order.payment
                except ObjectDoesNotExist:
                    addon_payment = None
                if not addon_payment or addon_payment.status != "HELD":
                    unpaid_approved_count += 1
        addon_pending = bool(open_addon_count or unpaid_approved_count)
        addon_complete = not addon_pending
        addon_count = len(addon_orders)
        if addon_pending:
            addon_description = f"{open_addon_count} open negotiation(s) and {unpaid_approved_count} approved order(s) awaiting confirmed escrow payment."
        elif addon_count:
            addon_description = f"{addon_count} separate add-on order(s) resolved; approved work is funded in escrow."
        else:
            addon_description = "Optional — no separate add-on orders."

        if not has_booking:
            request_current = not request_complete
            offer_current = request_complete and bool(offer)
            payment_current = False
            progress_current = False
            addon_current = False
            completion_current = False
            dispute_current = False
        else:
            request_current = False
            offer_current = False
            payment_current = booking_status in {"REQUESTED", "ACCEPTED"} and not payment_held
            progress_current = booking_status == "IN_PROGRESS" and not addon_pending
            addon_current = booking_status == "IN_PROGRESS" and addon_pending
            completion_current = booking_status == "COMPLETION_REQUESTED"
            dispute_current = booking_status == "DISPUTED"

        return [
            {"step": 1, "key": "REQUEST", "title": "Request & Chat", "description": "Describe the work and agree the request details.", "complete": request_complete, "current": request_current},
            {"step": 2, "key": "OFFER", "title": "Offer & Negotiation", "description": "Review scope, price, and the agreed schedule.", "complete": offer_complete, "current": offer_current},
            {"step": 3, "key": "PAY", "title": "Booking & Escrow", "description": "The original booking payment is provider-verified and held in escrow.", "complete": payment_held, "current": payment_current},
            {"step": 4, "key": "PROGRESS", "title": "Service in Progress", "description": "Share updates and photos in this conversation.", "complete": progress_complete, "current": progress_current},
            {"step": 5, "key": "ADD_ONS", "title": "Separate add-on orders", "description": addon_description, "complete": addon_complete, "current": addon_current, "optional": True},
            {"step": 6, "key": "COMPLETE", "title": "Completion & Review", "description": "The customer confirms completion after approved add-ons are funded.", "complete": booking_status == "COMPLETED", "current": completion_current},
            {"step": 7, "key": "DISPUTE", "title": "Support (if needed)", "description": "Dispute handling keeps relevant escrow funds frozen.", "complete": state == "DISPUTED", "current": dispute_current, "optional": True},
        ]

    class Meta(ConversationListSerializer.Meta):
        fields = ConversationListSerializer.Meta.fields + ["viewer_role", "viewer_id", "assistant", "customer", "artisan", "service_request", "request_draft", "booking", "payment", "additional_orders", "active_offer", "available_actions", "next_question", "journey", "dispute_case"]