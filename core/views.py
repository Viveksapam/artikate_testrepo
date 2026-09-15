from django.shortcuts import render
from rest_framework import status, generics
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from .models import Asset, CheckOut
from .serializers import (
    AssetSerializer,
    CheckOutCreateSerializer,
    CheckOutReturnSerializer,
)
from .services import (
    checkout_asset,
    return_asset,
    ResourceNotFoundException,
    BusinessRuleViolationException,
)


class AssetListView(generics.ListAPIView):
    queryset = Asset.objects.all().order_by('-created_at')
    serializer_class = AssetSerializer
    permission_classes = [IsAuthenticated]


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
        except BusinessRuleViolationException as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class AssetReturnView(generics.GenericAPIView):
    serializer_class = CheckOutReturnSerializer
    permission_classes = [IsAuthenticated]

    def post(self, request, asset_tag, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
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
        except BusinessRuleViolationException as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)