from rest_framework import serializers


class LoginUserSerializer(serializers.Serializer):
    userId = serializers.IntegerField(read_only=True)
    email = serializers.EmailField(read_only=True)
    name = serializers.CharField(read_only=True)
    role = serializers.CharField(read_only=True)


class LoginDataSerializer(serializers.Serializer):
    token = serializers.CharField(read_only=True)
    user = LoginUserSerializer(read_only=True)


class LoginResponseSerializer(serializers.Serializer):
    """Validates the outer {status, message, data} envelope's `data` key
    (docs/pms-reference-analysis.md §6) - `status`/`message` are present in the
    actual response but intentionally left undeclared here, same as PMS's response
    serializers."""

    data = LoginDataSerializer(read_only=True)
