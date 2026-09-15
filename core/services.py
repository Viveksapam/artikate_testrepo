from django.db import transaction
from django.utils import timezone
from .models import Asset, Employee, CheckOut


class ServiceException(Exception):
    """Base exception for service layer operations."""
    pass


class ResourceNotFoundException(ServiceException):
    """Maps to 404 Not Found."""
    pass


class BusinessRuleViolationException(ServiceException):
    """Maps to 400 Bad Request or 409 Conflict."""
    pass


def checkout_asset(*, asset_tag: str, employee_code: str, due_at) -> CheckOut:
    """
    Processes asset check-out with row-level locking to prevent race conditions.
    Enforces Rules 1, 2, 3, 5, 7, and 8.
    """
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

        # Rule 1: Asset must be AVAILABLE
        if asset.status != Asset.Status.AVAILABLE:
            raise BusinessRuleViolationException(
                f"Asset '{asset_tag}' is not available for check-out (current status: {asset.status})."
            )

        # Rule 2: Employee must be active
        if not employee.is_active:
            raise BusinessRuleViolationException(
                f"Employee '{employee_code}' is inactive and cannot check out assets."
            )

        # Rule 3: Max 3 active check-outs per employee
        active_checkouts_count = CheckOut.objects.filter(
            employee=employee,
            returned_at__isnull=True
        ).count()
        if active_checkouts_count >= 3:
            raise BusinessRuleViolationException(
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
    """
    Processes asset check-in, clearing active status and applying maintenance flags.
    Enforces Rules 5, 6, 7, and 8.
    """
    with transaction.atomic():
        # Rule 7 & 8: Row-level lock on the target asset
        try:
            asset = Asset.objects.select_for_update().get(asset_tag=asset_tag)
        except Asset.DoesNotExist:
            raise ResourceNotFoundException(f"Asset '{asset_tag}' not found.")

        # Locate the active check-out record
        checkout = CheckOut.objects.filter(
            asset=asset,
            returned_at__isnull=True
        ).select_for_update().first()

        if not checkout:
            raise BusinessRuleViolationException(f"Asset '{asset_tag}' has no active check-out to return.")

        # Rule 6: Record return timestamp and condition
        checkout.returned_at = timezone.now()
        if condition_note:
            checkout.condition_note = condition_note
        checkout.save(update_fields=['returned_at', 'condition_note'])

        # Update asset status based on maintenance requirement
        asset.status = Asset.Status.MAINTENANCE if needs_maintenance else Asset.Status.AVAILABLE
        asset.save(update_fields=['status'])

        return checkout