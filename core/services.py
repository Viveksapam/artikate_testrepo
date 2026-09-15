from django.db import transaction
from django.utils import timezone
from .models import Asset, Employee, CheckOut


class ServiceException(Exception):
    """Base exception for service layer operations."""
    pass


class ResourceNotFoundException(ServiceException):
    """Maps to 404 Not Found."""
    pass


class ValidationException(ServiceException):
    """Maps to 400 Bad Request."""
    pass


class ConflictException(ServiceException):
    """Maps to 409 Conflict."""
    pass


def checkout_asset(*, asset_tag: str, employee_code: str, due_at) -> CheckOut:
    with transaction.atomic():
        # Rule 7 & 8: Database-level row locking with select_for_update()
        try:
            asset = Asset.objects.select_for_update().get(asset_tag=asset_tag)
        except Asset.DoesNotExist:
            raise ResourceNotFoundException(f"Asset '{asset_tag}' not found.")

        try:
            employee = Employee.objects.select_for_update().get(employee_code=employee_code)
        except Employee.DoesNotExist:
            raise ResourceNotFoundException(f"Employee '{employee_code}' not found.")

        # Rule 1: Asset must be AVAILABLE -> 409 Conflict
        if asset.status != Asset.Status.AVAILABLE:
            raise ConflictException(
                f"Asset '{asset_tag}' is not available for check-out (current status: {asset.status})."
            )

        # Rule 2: Employee must be active -> 400 Bad Request
        if not employee.is_active:
            raise ValidationException(
                f"Employee '{employee_code}' is inactive and cannot check out assets."
            )

        # Rule 3: Max 3 active check-outs per employee -> 409 Conflict
        active_checkouts_count = CheckOut.objects.filter(
            employee=employee,
            returned_at__isnull=True
        ).count()
        if active_checkouts_count >= 3:
            raise ConflictException(
                f"Employee '{employee_code}' already has {active_checkouts_count} active check-outs (limit is 3)."
            )

        # Rule 5: Atomic updates to asset state and checkout log
        asset.status = Asset.Status.CHECKED_OUT
        asset.save(update_fields=['status'])

        checkout = CheckOut.objects.create(
            asset=asset,
            employee=employee,
            due_at=due_at
        )

        return checkout


def return_asset(*, asset_tag: str, condition_note: str = "", needs_maintenance: bool = False) -> CheckOut:
    with transaction.atomic():
        try:
            asset = Asset.objects.select_for_update().get(asset_tag=asset_tag)
        except Asset.DoesNotExist:
            raise ResourceNotFoundException(f"Asset '{asset_tag}' not found.")

        checkout = CheckOut.objects.filter(
            asset=asset,
            returned_at__isnull=True
        ).select_for_update().first()

        # Rule 6: Returning an already-returned check-out -> 409 Conflict
        if not checkout:
            raise ConflictException(f"Asset '{asset_tag}' has no active check-out to return.")

        checkout.returned_at = timezone.now()
        if condition_note:
            checkout.condition_note = condition_note
        checkout.save(update_fields=['returned_at', 'condition_note'])

        asset.status = Asset.Status.MAINTENANCE if needs_maintenance else Asset.Status.AVAILABLE
        asset.save(update_fields=['status'])

        return checkout