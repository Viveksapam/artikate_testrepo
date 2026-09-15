from django.db import models

# Create your models here.

class Asset(models.Model):
    class Category(models.TextChoices):
        CAMERA = 'CAMERA', 'Camera'
        LAPTOP = 'LAPTOP', 'Laptop'
        SENSOR = 'SENSOR', 'Sensor'
        VEHICLE = 'VEHICLE', 'Vehicle'

    class Status(models.TextChoices):
        AVAILABLE = 'AVAILABLE', 'Available'
        CHECKED_OUT = 'CHECKED_OUT', 'Checked Out'
        MAINTENANCE = 'MAINTENANCE', 'Maintenance'

    asset_tag = models.CharField(max_length=32, unique=True, db_index=True)
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=20, choices=Category.choices)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.AVAILABLE
    )
    purchase_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.asset_tag})"


class Employee(models.Model):
    employee_code = models.CharField(max_length=16, unique=True, db_index=True)
    full_name = models.CharField(max_length=120)
    email = models.EmailField(unique=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.full_name} ({self.employee_code})"


class CheckOut(models.Model):
    asset = models.ForeignKey(
        Asset, on_delete=models.PROTECT, related_name='checkouts'
    )
    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, related_name='checkouts'
    )
    checked_out_at = models.DateTimeField(auto_now_add=True)
    due_at = models.DateTimeField()
    returned_at = models.DateTimeField(null=True, blank=True)
    condition_note = models.TextField(blank=True)

    def __str__(self):
        return f"{self.asset.asset_tag} checked out by {self.employee.employee_code}"


class OverdueNotice(models.Model):
    checkout = models.ForeignKey(
        CheckOut, on_delete=models.CASCADE, related_name='notices'
    )
    notice_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['checkout', 'notice_date'],
                name='unique_checkout_notice_date',
            )
        ]

    def __str__(self):
        return f"Notice for {self.checkout_id} on {self.notice_date}"