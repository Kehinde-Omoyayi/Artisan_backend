from rest_framework import serializers


class NativeStartSerializer(serializers.Serializer):
    intent = serializers.ChoiceField(choices=("register", "login"))
    role = serializers.ChoiceField(choices=("customer", "artisan"), required=False)

    def validate(self, attrs):
        if attrs["intent"] == "register" and not attrs.get("role"):
            raise serializers.ValidationError({"role": "Choose customer or artisan first."})
        return attrs


class WebStartSerializer(serializers.Serializer):
    """Input from a server-rendered frontend starting a browser redirect."""

    intent = serializers.ChoiceField(choices=("register", "login"))
    role = serializers.ChoiceField(choices=("customer", "artisan"), required=False)
    first_name = serializers.CharField(max_length=40, required=False, allow_blank=True, trim_whitespace=True)
    last_name = serializers.CharField(max_length=40, required=False, allow_blank=True, trim_whitespace=True)
    browser_binding = serializers.CharField(min_length=32, max_length=512, write_only=True)

    def validate(self, attrs):
        if attrs["intent"] == "register" and not attrs.get("role"):
            raise serializers.ValidationError({"role": "Choose customer or artisan first."})
        return attrs


class WebCompleteSerializer(serializers.Serializer):
    state = serializers.CharField(min_length=20, max_length=512)
    authorization_code = serializers.CharField(min_length=1, max_length=4096, trim_whitespace=True)
    browser_binding = serializers.CharField(min_length=32, max_length=512, write_only=True)
    user = serializers.CharField(max_length=4096, required=False, allow_blank=True, write_only=True)


class NativeCompleteSerializer(serializers.Serializer):
    state = serializers.CharField(min_length=20, max_length=512)
    authorization_code = serializers.CharField(min_length=1, max_length=4096, trim_whitespace=True)
    first_name = serializers.CharField(max_length=40, required=False, allow_blank=True, trim_whitespace=True)
    last_name = serializers.CharField(max_length=40, required=False, allow_blank=True, trim_whitespace=True)


class CompleteProfileSerializer(serializers.Serializer):
    registration_token = serializers.CharField(min_length=20, max_length=256)
    first_name = serializers.CharField(max_length=40, required=False, allow_blank=True, trim_whitespace=True)
    last_name = serializers.CharField(max_length=40, required=False, allow_blank=True, trim_whitespace=True)
