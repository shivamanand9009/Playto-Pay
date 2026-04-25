from django.urls import path
from . import views

urlpatterns = [
    # Merchant
    path('merchants/', views.list_merchants),
    path('merchants/<uuid:merchant_id>/', views.merchant_dashboard),

    # Payouts
    path('merchants/<uuid:merchant_id>/payouts/', views.request_payout),
    path('merchants/<uuid:merchant_id>/payouts/list/', views.list_payouts),
    path('merchants/<uuid:merchant_id>/payouts/<uuid:payout_id>/', views.payout_detail),

    # Audit
    path('merchants/<uuid:merchant_id>/invariant/', views.balance_invariant_check),
]
