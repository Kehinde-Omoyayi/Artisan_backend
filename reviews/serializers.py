from django.db import IntegrityError, transaction
from rest_framework import serializers

from booking.models import Booking
from notifications.events import notify_review_received
from conversations.events import record_booking_event
from core.services import create_audit_log

from .models import Review

class ReviewSerializer(serializers.ModelSerializer):
    booking = serializers.PrimaryKeyRelatedField(queryset=Booking.objects.all())
    customer = serializers.PrimaryKeyRelatedField(read_only=True)
    artisan = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Review
        fields = ["id", "booking", "customer", "artisan", "rating", "comment"]
        read_only_fields = ["id", "customer", "artisan"]

    def validate(self, attrs):
        request = self.context["request"]
        booking = attrs["booking"]

        if booking.customer.user_id != request.user.id:
            raise serializers.ValidationError({"booking": "You can only review your own booking."})
        if booking.status != Booking.Status.COMPLETED:
            raise serializers.ValidationError({"booking": "A review can only be submitted for a completed booking."})
        comment = attrs.get("comment", "")
        if len(comment) > 5000:
            raise serializers.ValidationError({"comment": "Comment must be 5,000 characters or fewer."})
        if hasattr(booking, "review"):
            raise serializers.ValidationError({"booking": "This booking has already been reviewed."})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        request = self.context["request"]
        requested_booking = validated_data["booking"]
        booking = Booking.objects.select_for_update(of=("self",)).select_related("artisan__user", "customer").get(pk=requested_booking.pk)

        if booking.customer.user_id != request.user.id:
            raise serializers.ValidationError({"booking": "You can only review your own booking."})
        if booking.status != Booking.Status.COMPLETED:
            raise serializers.ValidationError({"booking": "A review can only be submitted for a completed booking."})
        if Review.objects.filter(booking=booking).exists():
            raise serializers.ValidationError({"booking": "This booking has already been reviewed."})

        try:
            with transaction.atomic():
                review = Review.objects.create(
                    booking=booking,
                    customer=request.user,
                    artisan=booking.artisan,
                    rating=validated_data["rating"],
                    comment=validated_data.get("comment", ""),
                )
        except IntegrityError as exc:
            raise serializers.ValidationError({"booking": "This booking has already been reviewed."}) from exc

        create_audit_log(action="REVIEW_SUBMITTED", resource_type="Review", resource_id=str(review.id), actor=request.user, metadata={"booking_id": str(booking.id), "rating": review.rating})
        transaction.on_commit(
            lambda: record_booking_event(booking=booking, event_code="REVIEW_SUBMITTED", event_key=f"review:{review.id}:submitted", metadata={"booking_id": str(booking.id), "review_id": review.id, "rating": review.rating})
        )
        transaction.on_commit(
            lambda: notify_review_received(
                artisan_user=booking.artisan.user,
                booking=booking,
                rating=review.rating,
            )
        )
        return review
