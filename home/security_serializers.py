from rest_framework import serializers

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError

from .security_questions import SECURITY_QUESTION_OPTIONS, normalize_security_answer


class SecurityQuestionAnswerSerializer(serializers.Serializer):
    question_id = serializers.ChoiceField(choices=SECURITY_QUESTION_OPTIONS)
    answer = serializers.CharField(min_length=3, max_length=160, trim_whitespace=True, write_only=True)

    def validate_answer(self, value):
        if len(normalize_security_answer(value)) < 3:
            raise serializers.ValidationError("Enter a meaningful answer of at least three letters or numbers.")
        return value


class ThreeSecurityQuestionAnswersSerializer(serializers.Serializer):
    questions = SecurityQuestionAnswerSerializer(many=True, min_length=3, max_length=3, write_only=True)

    def validate_questions(self, value):
        question_ids = [item["question_id"] for item in value]
        if len(set(question_ids)) != 3:
            raise serializers.ValidationError("Choose three different security questions.")
        return value


class StaffSecurityQuestionsSerializer(ThreeSecurityQuestionAnswersSerializer):
    current_password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=256)
    current_answers = SecurityQuestionAnswerSerializer(many=True, min_length=3, max_length=3, required=False, write_only=True)

    def validate_current_answers(self, value):
        question_ids = [item["question_id"] for item in value]
        if len(set(question_ids)) != 3:
            raise serializers.ValidationError("Enter all three different registered questions.")
        return value


class StaffPasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=256)
    new_password = serializers.CharField(write_only=True, min_length=8, max_length=256)
    confirm_password = serializers.CharField(write_only=True, min_length=8, max_length=256)

    def validate(self, attrs):
        if attrs["new_password"] != attrs["confirm_password"]:
            raise serializers.ValidationError({"confirm_password": "Passwords do not match."})  # nosec B105: validation message, not a credential
        if attrs["new_password"] == attrs["current_password"]:
            raise serializers.ValidationError({"new_password": "Choose a password different from your current password."})  # nosec B105: validation message, not a credential
        user = self.context.get("user")
        if user is not None:
            try:
                validate_password(attrs["new_password"], user=user)
            except DjangoValidationError as exc:
                raise serializers.ValidationError({"new_password": exc.messages}) from None
        return attrs


class StaffRecoveryIdentitySerializer(ThreeSecurityQuestionAnswersSerializer):
    email = serializers.EmailField(write_only=True)
    otp = serializers.RegexField(r"^[0-9]{6}$", write_only=True)


class StaffSecuritySetupCompleteSerializer(StaffRecoveryIdentitySerializer):
    pass


class StaffPasswordResetVerifySerializer(StaffRecoveryIdentitySerializer):
    pass


class StaffPasswordResetCompleteSerializer(serializers.Serializer):
    reset_token = serializers.CharField(write_only=True, min_length=40, max_length=128)
    new_password = serializers.CharField(write_only=True, min_length=8, max_length=256)
    confirm_password = serializers.CharField(write_only=True, min_length=8, max_length=256)

    def validate(self, attrs):
        if attrs["new_password"] != attrs["confirm_password"]:
            raise serializers.ValidationError({"confirm_password": "Passwords do not match."})  # nosec B105: validation message, not a credential
        return attrs
