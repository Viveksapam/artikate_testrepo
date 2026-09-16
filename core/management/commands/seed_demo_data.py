from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from core.models import Asset, Employee, CheckOut


class Command(BaseCommand):
    help = "Populates the database with demo data for Artikate asset management service."

    def handle(self, *args, **options):
        self.stdout.write("Seeding demo data...")

        # 1. Employees (At least 4, one inactive)
        employees_data = [
            {"code": "EMP-001", "name": "Aarav Sharma", "email": "aarav@example.com", "active": True},
            {"code": "EMP-002", "name": "Diya Patel", "email": "diya@example.com", "active": True},
            {"code": "EMP-003", "name": "Rohan Verma", "email": "rohan@example.com", "active": True},
            {"code": "EMP-004", "name": "Inactive User", "email": "inactive@example.com", "active": False},
        ]

        employee_objs = {}
        for data in employees_data:
            emp, _ = Employee.objects.update_or_create(
                employee_code=data["code"],
                defaults={
                    "full_name": data["name"],
                    "email": data["email"],
                    "is_active": data["active"],
                }
            )
            employee_objs[data["code"]] = emp

        # 2. Assets (At least 8 across all 4 categories: CAMERA, LAPTOP, SENSOR, VEHICLE)
        assets_data = [
            {"tag": "CAM-001", "name": "Sony Alpha 7", "category": Asset.Category.CAMERA},
            {"tag": "CAM-002", "name": "Canon EOS R5", "category": Asset.Category.CAMERA},
            {"tag": "LAP-001", "name": "MacBook Pro 16", "category": Asset.Category.LAPTOP},
            {"tag": "LAP-002", "name": "Dell XPS 15", "category": Asset.Category.LAPTOP},
            {"tag": "SEN-001", "name": "LiDAR Sensor v2", "category": Asset.Category.SENSOR},
            {"tag": "SEN-002", "name": "Thermal Camera Module", "category": Asset.Category.SENSOR},
            {"tag": "VEH-001", "name": "Electric Delivery Scooter", "category": Asset.Category.VEHICLE},
            {"tag": "VEH-002", "name": "Cargo E-Bike", "category": Asset.Category.VEHICLE},
        ]

        asset_objs = {}
        for data in assets_data:
            asset, _ = Asset.objects.update_or_create(
                asset_tag=data["tag"],
                defaults={
                    "name": data["name"],
                    "category": data["category"],
                    "status": Asset.Status.AVAILABLE,
                    "purchase_date": "2025-01-15",
                }
            )
            asset_objs[data["tag"]] = asset

        # 3. Checkouts (Spread: overdue, returned on time, returned late, open)
        now = timezone.now()

        checkout_scenarios = [
            # Overdue 1
            {
                "asset": "CAM-001", "emp": "EMP-001",
                "checked_out": now - timedelta(days=10),
                "due": now - timedelta(days=3),
                "returned": None,
                "status": Asset.Status.CHECKED_OUT
            },
            # Overdue 2
            {
                "asset": "LAP-001", "emp": "EMP-002",
                "checked_out": now - timedelta(days=15),
                "due": now - timedelta(days=1),
                "returned": None,
                "status": Asset.Status.CHECKED_OUT
            },
            # Returned on time 1
            {
                "asset": "SEN-001", "emp": "EMP-003",
                "checked_out": now - timedelta(days=10),
                "due": now - timedelta(days=2),
                "returned": now - timedelta(days=3),
                "status": Asset.Status.AVAILABLE
            },
            # Returned on time 2
            {
                "asset": "CAM-002", "emp": "EMP-002",
                "checked_out": now - timedelta(days=8),
                "due": now - timedelta(days=1),
                "returned": now - timedelta(days=2),
                "status": Asset.Status.AVAILABLE
            },
            # Returned late
            {
                "asset": "VEH-001", "emp": "EMP-001",
                "checked_out": now - timedelta(days=10),
                "due": now - timedelta(days=5),
                "returned": now - timedelta(days=2),
                "status": Asset.Status.AVAILABLE
            },
        ]

        for s in checkout_scenarios:
            asset = asset_objs[s["asset"]]
            emp = employee_objs[s["emp"]]
            
            checkout, created = CheckOut.objects.get_or_create(
                asset=asset,
                employee=emp,
                due_at=s["due"],
                defaults={
                    "returned_at": s["returned"],
                }
            )
            CheckOut.objects.filter(id=checkout.id).update(
                checked_out_at=s["checked_out"],
                returned_at=s["returned"]
            )
            asset.status = s["status"]
            asset.save(update_fields=["status"])

        self.stdout.write(self.style.SUCCESS("Successfully seeded demo data!"))