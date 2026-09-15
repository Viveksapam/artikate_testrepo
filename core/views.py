from django.db import connection
from django.db.models import Q, Count, Avg, ExpressionWrapper, fields, F
from django.utils import timezone
from rest_framework import status, generics, filters
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.decorators import api_view, permission_classes
from django_filters.rest_framework import DjangoFilterBackend

from .models import Asset, Employee, CheckOut
from .serializers import (
    AssetSerializer,
    CheckOutCreateSerializer,
    CheckOutReturnSerializer,
)
from .services import (
    checkout_asset,
    return_asset,
    ResourceNotFoundException,
    ValidationException,
    ConflictException,
)


class AssetListCreateView(generics.ListCreateAPIView):
    queryset = Asset.objects.all().order_by('-created_at')
    serializer_class = AssetSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['status', 'category']
    search_fields = ['name', 'asset_tag']


class AssetDetailView(generics.RetrieveAPIView):
    queryset = Asset.objects.all()
    serializer_class = AssetSerializer
    permission_classes = [IsAuthenticated]


@api_view(['GET'])
@permission_classes([AllowAny])
def health_check(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return Response({"status": "ok", "database": "connected"}, status=status.HTTP_200_OK)
    except Exception as e:
        return Response({"status": "error", "database": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AssetCheckOutView(generics.GenericAPIView):
    serializer_class = CheckOutCreateSerializer
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        try:
            checkout = checkout_asset(
                asset_tag=serializer.validated_data['asset_tag'],
                employee_code=serializer.validated_data['employee_code'],
                due_at=serializer.validated_data['due_at']
            )
            return Response(
                {
                    "message": "Asset checked out successfully.",
                    "checkout_id": checkout.id,
                    "asset_tag": checkout.asset.asset_tag,
                    "due_at": checkout.due_at,
                },
                status=status.HTTP_201_CREATED
            )
        except ResourceNotFoundException as e:
            return Response({"error": str(e)}, status=status.HTTP_404_NOT_FOUND)
        except ValidationException as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except ConflictException as e:
            return Response({"error": str(e)}, status=status.HTTP_409_CONFLICT)


class AssetReturnView(generics.GenericAPIView):
    serializer_class = CheckOutReturnSerializer
    permission_classes = [IsAuthenticated]

    def post(self, request, pk, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        try:
            checkout_obj = CheckOut.objects.get(pk=pk)
        except CheckOut.DoesNotExist:
            return Response({"error": f"Check-out with ID '{pk}' not found."}, status=status.HTTP_404_NOT_FOUND)

        if checkout_obj.returned_at is not None:
            return Response({"error": f"Check-out with ID '{pk}' has already been returned."}, status=status.HTTP_409_CONFLICT)

        asset_tag = checkout_obj.asset.asset_tag
        
        try:
            checkout = return_asset(
                asset_tag=asset_tag,
                condition_note=serializer.validated_data.get('condition_note', ''),
                needs_maintenance=serializer.validated_data.get('needs_maintenance', False)
            )
            return Response(
                {
                    "message": "Asset returned successfully.",
                    "asset_tag": checkout.asset.asset_tag,
                    "status": checkout.asset.status,
                    "returned_at": checkout.returned_at,
                },
                status=status.HTTP_200_OK
            )
        except ResourceNotFoundException as e:
            return Response({"error": str(e)}, status=status.HTTP_404_NOT_FOUND)
        except ValidationException as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except ConflictException as e:
            return Response({"error": str(e)}, status=status.HTTP_409_CONFLICT)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def employee_summary(request, employee_code):
    try:
        employee = Employee.objects.get(employee_code=employee_code)
    except Employee.DoesNotExist:
        return Response({"error": f"Employee '{employee_code}' not found."}, status=status.HTTP_404_NOT_FOUND)

    now = timezone.now()
    hold_duration_expr = ExpressionWrapper(
        F('returned_at') - F('checked_out_at'),
        output_field=fields.DurationField()
    )

    stats = employee.checkouts.aggregate(
        lifetime_count=Count('id'),
        currently_held=Count('id', filter=Q(returned_at__isnull=True)),
        currently_overdue=Count('id', filter=Q(returned_at__isnull=True, due_at__lt=now)),
        mean_hold_duration=Avg(hold_duration_expr, filter=Q(returned_at__isnull=False))
    )

    mean_days = None
    if stats['mean_hold_duration'] is not None:
        mean_days = round(stats['mean_hold_duration'].total_seconds() / 86400.0, 2)

    return Response({
        "employee_code": employee.employee_code,
        "lifetime_checkouts": stats['lifetime_count'],
        "currently_held": stats['currently_held'],
        "currently_overdue": stats['currently_overdue'],
        "mean_hold_duration_days": mean_days
    }, status=status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def overdue_report(request):
    now = timezone.now()
    overdue_checkouts = CheckOut.objects.filter(
        returned_at__isnull=True,
        due_at__lt=now
    ).select_related('asset', 'employee').order_by('due_at')

    report_data = []
    for c in overdue_checkouts:
        days_overdue = (now - c.due_at).days
        report_data.append({
            "checkout_id": c.id,
            "asset_name": c.asset.name,
            "asset_tag": c.asset.asset_tag,
            "employee_code": c.employee.employee_code,
            "employee_name": c.employee.full_name,
            "due_at": c.due_at,
            "days_overdue": days_overdue
        })

    return Response(report_data, status=status.HTTP_200_OK)