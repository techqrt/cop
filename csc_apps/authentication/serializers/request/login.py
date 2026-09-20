from rest_framework import serializers

from csc_apps.authentication.dataclasses.request.login import LoginRequest


class LoginRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(max_length=128, trim_whitespace=False)

    def create(self, validated_data) -> LoginRequest:
        return LoginRequest(**validated_data)
