from django.urls import path
from .views import AssetListView, AssetCheckOutView, AssetReturnView

urlpatterns = [
    path('assets/', AssetListView.as_view(), name='asset-list'),
    path('assets/checkout/', AssetCheckOutView.as_view(), name='asset-checkout'),
    path('assets/return/<str:asset_tag>/', AssetReturnView.as_view(), name='asset-return'),
]