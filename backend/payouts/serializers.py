from rest_framework import serializers
from .models import Merchant, Payout, LedgerEntry


class LedgerEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = LedgerEntry
        fields = ['id', 'amount_paise', 'entry_type', 'description', 'payout_id', 'created_at']


class PayoutSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payout
        fields = [
            'id', 'merchant_id', 'amount_paise', 'bank_account_id',
            'status', 'attempt_count', 'failure_reason',
            'processing_started_at', 'completed_at', 'created_at', 'updated_at'
        ]


class PayoutRequestSerializer(serializers.Serializer):
    amount_paise = serializers.IntegerField(min_value=1)
    bank_account_id = serializers.CharField(max_length=64)


class MerchantDashboardSerializer(serializers.ModelSerializer):
    available_balance_paise = serializers.SerializerMethodField()
    held_balance_paise = serializers.SerializerMethodField()

    class Meta:
        model = Merchant
        fields = ['id', 'name', 'email', 'bank_account_id',
                  'available_balance_paise', 'held_balance_paise']

    def get_available_balance_paise(self, obj):
        available, _ = obj.get_balance()
        return available

    def get_held_balance_paise(self, obj):
        _, held = obj.get_balance()
        return held
