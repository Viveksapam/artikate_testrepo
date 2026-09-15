from django.urls import path
from .views import (
    AssetListCreateView,
    AssetDetailView,
    AssetCheckOutView,
    AssetReturnView,
    health_check,
    employee_summary,
    overdue_report,
)

urlpatterns = [
    path('health/', health_check, name='health-check'),
    path('assets/', AssetListCreateView.as_view(), name='asset-list-create'),
    path('assets/<int:pk>/', AssetDetailView.as_view(), name='asset-detail'),
    path('checkouts/', AssetCheckOutView.as_view(), name='asset-checkout'),
    path('checkouts/<int:pk>/return/', AssetReturnView.as_view(), name='asset-return'),
    path('employees/<str:employee_code>/summary/', employee_summary, name='employee-summary'),
    path('reports/overdue/', overdue_report, name='overdue-report'),
]