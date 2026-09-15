from rest_framework import serializers
from django.utils import timezone
from datetime import timedelta
from .models import Asset, Employee, CheckOut, OverdueNotice


class AssetSerializer(serializers.ModelSerializer):
    current_holder = serializers.SerializerMethodField()

    class Meta:
        model = Asset
        fields = ['id', 'asset_tag', 'name', 'category', 'status', 'current_holder', 'created_at']
        read_only_fields = ['status', 'current_holder', 'created_at']

    def get_current_holder(self, obj):
        if obj.status == Asset.Status.AVAILABLE:
            return None
        active_checkout = obj.checkouts.filter(returned_at__isnull=True).first()
        if active_checkout and active_checkout.employee:
            return {
                'employee_code': active_checkout.employee.employee_code,
                'name': active_checkout.employee.name
            }
        return None


class CheckOutCreateSerializer(serializers.Serializer):
    asset_tag = serializers.CharField(max_length=100)
    employee_code = serializers.CharField(max_length=100)
    due_at = serializers.DateTimeField()

    def validate_due_at(self, value):
        now = timezone.now()
        max_due = now + timedelta(days=30)
        
        # Rule 4: due_at must be in the future and no more than 30 days ahead
        if value <= now or value > max_due:
            raise serializers.ValidationError(
                "due_at must be in the future and no more than 30 days ahead of now."
            )
        return value


class CheckOutReturnSerializer(serializers.Serializer):
    condition_note = serializers.CharField(required=False, allow_blank=True)
    needs_maintenance = serializers.BooleanField(default=False)